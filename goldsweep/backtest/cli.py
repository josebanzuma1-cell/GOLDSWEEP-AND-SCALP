"""``python -m goldsweep.backtest``: the one-command backtest report (spec section 10).

Examples::

    python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31
    python -m goldsweep.backtest --from 2024-01-01 --to 2026-10-31 --with-m1
    python -m goldsweep.backtest --from 2026-01-01 --to 2026-03-31 --mock

Prices come from the SQLite database. When it does not cover the period yet,
the history is downloaded from MetaTrader 5 first (the terminal must be open and
logged in). ``--mock`` uses synthetic prices instead, to try the pipeline.

Exit codes: 0 report written, 1 no usable price history, 2 invalid arguments or
configuration.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import pandas as pd

from goldsweep.backtest.data import (
    days_without_bars,
    download_history,
    load_symbol_spec,
    save_symbol_spec,
    summarize_gaps,
)
from goldsweep.backtest.engine import BacktestResult, MarketData, run_backtest
from goldsweep.backtest.metrics import Metrics
from goldsweep.backtest.report import DISCLAIMER, write_report
from goldsweep.core.clock import Clock, SystemClock
from goldsweep.core.logging import setup_logging, shutdown_logging
from goldsweep.core.settings import ConfigError, Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError
from goldsweep.data.candles import aggregate_candles
from goldsweep.data.factory import create_broker
from goldsweep.data.mock_broker import default_gold_spec, generate_m1_candles
from goldsweep.storage.db import Database

#: Days of prices loaded after the end, so trades opened near it can finish.
FOLLOW_THROUGH = dt.timedelta(days=5)
#: Stored history may start or stop this much inside the period before a download is needed.
COVERAGE_SLACK = dt.timedelta(days=4)

BrokerFactory = Callable[[Settings], BrokerClient]


class HistoryError(Exception):
    """No usable price history for the requested period."""


def _day(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2024-01-31") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m goldsweep.backtest",
        description="Backtest the GoldSweep strategy on XAUUSD history and write a report.",
    )
    parser.add_argument("--from", dest="start", type=_day, required=True, help="first day (UTC)")
    parser.add_argument("--to", dest="end", type=_day, required=True, help="last day, included")
    parser.add_argument("--mock", action="store_true", help="synthetic prices, no MetaTrader 5")
    parser.add_argument("--seed", type=int, default=7, help="random seed for --mock prices")
    parser.add_argument(
        "--download", action="store_true", help="download the history from MT5 again first"
    )
    parser.add_argument(
        "--offline", action="store_true", help="never contact MT5; use stored history only"
    )
    parser.add_argument(
        "--with-m1", action="store_true", help="also download one-minute bars for precise fills"
    )
    parser.add_argument("--no-walk-forward", action="store_true", help="skip the walk-forward test")
    parser.add_argument(
        "--threshold",
        choices=("auto_trade", "alert"),
        help="trade signals from the auto-trade threshold (default) or from the alert threshold",
    )
    parser.add_argument(
        "--candidates",
        action="store_true",
        help="trade each session's A+ candidate as if approved, to compare (D87)",
    )
    parser.add_argument("--out", type=Path, help="folder for the report (default: reports/...)")
    parser.add_argument("--config", type=Path, help="config.yaml to use")
    parser.add_argument("--no-cache", action="store_true", help="do not reuse cached signals")
    return parser


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def mock_data(settings: Settings, start: dt.datetime, end: dt.datetime, seed: int) -> MarketData:
    """Synthetic one-minute prices for ``[start, end)``, the trigger timeframe built from them."""
    m1 = generate_m1_candles(start, end, seed=seed)
    trigger_tf = settings.config.timeframes.trigger
    trigger = m1 if trigger_tf is Timeframe.M1 else aggregate_candles(m1, trigger_tf)
    return MarketData(
        symbol="XAUUSD",
        spec=default_gold_spec(),
        trigger=trigger,
        fine=m1,
        source=f"synthetic random walk (mock broker, seed {seed})",
        synthetic=True,
    )


def _covers(frame: pd.DataFrame, start: dt.datetime, end: dt.datetime) -> bool:
    if frame.empty:
        return False
    first = frame.index[0].to_pydatetime()
    last = frame.index[-1].to_pydatetime()
    return first <= start + COVERAGE_SLACK and last >= end - COVERAGE_SLACK


def stored_data(
    settings: Settings,
    db: Database,
    start: dt.datetime,
    end: dt.datetime,
    *,
    download: bool,
    offline: bool,
    with_m1: bool,
    broker_factory: BrokerFactory,
    say: Callable[[str], None],
) -> MarketData:
    """MetaTrader 5 history from the database, downloading what is missing."""
    trigger_tf = settings.config.timeframes.trigger
    spec = load_symbol_spec(db)
    trigger = (
        db.load_candles(spec.name, trigger_tf, start=start, end=end)
        if spec is not None
        else pd.DataFrame()
    )
    m1_missing = with_m1 and (
        spec is None
        or not _covers(db.load_candles(spec.name, Timeframe.M1, start=start, end=end), start, end)
    )
    if download or spec is None or not _covers(trigger, start, end) or m1_missing:
        if offline:
            raise HistoryError(
                f"The database has no {trigger_tf.value} history covering "
                f"{start:%Y-%m-%d} .. {end:%Y-%m-%d}, and --offline forbids downloading it. "
                "Run once without --offline, with MetaTrader 5 open and logged in."
            )
        say("Downloading history from MetaTrader 5 (one month at a time)...")
        with broker_factory(settings) as broker:
            spec = broker.resolve_symbol(settings.config.symbol_candidates)
            save_symbol_spec(db, spec)
            timeframes = [trigger_tf, *([Timeframe.M1] if with_m1 else [])]
            for timeframe in timeframes:
                report = download_history(broker, db, spec.name, timeframe, start, end)
                say(f"  {report.describe()}")
            # Silver in the same session, so MetaTrader 5 is opened once.
            download_silver(settings, db, broker, start, end, say=say)
        trigger = db.load_candles(spec.name, trigger_tf, start=start, end=end)
        download = False  # silver was just fetched too
        offline = True  # nothing more to ask MetaTrader 5 in this run
    assert spec is not None
    if trigger.empty:
        raise HistoryError(
            f"MetaTrader 5 returned no {trigger_tf.value} bars for {start:%Y-%m-%d} .. "
            f"{end:%Y-%m-%d}. Raise Tools > Options > Charts > Max bars in chart to "
            "Unlimited, restart the terminal, and try again with --download."
        )
    fine = None
    if settings.config.backtest.use_m1_for_fills:
        fine = db.load_candles(spec.name, Timeframe.M1, start=start, end=end)
        fine = None if fine.empty else fine
    source = f"MetaTrader 5 history from {db.path} ({spec.name}, {trigger_tf.value}"
    source += ", M1 for fills)" if fine is not None else ")"
    silver = silver_history(
        settings,
        db,
        start,
        end,
        download=download,
        offline=offline,
        broker_factory=broker_factory,
        say=say,
    )
    return MarketData(spec.name, spec, trigger, fine, source, silver=silver)


#: Where the backtester remembers which broker symbol is silver (D85), or
#: NO_SILVER when the broker has none (so it is not asked again on every run).
SILVER_STATE = "backtest.silver_symbol"
NO_SILVER = "-"


def download_silver(
    settings: Settings,
    db: Database,
    broker: BrokerClient,
    start: dt.datetime,
    end: dt.datetime,
    *,
    say: Callable[[str], None],
) -> None:
    """Fetch silver bars for the SMT tag through an open broker session."""
    smt = settings.config.confluence.smt
    if not smt.enabled:
        return
    try:
        silver = broker.resolve_symbol(smt.symbol_candidates)
        db.set_state(SILVER_STATE, silver.name)
        timeframe = settings.config.timeframes.trigger
        report = download_history(broker, db, silver.name, timeframe, start, end)
        say(f"  silver {report.describe()}")
    except BrokerError as exc:
        db.set_state(SILVER_STATE, NO_SILVER)
        say(f"  no silver history ({exc}): the SMT tag is left out")


def silver_history(
    settings: Settings,
    db: Database,
    start: dt.datetime,
    end: dt.datetime,
    *,
    download: bool,
    offline: bool,
    broker_factory: BrokerFactory,
    say: Callable[[str], None],
) -> pd.DataFrame | None:
    """Silver bars for the SMT tag. ``None`` when the tag is off or there is no silver
    history: the backtest goes on without the tag."""
    smt = settings.config.confluence.smt
    if not smt.enabled:
        return None
    trigger_tf = settings.config.timeframes.trigger
    name = db.get_state(SILVER_STATE)
    if name == NO_SILVER and not download:
        return None
    usable = bool(name) and name != NO_SILVER
    frame = db.load_candles(name, trigger_tf, start=start, end=end) if usable else pd.DataFrame()
    if not usable or not _covers(frame, start, end):
        if offline:
            return None if frame.empty else frame
        try:
            with broker_factory(settings) as broker:
                download_silver(settings, db, broker, start, end, say=say)
        except BrokerError as exc:
            say(f"  MetaTrader 5 not reachable for silver ({exc}): the SMT tag is left out")
            return None
        name = db.get_state(SILVER_STATE)
        if not name or name == NO_SILVER:
            return None
        frame = db.load_candles(name, trigger_tf, start=start, end=end)
    return None if frame.empty else frame


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #


def _metrics_line(metrics: Metrics, currency: str) -> str:
    if metrics.trades == 0:
        return "no trades"

    def show(value: float | None, pattern: str) -> str:
        return "n/a" if value is None else pattern.format(value)

    return (
        f"{metrics.trades} trades | win rate {show(metrics.win_rate, '{:.1%}')} | "
        f"profit factor {show(metrics.profit_factor, '{:.2f}')} | "
        f"expectancy {show(metrics.expectancy_r, '{:+.2f} R')} | "
        f"net {metrics.net_profit:+,.2f} {currency} ({metrics.return_pct:+.1f}%) | "
        f"max drawdown {metrics.max_drawdown_pct:.1f}%"
    )


def console_summary(result: BacktestResult, paths: dict[str, Path]) -> list[str]:
    currency = result.data.spec.currency_profit or ""
    lines = [
        f"GoldSweep backtest  {result.data.symbol}  {result.start:%Y-%m-%d} .. "
        f"{result.end - dt.timedelta(days=1):%Y-%m-%d}",
        f"Prices      : {result.data.source}",
        f"Data        : {len(result.data.trigger):,} bars | fingerprint "
        f"{result.fingerprints['data']}",
    ]
    if result.data.synthetic:
        lines.append("              SYNTHETIC PRICES: the results say nothing about real gold.")
    gaps = days_without_bars(result.data.trigger, result.start, result.end)
    lines.append(f"No-bar days : {summarize_gaps(gaps)}")
    counts: dict[str, int] = {}
    for item in result.result.outcomes:
        counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
    detail = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(counts.items()))
    cached = " (reused from cache)" if result.signals_cached else ""
    lines.append(
        f"Signals     : {len(result.records)}{cached}" + (f" -> {detail}" if detail else "")
    )
    which = "A+ candidates as if approved" if result.candidates else "trading rules"
    for number, (playbook, rule) in enumerate(result.playbooks.items()):
        label = "Playbooks" if number == 0 else ""
        lines.append(f"{label:<12}: {playbook:<9}{rule}" + (f"  ({which})" if number == 0 else ""))
    lines.append(f"Strategy    : {_metrics_line(result.metrics, currency)}")
    for name in ("playbook", "session", "weekday", "pressure", "model", "confluence"):
        groups = result.breakdowns.get(name, {})
        for number, (key, metrics) in enumerate(groups.items()):
            label = f"By {name}" if number == 0 else ""
            lines.append(f"{label:<12}: {key:<16} {_metrics_line(metrics, currency)}")
    wf = result.walk_forward
    if wf is not None:
        if wf.folds:
            lines.append(
                f"Walk-forward: {len(wf.folds)} windows | out-of-sample "
                f"{_metrics_line(wf.oos_metrics, currency)}"
            )
            for fold in wf.folds:
                window = f"{fold.fold.test_start:%Y-%m-%d}..{fold.fold.test_end:%Y-%m-%d}"
                lines.append(
                    f"              test {window} with {fold.chosen.describe()}"
                    f"{' (defaults)' if fold.used_defaults else ''}: {fold.test.trades} trades"
                )
        for warning in wf.warnings:
            lines.append(f"!!! WARNING  : {warning}")
    lines.append("Limitations :")
    lines.extend(f"  - {note}" for note in result.limitations)
    lines.append(f"Report      : {paths['report']}")
    folder = paths["report"].parent
    lines.append(f"              trades.csv, signals.csv, equity.csv, summary.json in {folder}")
    lines.append(DISCLAIMER)
    return lines


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    broker_factory: BrokerFactory | None = None,
    clock: Clock | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
    except SystemExit as exc:
        return int(exc.code or 0) and 2
    if args.end < args.start:
        print("--to must not be before --from", file=err)
        return 2
    try:
        settings = load_settings(config_path=args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=err)
        return 2
    if args.threshold:
        backtest = settings.config.backtest.model_copy(update={"trade_threshold": args.threshold})
        settings = dataclasses.replace(
            settings, config=settings.config.model_copy(update={"backtest": backtest})
        )

    def say(text: str) -> None:
        print(text, file=err, flush=True)

    now = (clock or SystemClock()).now()
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    if start >= now:
        print("--from is in the future: there is no history to test yet", file=err)
        return 2
    load_start = start - dt.timedelta(days=settings.config.backtest.warmup_days)
    load_end = min(end + FOLLOW_THROUGH, now)

    setup_logging(settings.log_dir, console=False)
    try:
        if args.mock:
            data = mock_data(settings, load_start, end + FOLLOW_THROUGH, args.seed)
        else:
            with Database(settings.db_path) as db:
                data = stored_data(
                    settings,
                    db,
                    load_start,
                    load_end,
                    download=args.download,
                    offline=args.offline,
                    # M1 refinement needs the one-minute history (D83).
                    with_m1=args.with_m1 or settings.config.strategy.refinement.mode == "m1",
                    broker_factory=broker_factory or (lambda s: create_broker(s)),
                    say=say,
                )
        bars = int((data.trigger.index >= pd.Timestamp(load_start)).sum())
        say(
            f"Replaying {bars:,} {settings.config.timeframes.trigger.value} bars through the "
            "signal engine (the slow part; cached for the next run)..."
        )

        shown: set[int] = set()

        def progress(fraction: float, moment: dt.datetime) -> None:
            decile = int(fraction * 10)
            if decile not in shown:
                shown.add(decile)
                say(f"  {decile * 10:3d}%  {moment:%Y-%m-%d}")

        cache_dir = None if args.no_cache else settings.data_dir / "backtest-cache"
        result = run_backtest(
            settings,
            data,
            start,
            end,
            run_walk_forward=not args.no_walk_forward,
            cache_dir=cache_dir,
            progress=progress,
            candidates=args.candidates,
        )
        name = (
            f"backtest_{args.start:%Y-%m-%d}_{args.end:%Y-%m-%d}"
            + ("_mock" if args.mock else "")
            + ("_candidates" if args.candidates else "")
        )
        out_dir = args.out or settings.config_path.parent / "reports" / name
        command = "python -m goldsweep.backtest " + " ".join(arguments)
        paths = write_report(result, out_dir, command)
    except (HistoryError, BrokerError) as exc:
        print(f"No usable price history: {exc}", file=err)
        return 1
    finally:
        shutdown_logging()
    for line in console_summary(result, paths):
        print(line, file=out)
    return 0

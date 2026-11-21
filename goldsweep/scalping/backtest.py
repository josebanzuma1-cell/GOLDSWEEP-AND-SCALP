"""The scalper's backtest on stored M1 bars (S1: the first verdict, D95).

``python -m goldsweep.scalping.backtest --from 2024-01-01 --to 2026-10-31``

Two passes, like the main backtester:

1. **Setups.** Every closed bar goes through :class:`ScalpEngine` (both strategy
   modes at once), as the live bot will feed it.
2. **Trades.** For each variant (mode x H1 filter x ladder, with and without costs)
   the setups are traded by a :class:`~goldsweep.execution.book.TradeBook` with the
   scalper's risk limits and :class:`~goldsweep.execution.book.Management` rules:
   market entry at the next bar's open, the three children, the adaptive trail, the
   time stop. Bars are bid prices and the worse case is assumed inside a bar (the
   book's rules), which is harsh on one-minute trades.

The gates before an order, in order: one scalp at a time; the configured sessions
(and the clock's blocks); the H1 bias; the cooldown after a stop-out; a stop and a
ladder that pass the cost gate; the book's own risk checks (daily limits, spread).
There is no historical calendar, so no news blackout: a limitation, reported.

Report files carry no run timestamps, so a rerun on the same data is identical.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import numpy as np
import pandas as pd

from goldsweep.analysis.bias import Bias, compute_bias
from goldsweep.analysis.structure import Direction
from goldsweep.backtest.data import load_symbol_spec
from goldsweep.backtest.engine import rolling_median, spread_points
from goldsweep.backtest.metrics import Metrics, breakdown, summarize
from goldsweep.core.clock import SessionCalculator, ensure_utc
from goldsweep.core.settings import AppConfig, Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import SymbolSpec
from goldsweep.data.candles import aggregate_candles
from goldsweep.execution.book import Bar, EventKind, ExitReason, Trade, TradeBook, to_ns
from goldsweep.scalping.engine import MODES, ScalpEngine, ScalpSetup
from goldsweep.scalping.plan import LadderCosts, scalp_management, scalp_plan, scalp_risk
from goldsweep.storage.db import Database

#: H1 bars the bias looks at (structure needs a few dozen swing points at most).
BIAS_HOURS = 120
DISCLAIMER = (
    "Past results on historical data do not predict future results. Every scalp can "
    "lose; the stop-loss is what keeps each loss small."
)


@dataclass(frozen=True, slots=True)
class Variant:
    """One way of trading the setups."""

    mode: str
    htf_filter: bool
    tp_step_r: float
    costs: bool = True

    @property
    def name(self) -> str:
        bias = "H1 bias" if self.htf_filter else "no bias filter"
        cost = "" if self.costs else ", no costs"
        return f"{self.mode}, {bias}, {self.tp_step_r:g} R steps{cost}"


@dataclass
class ScalpRun:
    variant: Variant
    trades: list[Trade]
    outcomes: Counter[str]
    plan_rejections: Counter[str]
    metrics: Metrics
    days: int

    @property
    def per_day(self) -> float:
        return len(self.trades) / self.days if self.days else 0.0

    @property
    def breakeven_win_rate(self) -> float | None:
        """The win rate at which the average win and average loss cancel out."""
        win, loss = self.metrics.average_win_r, self.metrics.average_loss_r
        if win is None or loss is None or win - loss == 0:
            return None
        return -loss / (win - loss)


@dataclass
class Prepared:
    """Everything the trade pass needs, computed once."""

    bars: pd.DataFrame
    spec: SymbolSpec
    setups: list[ScalpSetup]
    lines: np.ndarray  # the step line after each bar
    atrs: np.ndarray  # ATR after each bar
    bias_close_ns: np.ndarray
    bias_values: list[Bias]
    spread_pts: np.ndarray
    median_pts: np.ndarray
    start: dt.datetime
    end: dt.datetime
    timeframe: Timeframe


class _Lines:
    """The step line for the trade book, looked up by bar close."""

    def __init__(self, close_ns: np.ndarray, lines: np.ndarray, atrs: np.ndarray) -> None:
        self._close_ns, self._lines, self._atrs = close_ns, lines, atrs

    def line_at(self, close_ns: int) -> tuple[float, float]:
        i = int(np.searchsorted(self._close_ns, close_ns))
        if i >= len(self._close_ns) or int(self._close_ns[i]) != close_ns:
            return float("nan"), float("nan")
        return float(self._lines[i]), float(self._atrs[i])


def hourly_bias(bars: pd.DataFrame, config: AppConfig) -> tuple[np.ndarray, list[Bias]]:
    """The H1 bias after each H1 close (structure only: no news, no previous-day location)."""
    hourly = aggregate_candles(bars, Timeframe.H1)
    strategy = config.strategy
    values: list[Bias] = []
    closes = hourly["close"].to_numpy(dtype="float64")
    for j in range(len(hourly)):
        frame = hourly.iloc[max(0, j - BIAS_HOURS + 1) : j + 1]
        result = compute_bias(
            frame,
            price=float(closes[j]),
            previous_mid=None,
            news_bias=0.0,
            config=strategy.bias,
            swing_lookback=strategy.swing_lookback,
        )
        values.append(result.bias)
    close_ns = _nanoseconds(hourly.index + pd.Timedelta(hours=1))
    return close_ns, values


def _nanoseconds(index: pd.DatetimeIndex) -> np.ndarray:
    """Epoch nanoseconds, whatever unit pandas stores the index in."""
    return np.asarray(index.as_unit("ns").asi8, dtype="int64")


def prepare(
    settings: Settings,
    bars: pd.DataFrame,
    spec: SymbolSpec,
    start: dt.datetime,
    end: dt.datetime,
) -> Prepared:
    """Pass 1: the setups of both modes, the step line, the H1 bias and the spreads."""
    config = settings.config
    scalper = config.scalper
    timeframe = Timeframe(scalper.timeframe)
    if timeframe is not Timeframe.M1:
        bars = aggregate_candles(bars, timeframe)
    sessions = SessionCalculator(config.sessions)
    engine = ScalpEngine(scalper, bar=timeframe.duration, sessions=sessions, modes=MODES)
    opens = bars["open"].to_numpy(dtype="float64")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")
    times = bars.index.to_pydatetime()
    lines = np.full(len(bars), np.nan)
    atrs = np.full(len(bars), np.nan)
    setups: list[ScalpSetup] = []
    for i in range(len(bars)):
        found = engine.on_bar(
            times[i], float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
        )
        lines[i] = engine.line
        atrs[i] = engine.atr if engine.atr is not None else np.nan
        setups.extend(found)
    bias_ns, bias_values = hourly_bias(bars, config)
    costs = config.backtest.costs
    points = spread_points(bars, costs)
    median = rolling_median(points, bars.index, config.risk.spread_median_days)
    return Prepared(
        bars=bars,
        spec=spec,
        setups=setups,
        lines=lines,
        atrs=atrs,
        bias_close_ns=bias_ns,
        bias_values=bias_values,
        spread_pts=points,
        median_pts=median,
        start=ensure_utc(start),
        end=ensure_utc(end),
        timeframe=timeframe,
    )


def _bias_at(prepared: Prepared, moment_ns: int) -> Bias:
    j = int(np.searchsorted(prepared.bias_close_ns, moment_ns, side="right")) - 1
    return prepared.bias_values[j] if j >= 0 else Bias.NEUTRAL


def simulate(settings: Settings, prepared: Prepared, variant: Variant) -> ScalpRun:
    """Pass 2: trade the setups of ``variant.mode`` under the scalper's rules."""
    config = settings.config
    scalper = config.scalper.model_copy(update={"tp_step_r": variant.tp_step_r})
    costs_cfg = config.backtest.costs
    if not variant.costs:
        costs_cfg = costs_cfg.model_copy(update={"slippage_points": 0.0, "commission_per_lot": 0.0})
    spec = prepared.spec
    bars = prepared.bars
    step = prepared.timeframe.duration
    step_ns = int(step.total_seconds() * 1e9)
    open_ns = _nanoseconds(bars.index)
    close_ns = open_ns + step_ns
    opens = bars["open"].to_numpy(dtype="float64")
    highs = bars["high"].to_numpy(dtype="float64")
    lows = bars["low"].to_numpy(dtype="float64")
    closes = bars["close"].to_numpy(dtype="float64")
    spreads = prepared.spread_pts * spec.point if variant.costs else np.zeros(len(bars))
    base_costs = LadderCosts.of(spec, costs_cfg, 0.0)
    book = TradeBook(
        spec,
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=config.backtest.initial_balance,
        slippage_points=costs_cfg.slippage_points,
        commission_per_lot=costs_cfg.commission_per_lot,
        trail_after_tp1=False,
        management=scalp_management(scalper, base_costs.breakeven_offset),
    )
    lines = _Lines(close_ns, prepared.lines, prepared.atrs)
    sessions = SessionCalculator(config.sessions)
    start_ns, end_ns = to_ns(prepared.start), to_ns(prepared.end)
    setups = [s for s in prepared.setups if s.mode == variant.mode]
    trades: list[Trade] = []
    outcomes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    last_stop: dict[Direction, dt.datetime] = {}
    cooldown = dt.timedelta(seconds=config.scalper.risk.cooldown_sec)
    count = len(bars)
    j = 0
    i = 0
    number = 0

    def bar_at(k: int) -> Bar:
        return Bar(
            int(open_ns[k]),
            int(close_ns[k]),
            float(opens[k]),
            float(highs[k]),
            float(lows[k]),
            float(closes[k]),
            float(spreads[k]),
        )

    while i < count:
        if book.idle:
            while j < len(setups) and to_ns(setups[j].time) < start_ns:
                j += 1
            if j >= len(setups) or to_ns(setups[j].time) >= end_ns:
                break
            i = max(i, setups[j].index)
        for event in book.on_bar(bar_at(i), line=lines):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
                if event.trade.exit_reason is ExitReason.STOP:
                    last_stop[event.plan.direction] = event.trade.exit_time
        while j < len(setups) and setups[j].index <= i:
            setup = setups[j]
            j += 1
            if setup.index < i or not (start_ns <= to_ns(setup.time) < end_ns):
                continue
            if not book.idle:
                outcomes["busy"] += 1
                continue
            status = sessions.status(setup.time)
            if not status.allowed or status.session not in config.scalper.sessions:
                outcomes["outside_sessions"] += 1
                continue
            if variant.htf_filter and not _bias_at(prepared, to_ns(setup.time)).allows(
                setup.direction
            ):
                outcomes["against_bias"] += 1
                continue
            stopped = last_stop.get(setup.direction)
            if stopped is not None and setup.time - stopped < cooldown:
                outcomes["cooldown"] += 1
                continue
            number += 1
            costs = LadderCosts.of(spec, costs_cfg, float(prepared.spread_pts[i]))
            if not variant.costs:
                costs = LadderCosts(0.0, 0.0, 0.0)
            made = scalp_plan(
                setup,
                scalper,
                costs,
                plan_id=f"SC-{number:05d}",
                session=status.session,
                bar=step,
            )
            if made.plan is None:
                outcomes["no_plan"] += 1
                rejections[made.reason.split(" (")[0]] += 1
                continue
            event = book.submit(
                made.plan,
                setup,
                spread_points=float(prepared.spread_pts[i]) if variant.costs else 0.0,
                median_spread_points=float(prepared.median_pts[i]) if variant.costs else None,
            )
            if event.kind is EventKind.PLACED:
                outcomes["traded"] += 1
            else:
                outcomes[event.outcome.value if event.outcome else "rejected"] += 1
        i += 1
    if not book.idle:
        last = count - 1
        quote = float(closes[last])
        for event in book.close_everything(
            bars.index[last].to_pydatetime() + step,
            quote,
            quote + float(spreads[last]),
            ExitReason.END_OF_DATA,
            "end of data",
        ):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
    in_period = bars.index[(bars.index >= prepared.start) & (bars.index < prepared.end)]
    days = len(set(in_period.date))
    balance = config.backtest.initial_balance
    return ScalpRun(variant, trades, outcomes, rejections, summarize(trades, balance), days)


def variants(config: AppConfig) -> list[Variant]:
    """Both modes, with and without the H1 filter, the configured ladder and the wide one."""
    ladders = sorted({config.scalper.tp_step_r, 2.0})
    return [
        Variant(mode, htf, ladder) for mode in MODES for htf in (True, False) for ladder in ladders
    ]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def run_line(run: ScalpRun, gross: ScalpRun | None) -> str:
    m = run.metrics
    pf = "n/a" if m.profit_factor is None else f"{m.profit_factor:.2f}"
    gross_r = _r(gross.metrics.expectancy_r) if gross is not None else "n/a"
    return (
        f"{run.variant.name:<52} {m.trades:>6} {run.per_day:>6.2f} {_pct(m.win_rate):>7} "
        f"{_pct(run.breakeven_win_rate):>7} {pf:>6} {_r(m.expectancy_r):>7} {gross_r:>7} "
        f"{m.total_r:>+9.1f} {m.return_pct:>+8.1f}% {m.max_drawdown_pct:>6.1f}%"
    )


HEADER = (
    f"{'variant':<52} {'trades':>6} {'a day':>6} {'won':>7} {'BE won':>7} {'PF':>6} "
    f"{'R/trade':>7} {'gross':>7} {'total R':>9} {'return':>9} {'max DD':>7}"
)


def _by(
    trades: Sequence[Trade], key: Callable[[Trade], str], balance: float
) -> dict[str, dict[str, object]]:
    groups = breakdown(trades, key, balance)
    return {
        str(name): {
            "trades": m.trades,
            "win_rate": m.win_rate,
            "expectancy_r": m.expectancy_r,
            "total_r": m.total_r,
        }
        for name, m in groups.items()
    }


def breakdowns(run: ScalpRun, balance: float) -> dict[str, dict[str, dict[str, object]]]:
    trades = run.trades
    return {
        "session": _by(trades, lambda t: t.plan.session or "none", balance),
        "direction": _by(trades, lambda t: t.plan.direction.value, balance),
        "exit": _by(trades, lambda t: t.exit_reason.value, balance),
        "level": _by(trades, lambda t: getattr(t.payload, "level_kind", None) or "line", balance),
        "hour_utc": _by(trades, lambda t: f"{t.fill_time.hour:02d}", balance),
        "year": _by(trades, lambda t: str(t.fill_time.year), balance),
    }


def write_report(
    folder: Path,
    runs: list[tuple[ScalpRun, ScalpRun]],
    chosen: ScalpRun,
    *,
    command: str,
    balance: float,
    period: tuple[str, str],
) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "command": command,
        "period": {"from": period[0], "to": period[1]},
        "variants": [
            {
                "variant": net.variant.name,
                "trades": net.metrics.trades,
                "per_day": round(net.per_day, 4),
                "win_rate": net.metrics.win_rate,
                "breakeven_win_rate": net.breakeven_win_rate,
                "profit_factor": net.metrics.profit_factor,
                "expectancy_r": net.metrics.expectancy_r,
                "gross_expectancy_r": gross.metrics.expectancy_r,
                "total_r": net.metrics.total_r,
                "return_pct": net.metrics.return_pct,
                "max_drawdown_pct": net.metrics.max_drawdown_pct,
                "average_hold_minutes": net.metrics.average_hold_minutes,
                "outcomes": dict(sorted(net.outcomes.items())),
                "no_plan": dict(sorted(net.plan_rejections.items())),
            }
            for net, gross in runs
        ],
        "configured": {
            "variant": chosen.variant.name,
            "breakdowns": breakdowns(chosen, balance),
        },
        "limitations": [
            "No historical economic calendar: no news blackout was applied.",
            "Bars only: inside a one-minute bar the worse case is assumed (a stop and a "
            "target in the same bar count as the stop).",
            "Market entries fill at the next bar's open plus slippage; the live delay of "
            "one to three seconds after the close is not modelled separately.",
            "The H1 bias here is structure only (no news, no previous-day location).",
        ],
        "disclaimer": DISCLAIMER,
    }
    paths = {"summary": folder / "summary.json", "trades": folder / "trades.csv"}
    paths["summary"].write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    with paths["trades"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("id", "direction", "session", "level", "fill_time", "fill_price", "stop_loss",
             "tp1", "tp2", "tp3", "lots", "exit_reason", "exit_time", "net_pnl", "r_multiple")
        )  # fmt: skip
        for t in chosen.trades:
            writer.writerow(
                (
                    t.plan.id,
                    t.plan.direction.value,
                    t.plan.session,
                    getattr(t.payload, "level_kind", None) or "line",
                    f"{t.fill_time:%Y-%m-%d %H:%M}",
                    f"{t.fill_price:.3f}",
                    f"{t.stop_loss:.3f}",
                    f"{t.take_profit_1:.3f}",
                    "" if t.take_profit_2 is None else f"{t.take_profit_2:.3f}",
                    "" if t.take_profit_3 is None else f"{t.take_profit_3:.3f}",
                    f"{t.lots:g}",
                    t.exit_reason.value,
                    f"{t.exit_time:%Y-%m-%d %H:%M}",
                    f"{t.net_pnl:.2f}",
                    f"{t.r_multiple:.3f}",
                )
            )
    return paths


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m goldsweep.scalping.backtest")
    parser.add_argument("--from", dest="start", type=_day, required=True)
    parser.add_argument("--to", dest="end", type=_day, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    settings = load_settings(config_path=args.config)
    config = settings.config
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    warmup = dt.timedelta(days=config.backtest.warmup_days)
    with Database(settings.db_path) as db:
        spec = load_symbol_spec(db)
        if spec is None:
            print("No stored history: run the main backtest once to download it.", file=out)
            return 1
        bars = db.load_candles(spec.name, Timeframe.M1, start=start - warmup, end=end)
    if bars.empty:
        print(
            "No stored M1 bars for that period (python -m goldsweep.backtest --with-m1).", file=out
        )
        return 1
    print(f"Replaying {len(bars):,} M1 bars through the scalp engine...", file=out, flush=True)
    prepared = prepare(settings, bars, spec, start, end)
    balance = config.backtest.initial_balance
    runs: list[tuple[ScalpRun, ScalpRun]] = []
    for variant in variants(config):
        net = simulate(settings, prepared, variant)
        gross = simulate(
            settings,
            prepared,
            Variant(variant.mode, variant.htf_filter, variant.tp_step_r, costs=False),
        )
        runs.append((net, gross))
    configured = Variant(
        config.scalper.strategy, config.scalper.htf_filter, config.scalper.tp_step_r
    )
    chosen = next(net for net, _ in runs if net.variant == configured)
    period = (f"{args.start:%Y-%m-%d}", f"{args.end:%Y-%m-%d}")
    folder = args.out or settings.config_path.parent / "reports" / (
        f"scalper_{period[0]}_{period[1]}"
    )
    command = "python -m goldsweep.scalping.backtest " + " ".join(
        sys.argv[1:] if argv is None else argv
    )
    paths = write_report(folder, runs, chosen, command=command, balance=balance, period=period)
    print(f"GoldSweep scalper backtest {spec.name} {period[0]} .. {period[1]}", file=out)
    print(f"Setups: {len(prepared.setups):,} (both modes)", file=out)
    print(HEADER, file=out)
    for net, gross in runs:
        print(run_line(net, gross), file=out)
    print(f"Configured: {chosen.variant.name}", file=out)
    for name, groups in breakdowns(chosen, balance).items():
        text = " | ".join(
            f"{key} {value['trades']} ({_r(value['expectancy_r'])} R)"  # type: ignore[arg-type]
            for key, value in groups.items()
        )
        print(f"  by {name}: {text}", file=out)
    print(f"  outcomes: {dict(sorted(chosen.outcomes.items()))}", file=out)
    print(f"  no plan: {dict(sorted(chosen.plan_rejections.items()))}", file=out)
    print(f"Report: {paths['summary']}", file=out)
    print(DISCLAIMER, file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

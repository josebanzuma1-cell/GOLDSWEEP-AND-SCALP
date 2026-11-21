"""The swing mode's backtest and its evidence gate (S4, D110).

``python -m goldsweep.scalping.swing_backtest --from 2014-01-01 --to 2026-10-31``

1. **History.** H4 bars of the gold symbol, stored in SQLite. The first run downloads
   them from MetaTrader 5 (read-only, month by month) and refreshes the broker's
   symbol facts, swap included; ``--offline`` never contacts MT5.
2. **Setups.** Daily bars (from the H4 bars, UTC days, weekend stubs left out) are fed
   one at a time to :class:`~goldsweep.scalping.swing.SwingEngine` (all rules).
3. **Trades.** Each rule is traded on the H4 bars with every ladder of
   ``scalper.swing.ladders``: market entry at the next open, three children,
   breakeven+ at TP1, the stop to TP1 after TP2, the 20-day channel trailing
   breakouts and pullbacks, momentum closed at each month's start, and the overnight
   swap charged every night (triple once a week). Each variant also runs with no
   spread, slippage or swap.
4. **The gate (D103).** The ladder with the best R per trade on the first two-thirds
   is chosen; the rule passes only if it is positive on the held-out third, positive
   overall after costs, with at least ``min_trades`` trades.

No historical calendar exists for these years, so no news blackout applies.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import numpy as np
import pandas as pd

from goldsweep.backtest.data import (
    SPEC_STATE_KEY,
    download_history,
    load_symbol_spec,
    save_symbol_spec,
)
from goldsweep.backtest.engine import rolling_median, spread_points
from goldsweep.backtest.metrics import Metrics, summarize
from goldsweep.core.clock import ensure_utc
from goldsweep.core.settings import Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError, SymbolSpec
from goldsweep.data.candles import aggregate_candles
from goldsweep.execution.book import Bar, EventKind, ExitReason, Trade, TradeBook, to_ns
from goldsweep.scalping.backtest import DISCLAIMER, _nanoseconds
from goldsweep.scalping.intraday_backtest import HEADER, _line, _metrics_json, _r
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.scalping.swing import (
    MOMENTUM,
    RULES,
    SwingEngine,
    SwingSetup,
    swing_management,
    swing_plan,
)
from goldsweep.storage.db import Database

MANAGE_TF = Timeframe.H4
#: Days of history before ``--from`` (the 200-day average and 12 month-ends need them).
WARMUP = dt.timedelta(days=420)
#: The broker's H4 history is downloaded once; this remembers how far back was asked
#: (it holds nothing before January 2014, so a request reaching further is still covered).
H4_FETCHED_KEY = "swing.h4_fetched_from"


@dataclass
class SwingData:
    h4: pd.DataFrame
    spec: SymbolSpec
    setups: list[SwingSetup]
    day_close_ns: np.ndarray
    channel_low: np.ndarray
    channel_high: np.ndarray
    spread_pts: np.ndarray  # per H4 bar
    median_pts: np.ndarray
    start: dt.datetime
    end: dt.datetime


@dataclass
class SwingRun:
    rule: str
    ladder: tuple[float, ...]
    costs: bool
    trades: list[Trade]
    outcomes: Counter[str]
    metrics: Metrics

    @property
    def name(self) -> str:
        steps = "/".join(f"{s:g}" for s in self.ladder)
        return f"{self.rule}, {steps} R" + ("" if self.costs else ", no costs")

    @property
    def swap(self) -> float:
        return sum(t.swap for t in self.trades)


@dataclass
class SwingVerdict:
    rule: str
    chosen: SwingRun
    gross: SwingRun
    first: Metrics
    held_out: Metrics
    passed: bool
    reason: str


class _Channel:
    """The last ``exit_days`` days' low (for longs) or high (for shorts) at an H4 close."""

    def __init__(self, data: SwingData) -> None:
        self._close_ns, self._low, self._high = (
            data.day_close_ns,
            data.channel_low,
            data.channel_high,
        )

    def _at(self, close_ns: int) -> int:
        return int(np.searchsorted(self._close_ns, close_ns, side="right")) - 1

    def line_for(self, close_ns: int, long: bool) -> tuple[float, float]:
        i = self._at(close_ns)
        if i < 0:
            return float("nan"), float("nan")
        return float(self._low[i] if long else self._high[i]), 0.0

    def line_at(self, close_ns: int) -> tuple[float, float]:
        return self.line_for(close_ns, True)


def daily_bars(h4: pd.DataFrame) -> pd.DataFrame:
    """UTC days from H4 bars, without the weekend's stubs (Sunday's first hour)."""
    days = aggregate_candles(h4, Timeframe.D1)
    return days.loc[days.index.day_of_week < 5]


def prepare(
    settings: Settings, h4: pd.DataFrame, spec: SymbolSpec, start: dt.datetime, end: dt.datetime
) -> SwingData:
    config = settings.config
    days = daily_bars(h4)
    engine = SwingEngine(config.scalper, rules=RULES)
    times = days.index.to_pydatetime()
    columns = [days[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")]
    low = np.full(len(days), np.nan)
    high = np.full(len(days), np.nan)
    setups: list[SwingSetup] = []
    for i in range(len(days)):
        setups += engine.on_day(
            times[i],
            float(columns[0][i]),
            float(columns[1][i]),
            float(columns[2][i]),
            float(columns[3][i]),
        )
        low[i], high[i] = engine.channel_low, engine.channel_high
    points = spread_points(h4, config.backtest.costs)
    return SwingData(
        h4=h4,
        spec=spec,
        setups=setups,
        day_close_ns=_nanoseconds(days.index) + 86_400 * 10**9,
        channel_low=low,
        channel_high=high,
        spread_pts=points,
        median_pts=rolling_median(points, h4.index, config.risk.spread_median_days),
        start=ensure_utc(start),
        end=ensure_utc(end),
    )


def simulate(
    settings: Settings, data: SwingData, rule: str, ladder: tuple[float, ...], *, costs: bool
) -> SwingRun:
    """Trade ``rule``'s setups on the H4 bars with ``ladder`` (with or without costs)."""
    config = settings.config
    scalper = config.scalper
    costs_cfg = config.backtest.costs
    if not costs:
        costs_cfg = costs_cfg.model_copy(update={"slippage_points": 0.0, "commission_per_lot": 0.0})
    spec, h4 = data.spec, data.h4
    step = MANAGE_TF.duration
    open_ns = _nanoseconds(h4.index)
    close_ns = open_ns + int(step.total_seconds() * 1e9)
    opens, highs, lows, closes = (
        h4[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")
    )
    spreads = data.spread_pts * spec.point if costs else np.zeros(len(h4))
    base = LadderCosts.of(spec, costs_cfg, 0.0)
    book = TradeBook(
        spec,
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=config.backtest.initial_balance,
        slippage_points=costs_cfg.slippage_points,
        commission_per_lot=costs_cfg.commission_per_lot,
        trail_after_tp1=False,
        management=swing_management(
            scalper, base.breakeven_offset, trail=rule != MOMENTUM, swap=costs
        ),
    )
    channel = _Channel(data) if rule != MOMENTUM else None
    start_ns, end_ns = to_ns(data.start), to_ns(data.end)
    setups = [s for s in data.setups if s.rule == rule and start_ns <= to_ns(s.time) < end_ns]
    at = [max(0, int(np.searchsorted(close_ns, to_ns(s.time), side="right")) - 1) for s in setups]
    trades: list[Trade] = []
    outcomes: Counter[str] = Counter()
    count = len(h4)
    i = j = number = 0
    while i < count:
        if book.idle:
            if j >= len(setups):
                break
            i = max(i, at[j])
        bar = Bar(
            int(open_ns[i]),
            int(close_ns[i]),
            float(opens[i]),
            float(highs[i]),
            float(lows[i]),
            float(closes[i]),
            float(spreads[i]),
        )
        for event in book.on_bar(bar, line=channel):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
        while j < len(setups) and at[j] <= i:
            setup = setups[j]
            j += 1
            if at[j - 1] < i or not book.idle:
                outcomes["busy"] += 1
                continue
            number += 1
            here = (
                LadderCosts.of(spec, costs_cfg, float(data.spread_pts[i]))
                if costs
                else LadderCosts(0.0, 0.0, 0.0)
            )
            made = swing_plan(
                setup,
                scalper.swing,
                ladder,
                here,
                cost_multiple=scalper.risk.cost_multiple,
                plan_id=f"SW-{number:05d}",
                bar=step,
            )
            if made.plan is None:
                outcomes["no_plan"] += 1
                continue
            event = book.submit(
                made.plan,
                setup,
                spread_points=float(data.spread_pts[i]) if costs else 0.0,
                median_spread_points=None,  # spreads on H4 history vary by era; no filter
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
            h4.index[last].to_pydatetime() + step,
            quote,
            quote + float(spreads[last]),
            ExitReason.END_OF_DATA,
            "end of data",
        ):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
    balance = config.backtest.initial_balance
    return SwingRun(rule, ladder, costs, trades, outcomes, summarize(trades, balance))


def judge(settings: Settings, data: SwingData, rule: str) -> tuple[SwingVerdict, list[SwingRun]]:
    """Choose a ladder on the first two-thirds; pass only if the held-out third is positive."""
    swing = settings.config.scalper.swing
    balance = settings.config.backtest.initial_balance
    split = data.start + (data.end - data.start) * 2 / 3
    runs = [simulate(settings, data, rule, ladder, costs=True) for ladder in swing.ladders]

    def part(run: SwingRun, first: bool) -> Metrics:
        return summarize([t for t in run.trades if (t.fill_time < split) == first], balance)

    best = max(runs, key=lambda run: (part(run, True).expectancy_r or -99.0, -len(run.ladder)))
    gross = simulate(settings, data, rule, best.ladder, costs=False)
    first, held_out = part(best, True), part(best, False)
    reasons = []
    if best.metrics.trades < swing.min_trades:
        reasons.append(f"{best.metrics.trades} trades, fewer than {swing.min_trades}")
    if (first.expectancy_r or 0.0) <= 0:
        reasons.append("negative on the first two-thirds")
    if (held_out.expectancy_r or 0.0) <= 0:
        reasons.append("negative on the held-out third")
    if (best.metrics.expectancy_r or 0.0) <= 0:
        reasons.append("negative after costs overall")
    verdict = SwingVerdict(
        rule, best, gross, first, held_out, not reasons, "; ".join(reasons) or "passed"
    )
    return verdict, runs


def feasibility(settings: Settings, data: SwingData) -> tuple[float, float] | None:
    """(median first-stop distance of the last year's setups, the balance at which the
    minimum lot stays within the risk per trade), or None without setups."""
    last_year = [s for s in data.setups if s.time >= data.end - dt.timedelta(days=365)]
    if not last_year:
        return None
    swing = settings.config.scalper.swing
    stop = float(np.median([swing.stop_atr * s.atr for s in last_year]))
    loss = stop * data.spec.value_per_price_unit * data.spec.volume_min
    return stop, loss / (settings.config.scalper.risk.risk_per_scalp_pct / 100.0)


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def stored_h4(
    settings: Settings,
    db: Database,
    start: dt.datetime,
    end: dt.datetime,
    *,
    offline: bool,
    broker: BrokerClient | None = None,
    say: TextIO | None = None,
) -> tuple[pd.DataFrame, SymbolSpec]:
    """H4 bars of ``[start, end)`` and the symbol facts (swap included), downloading once."""
    spec = load_symbol_spec(db)
    stored = db.get_state(SPEC_STATE_KEY) or {}
    h4 = db.load_candles(spec.name, MANAGE_TF, start=start, end=end) if spec else pd.DataFrame()
    fetched = db.get_state(H4_FETCHED_KEY)  # the earliest start already asked of the broker
    week = pd.Timedelta(days=7)
    covered = (
        not h4.empty
        and h4.index[-1] >= pd.Timestamp(end) - week
        and (
            h4.index[0] <= pd.Timestamp(start) + week
            or (fetched is not None and dt.datetime.fromisoformat(fetched) <= start)
        )
    )
    if covered and "swap_mode" in stored:
        assert spec is not None
        return h4, spec
    if offline:
        raise BrokerError(
            "the H4 history or the broker's swap terms are not stored yet: run once without "
            "--offline (MetaTrader 5 open and logged in)"
        )
    from goldsweep.data.factory import create_broker

    client = broker or create_broker(settings)
    client.connect()
    try:
        fresh = client.resolve_symbol(settings.config.symbol_candidates)
        save_symbol_spec(db, fresh)
        if not covered:
            download_history(
                client,
                db,
                fresh.name,
                MANAGE_TF,
                start,
                end,
                progress=(lambda text: print(f"  {text}", file=say)) if say else None,
            )
            db.set_state(H4_FETCHED_KEY, ensure_utc(start).isoformat())
    finally:
        if broker is None:
            client.shutdown()
    return db.load_candles(fresh.name, MANAGE_TF, start=start, end=end), fresh


# --------------------------------------------------------------------------- #
# Report and entry point
# --------------------------------------------------------------------------- #


def write_report(
    folder: Path,
    verdicts: list[SwingVerdict],
    runs: list[SwingRun],
    *,
    command: str,
    period: str,
    spec: SymbolSpec,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "command": command,
        "period": period,
        "swap_per_lot_per_night": {
            "long": spec.swap_per_lot(True),
            "short": spec.swap_per_lot(False),
            "triple_day_mt5": spec.swap_rollover3days,
        },
        "variants": [
            {
                "variant": run.name,
                "metrics": _metrics_json(run.metrics),
                "swap_paid": round(run.swap, 2),
                "outcomes": dict(sorted(run.outcomes.items())),
            }
            for run in runs
        ],  # fmt: skip
        "verdicts": [
            {
                "rule": v.rule,
                "chosen": v.chosen.name,
                "passed": v.passed,
                "reason": v.reason,
                "first_two_thirds": _metrics_json(v.first),
                "held_out_third": _metrics_json(v.held_out),
                "without_costs": _metrics_json(v.gross.metrics),
            }
            for v in verdicts
        ],
        "limitations": [
            "No historical economic calendar: no news blackout was applied.",
            "Swap is charged at each 00:00 UTC rollover at today's rates for every year; "
            "the broker's rates and rollover hour changed over time.",
            "Bid H4 bars: inside a bar the worse case is assumed; spreads come from the bars.",
            "Daily bars are UTC days built from H4 bars.",
        ],
        "disclaimer": DISCLAIMER,
    }
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    with (folder / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("rule", "id", "direction", "fill_time", "fill_price", "stop_loss", "exit_reason",
             "exit_time", "swap", "net_pnl", "r_multiple")
        )  # fmt: skip
        for verdict in verdicts:
            for t in verdict.chosen.trades:
                writer.writerow(
                    (
                        verdict.rule,
                        t.plan.id,
                        t.plan.direction.value,
                        f"{t.fill_time:%Y-%m-%d %H:%M}",
                        f"{t.fill_price:.3f}",
                        f"{t.stop_loss:.3f}",
                        t.exit_reason.value,
                        f"{t.exit_time:%Y-%m-%d %H:%M}",
                        f"{t.swap:.2f}",
                        f"{t.net_pnl:.2f}",
                        f"{t.r_multiple:.3f}",
                    )
                )
    return folder / "summary.json"


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    broker: BrokerClient | None = None,
) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m goldsweep.scalping.swing_backtest")
    parser.add_argument("--from", dest="start", type=_day, required=True)
    parser.add_argument("--to", dest="end", type=_day, required=True)
    parser.add_argument("--offline", action="store_true", help="never contact MetaTrader 5")
    parser.add_argument(
        "--balance",
        type=float,
        help="notional starting balance (default: backtest.initial_balance); a large one "
        "measures the rules' R without the minimum lot getting in the way",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    settings = load_settings(config_path=args.config)
    if args.balance:
        backtest = settings.config.backtest.model_copy(update={"initial_balance": args.balance})
        settings = dataclasses.replace(
            settings, config=settings.config.model_copy(update={"backtest": backtest})
        )
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    with Database(settings.db_path) as db:
        try:
            h4, spec = stored_h4(
                settings, db, start - WARMUP, end, offline=args.offline, broker=broker, say=out
            )
        except BrokerError as exc:
            print(f"No usable history: {exc}", file=out)
            return 1
    if h4.empty:
        print("No H4 bars for that period.", file=out)
        return 1
    rate = spec.swap_per_lot(True)
    if rate is None:
        print(f"The broker's swap mode {spec.swap_mode} is not modelled.", file=out)
        return 1
    print(
        f"Replaying {len(h4):,} H4 bars ({h4.index[0]:%Y-%m-%d} on) through the swing rules; "
        f"swap per lot per night: long {rate:+.2f}, short {spec.swap_per_lot(False):+.2f}",
        file=out,
    )
    data = prepare(settings, h4, spec, start, end)
    verdicts: list[SwingVerdict] = []
    runs: list[SwingRun] = []
    for rule in settings.config.scalper.swing.rules:
        verdict, tried = judge(settings, data, rule)
        verdicts.append(verdict)
        runs += tried
    period = f"{args.start:%Y-%m-%d} .. {args.end:%Y-%m-%d}"
    folder = args.out or settings.config_path.parent / "reports" / (
        f"swing_{args.start:%Y-%m-%d}_{args.end:%Y-%m-%d}"
    )
    command = "python -m goldsweep.scalping.swing_backtest " + " ".join(arguments)
    path = write_report(folder, verdicts, runs, command=command, period=period, spec=spec)
    counts = Counter(s.rule for s in data.setups)
    print(f"GoldSweep swing mode {spec.name} {period}", file=out)
    print("Setups: " + ", ".join(f"{rule} {counts.get(rule, 0)}" for rule in RULES), file=out)
    need = feasibility(settings, data)
    if need is not None:
        stop, balance = need
        print(
            f"Size: the median first stop of the last year is {stop:.2f} an ounce; at "
            f"{settings.config.scalper.risk.risk_per_scalp_pct:g}% a trade the minimum "
            f"{spec.volume_min:g} lot needs a balance of at least {balance:,.0f}",
            file=out,
        )
    print(HEADER + "    swap", file=out)
    for run in runs:
        print(f"{_line(run.name, run.metrics)} {run.swap:>+9.2f}", file=out)
    for v in verdicts:
        mark = "PASSED" if v.passed else "failed"
        print(
            f"{v.rule}: {mark} ({v.reason}). Chosen {v.chosen.name}: first two-thirds "
            f"{v.first.trades} trades {_r(v.first.expectancy_r)} R; held-out third "
            f"{v.held_out.trades} trades {_r(v.held_out.expectancy_r)} R; without costs "
            f"{_r(v.gross.metrics.expectancy_r)} R",
            file=out,
        )
    print(f"Report: {path}", file=out)
    print(DISCLAIMER, file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

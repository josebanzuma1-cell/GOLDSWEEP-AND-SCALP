"""The intraday mode's backtest and its evidence gate (S3, D109).

``python -m goldsweep.scalping.intraday_backtest --from 2024-01-01 --to 2026-10-31``

1. **Setups.** The stored M1 bars are aggregated to M5 and fed, one closed bar at a
   time, to :class:`~goldsweep.scalping.intraday.IntradayEngine` (all three rules).
2. **Trades.** Each rule is traded on the M1 bars, with every ladder of
   ``scalper.intraday.ladders``, by a :class:`~goldsweep.execution.book.TradeBook`
   with the scalper's risk limits: market entry at the next bar's open, three
   children, breakeven+ at TP1, the stop to TP1 after TP2, the VWAP trailing the
   ``vwap_trend`` stop, flat by the New York rollover. Bars are bid prices; inside a
   bar the worse case is assumed. Each variant also runs without costs.
3. **The gate (D103).** For each rule the ladder with the best R per trade on the
   first two-thirds of the period is chosen; the rule passes only if that ladder is
   positive on the held-out last third, positive overall after costs, and has at
   least ``min_trades`` trades. A rule that fails stays off.

No historical calendar is stored, so no news blackout applies here (a limitation, as
in every backtest of this project). Report files carry no run timestamps.
"""

from __future__ import annotations

import argparse
import csv
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

from goldsweep.backtest.data import load_symbol_spec
from goldsweep.backtest.engine import rolling_median, spread_points
from goldsweep.backtest.metrics import Metrics, summarize
from goldsweep.core.clock import SessionCalculator, ensure_utc
from goldsweep.core.settings import Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import SymbolSpec
from goldsweep.data.candles import aggregate_candles
from goldsweep.execution.book import Bar, EventKind, ExitReason, Trade, TradeBook, to_ns
from goldsweep.scalping.backtest import DISCLAIMER, _nanoseconds
from goldsweep.scalping.intraday import (
    RULES,
    VWAP_TREND,
    IntradayEngine,
    IntradaySetup,
    intraday_management,
    intraday_plan,
)
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.storage.db import Database

SIGNAL_TF = Timeframe.M5


@dataclass
class IntradayData:
    """Everything the trade pass needs, computed once."""

    m1: pd.DataFrame
    spec: SymbolSpec
    setups: list[IntradaySetup]
    signal_close_ns: np.ndarray  # M5 bar closes
    vwap: np.ndarray  # the VWAP after each M5 bar
    atr: np.ndarray  # the M5 ATR after each M5 bar
    spread_pts: np.ndarray  # per M1 bar
    median_pts: np.ndarray
    start: dt.datetime
    end: dt.datetime


@dataclass
class RuleRun:
    rule: str
    ladder: tuple[float, ...]
    costs: bool
    trades: list[Trade]
    outcomes: Counter[str]
    rejections: Counter[str]
    metrics: Metrics

    @property
    def name(self) -> str:
        steps = "/".join(f"{s:g}" for s in self.ladder)
        return f"{self.rule}, {steps} R" + ("" if self.costs else ", no costs")


@dataclass
class Verdict:
    rule: str
    chosen: RuleRun
    gross: RuleRun
    first: Metrics  # the chosen ladder on the first two-thirds
    held_out: Metrics  # ... and on the last third
    passed: bool
    reason: str


class _VwapLine:
    """The latest finished M5 VWAP and ATR at an M1 bar's close (for the trailing stop)."""

    def __init__(self, data: IntradayData) -> None:
        self._close_ns, self._vwap, self._atr = data.signal_close_ns, data.vwap, data.atr

    def line_at(self, close_ns: int) -> tuple[float, float]:
        i = int(np.searchsorted(self._close_ns, close_ns, side="right")) - 1
        if i < 0:
            return float("nan"), float("nan")
        return float(self._vwap[i]), float(self._atr[i])


def prepare(
    settings: Settings,
    m1: pd.DataFrame,
    spec: SymbolSpec,
    start: dt.datetime,
    end: dt.datetime,
) -> IntradayData:
    config = settings.config
    m5 = aggregate_candles(m1, SIGNAL_TF)
    engine = IntradayEngine(
        config.scalper, SessionCalculator(config.sessions), bar=SIGNAL_TF.duration, rules=RULES
    )
    times = m5.index.to_pydatetime()
    columns = [m5[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")]
    volume = m5["tick_volume"].to_numpy(dtype="float64")
    vwap = np.full(len(m5), np.nan)
    atr = np.full(len(m5), np.nan)
    setups: list[IntradaySetup] = []
    for i in range(len(m5)):
        setups += engine.on_bar(
            times[i],
            float(columns[0][i]),
            float(columns[1][i]),
            float(columns[2][i]),
            float(columns[3][i]),
            float(volume[i]),
        )
        vwap[i] = engine.vwap
        atr[i] = engine.atr if engine.atr is not None else np.nan
    points = spread_points(m1, config.backtest.costs)
    return IntradayData(
        m1=m1,
        spec=spec,
        setups=setups,
        signal_close_ns=_nanoseconds(m5.index) + int(SIGNAL_TF.duration.total_seconds() * 1e9),
        vwap=vwap,
        atr=atr,
        spread_pts=points,
        median_pts=rolling_median(points, m1.index, config.risk.spread_median_days),
        start=ensure_utc(start),
        end=ensure_utc(end),
    )


def simulate(
    settings: Settings, data: IntradayData, rule: str, ladder: tuple[float, ...], *, costs: bool
) -> RuleRun:
    """Trade ``rule``'s setups on the M1 bars with ``ladder`` (with or without costs)."""
    config = settings.config
    scalper = config.scalper
    costs_cfg = config.backtest.costs
    if not costs:
        costs_cfg = costs_cfg.model_copy(update={"slippage_points": 0.0, "commission_per_lot": 0.0})
    spec, m1 = data.spec, data.m1
    open_ns = _nanoseconds(m1.index)
    close_ns = open_ns + 60 * 10**9
    opens, highs, lows, closes = (
        m1[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")
    )
    spreads = data.spread_pts * spec.point if costs else np.zeros(len(m1))
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
        management=intraday_management(scalper, base.breakeven_offset, trail=rule == VWAP_TREND),
    )
    line = _VwapLine(data) if rule == VWAP_TREND else None
    sessions = SessionCalculator(config.sessions)
    start_ns, end_ns = to_ns(data.start), to_ns(data.end)
    setups = [s for s in data.setups if s.rule == rule and start_ns <= to_ns(s.time) < end_ns]
    # Each setup is decided at the close of the M1 bar that ends its M5 bar.
    at = [max(0, int(np.searchsorted(close_ns, to_ns(s.time), side="right")) - 1) for s in setups]
    trades: list[Trade] = []
    outcomes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    count = len(m1)
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
        for event in book.on_bar(bar, line=line):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
        while j < len(setups) and at[j] <= i:
            setup = setups[j]
            j += 1
            if at[j - 1] < i:
                outcomes["busy"] += 1
                continue
            if not book.idle:
                outcomes["busy"] += 1
                continue
            if sessions.status(setup.time).blocked:  # weekend, rollover, Friday cutoff
                outcomes["blocked_time"] += 1
                continue
            number += 1
            here = (
                LadderCosts.of(spec, costs_cfg, float(data.spread_pts[i]))
                if costs
                else LadderCosts(0.0, 0.0, 0.0)
            )
            made = intraday_plan(
                setup,
                scalper.intraday,
                ladder,
                here,
                cost_multiple=scalper.risk.cost_multiple,
                plan_id=f"ID-{number:05d}",
                bar=dt.timedelta(minutes=1),
            )
            if made.plan is None:
                outcomes["no_plan"] += 1
                kind = (
                    "stop outside the ATR range" if made.reason.startswith("stop") else made.reason
                )
                rejections[kind] += 1
                continue
            event = book.submit(
                made.plan,
                setup,
                spread_points=float(data.spread_pts[i]) if costs else 0.0,
                median_spread_points=float(data.median_pts[i]) if costs else None,
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
            m1.index[last].to_pydatetime() + dt.timedelta(minutes=1),
            quote,
            quote + float(spreads[last]),
            ExitReason.END_OF_DATA,
            "end of data",
        ):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
    balance = config.backtest.initial_balance
    return RuleRun(rule, ladder, costs, trades, outcomes, rejections, summarize(trades, balance))


def judge(settings: Settings, data: IntradayData, rule: str) -> tuple[Verdict, list[RuleRun]]:
    """Choose a ladder on the first two-thirds; pass only if the held-out third is positive."""
    intraday = settings.config.scalper.intraday
    balance = settings.config.backtest.initial_balance
    split = data.start + (data.end - data.start) * 2 / 3
    runs = [simulate(settings, data, rule, ladder, costs=True) for ladder in intraday.ladders]

    def part(run: RuleRun, first: bool) -> Metrics:
        chosen = [t for t in run.trades if (t.fill_time < split) == first]
        return summarize(chosen, balance)

    best = max(runs, key=lambda run: (part(run, True).expectancy_r or -99.0, -len(run.ladder)))
    gross = simulate(settings, data, rule, best.ladder, costs=False)
    first, held_out = part(best, True), part(best, False)
    reasons = []
    if best.metrics.trades < intraday.min_trades:
        reasons.append(f"{best.metrics.trades} trades, fewer than {intraday.min_trades}")
    if (first.expectancy_r or 0.0) <= 0:
        reasons.append("negative on the first two-thirds")
    if (held_out.expectancy_r or 0.0) <= 0:
        reasons.append("negative on the held-out third")
    if (best.metrics.expectancy_r or 0.0) <= 0:
        reasons.append("negative after costs overall")
    verdict = Verdict(
        rule=rule,
        chosen=best,
        gross=gross,
        first=first,
        held_out=held_out,
        passed=not reasons,
        reason="; ".join(reasons) or "passed",
    )
    return verdict, runs


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _line(name: str, m: Metrics) -> str:
    won = "n/a" if m.win_rate is None else f"{m.win_rate * 100:.1f}%"
    pf = "n/a" if m.profit_factor is None else f"{m.profit_factor:.2f}"
    return (
        f"{name:<44} {m.trades:>6} {won:>7} {pf:>6} {_r(m.expectancy_r):>7} "
        f"{m.total_r:>+8.1f} {m.return_pct:>+8.1f}% {m.max_drawdown_pct:>6.1f}%"
    )


HEADER = (
    f"{'rule, ladder':<44} {'trades':>6} {'won':>7} {'PF':>6} {'R/trade':>7} "
    f"{'total R':>8} {'return':>9} {'max DD':>7}"
)


def _metrics_json(m: Metrics) -> dict[str, object]:
    return {
        key: (round(value, 6) if isinstance(value, float) else value)
        for key, value in m.as_dict().items()
    }


def write_report(
    folder: Path, verdicts: list[Verdict], runs: list[RuleRun], *, command: str, period: str
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "command": command,
        "period": period,
        "variants": [
            {
                "variant": run.name,
                "metrics": _metrics_json(run.metrics),
                "outcomes": dict(sorted(run.outcomes.items())),
                "no_plan": dict(sorted(run.rejections.items())),
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
            "Bid bars only: inside a one-minute bar the worse case is assumed.",
            "The VWAP is weighted by tick volume (spot gold has no exchange volume).",
            "Market entries fill at the next bar's open plus slippage.",
        ],
        "disclaimer": DISCLAIMER,
    }
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    with (folder / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("rule", "id", "direction", "session", "fill_time", "fill_price", "stop_loss",
             "exit_reason", "exit_time", "net_pnl", "r_multiple")
        )  # fmt: skip
        for verdict in verdicts:
            for t in verdict.chosen.trades:
                writer.writerow(
                    (
                        verdict.rule,
                        t.plan.id,
                        t.plan.direction.value,
                        t.plan.session,
                        f"{t.fill_time:%Y-%m-%d %H:%M}",
                        f"{t.fill_price:.3f}",
                        f"{t.stop_loss:.3f}",
                        t.exit_reason.value,
                        f"{t.exit_time:%Y-%m-%d %H:%M}",
                        f"{t.net_pnl:.2f}",
                        f"{t.r_multiple:.3f}",
                    )
                )
    return folder / "summary.json"


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m goldsweep.scalping.intraday_backtest")
    parser.add_argument("--from", dest="start", type=_day, required=True)
    parser.add_argument("--to", dest="end", type=_day, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    settings = load_settings(config_path=args.config)
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    warmup = dt.timedelta(days=settings.config.backtest.warmup_days)
    with Database(settings.db_path) as db:
        spec = load_symbol_spec(db)
        if spec is None:
            print("No stored history: run the main backtest once to download it.", file=out)
            return 1
        m1 = db.load_candles(spec.name, Timeframe.M1, start=start - warmup, end=end)
    if m1.empty:
        print("No stored M1 bars for that period.", file=out)
        return 1
    print(f"Replaying {len(m1):,} M1 bars (as M5 signals) through the intraday rules...", file=out)
    data = prepare(settings, m1, spec, start, end)
    verdicts: list[Verdict] = []
    runs: list[RuleRun] = []
    for rule in settings.config.scalper.intraday.rules:
        verdict, tried = judge(settings, data, rule)
        verdicts.append(verdict)
        runs += tried
    period = f"{args.start:%Y-%m-%d} .. {args.end:%Y-%m-%d}"
    folder = args.out or settings.config_path.parent / "reports" / (
        f"intraday_{args.start:%Y-%m-%d}_{args.end:%Y-%m-%d}"
    )
    command = "python -m goldsweep.scalping.intraday_backtest " + " ".join(arguments)
    path = write_report(folder, verdicts, runs, command=command, period=period)
    counts = Counter(s.rule for s in data.setups)
    print(f"GoldSweep intraday mode {spec.name} {period}", file=out)
    print("Setups: " + ", ".join(f"{rule} {counts.get(rule, 0)}" for rule in RULES), file=out)
    print(HEADER, file=out)
    for run in runs:
        print(_line(run.name, run.metrics), file=out)
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

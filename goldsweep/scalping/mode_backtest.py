"""A scalper mode's backtest through the live logic (S6, D112): reported, not a gate.

``python -m goldsweep.scalping.mode_backtest --mode intraday --from 2024-01-01 --to 2026-10-31``

The stored M1 bars are replayed one at a time through the same objects the bot runs
(:mod:`~goldsweep.scalping.logic`: the mode builds its own M5/M15 bars and H1 trend),
in the runner's order: the bar goes to the mode, then to the trade book (fills,
stops, targets and the mode's management rules on M1), then the mode's candidates on
that bar meet the runner's gates (one scalp at a time, the mode's own filters, the
cooldown after a stop-out, the stop and cost gate, the book's risk checks). The run is
made with costs and again without them.

The owner judges every mode on paper and on the demo account (D111); this report is
what those results are compared with. Its limits: no historical calendar (no news
blackout), bid bars only with the worse case assumed inside a bar, market entries at
the next bar's open plus slippage, and no account guard (the main strategy is not
replayed). Report files carry no run timestamps.
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

from goldsweep.analysis.structure import Direction
from goldsweep.backtest.data import load_symbol_spec
from goldsweep.backtest.engine import rolling_median, spread_points
from goldsweep.backtest.metrics import Metrics, breakdown, summarize
from goldsweep.core.clock import SessionCalculator, ensure_utc
from goldsweep.core.settings import Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import SymbolSpec
from goldsweep.execution.book import Bar, EventKind, ExitReason, Trade, TradeBook, to_ns
from goldsweep.scalping.backtest import DISCLAIMER, _nanoseconds
from goldsweep.scalping.logic import LOGICS, Candidate, make_logic
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.storage.db import Database

LIMITATIONS = (
    "No historical economic calendar: no news blackout was applied.",
    "Bid bars only: inside a one-minute bar the worse case is assumed.",
    "Market entries fill at the next bar's open plus slippage.",
    "No account guard: the main strategy is not replayed beside the scalper.",
)


@dataclass
class ModeRun:
    mode: str
    costs: bool
    trades: list[Trade]
    outcomes: Counter[str]
    rejections: Counter[str]
    metrics: Metrics
    days: int

    @property
    def per_day(self) -> float:
        return len(self.trades) / self.days if self.days else 0.0


def simulate(
    settings: Settings,
    m1: pd.DataFrame,
    spec: SymbolSpec,
    mode: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    costs: bool = True,
) -> ModeRun:
    """Replay ``m1`` through ``mode`` as the runner would; trades opened in [start, end)."""
    config = settings.config
    costs_cfg = config.backtest.costs
    if not costs:
        costs_cfg = costs_cfg.model_copy(update={"slippage_points": 0.0, "commission_per_lot": 0.0})
    sessions = SessionCalculator(config.sessions)
    base = LadderCosts.of(spec, costs_cfg, 0.0)
    logic = make_logic(mode, config, sessions, base.breakeven_offset)
    balance = config.backtest.initial_balance
    book = TradeBook(
        spec,
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=balance,
        slippage_points=costs_cfg.slippage_points,
        commission_per_lot=costs_cfg.commission_per_lot,
        trail_after_tp1=False,
        management=logic.management,
    )
    points = spread_points(m1, costs_cfg)
    median = rolling_median(points, m1.index, config.risk.spread_median_days)
    spreads = points * spec.point if costs else np.zeros(len(m1))
    open_ns = _nanoseconds(m1.index)
    times = m1.index.to_pydatetime()
    opens, highs, lows, closes = (
        m1[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")
    )
    volumes = (
        m1["tick_volume"].to_numpy(dtype="float64") if "tick_volume" in m1 else np.ones(len(m1))
    )
    start_ns, end_ns = to_ns(ensure_utc(start)), to_ns(ensure_utc(end))
    cooldown = dt.timedelta(seconds=config.scalper.risk.cooldown_sec)
    stopped: dict[Direction, dt.datetime] = {}
    trades: list[Trade] = []
    outcomes: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    number = 0
    line = logic.line
    for i in range(len(m1)):
        opened = ensure_utc(times[i])
        o, h, low, c = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i])
        candidates: list[Candidate] = logic.on_bar(opened, o, h, low, c, float(volumes[i]))
        if not book.idle:
            bar = Bar(
                int(open_ns[i]), int(open_ns[i]) + 60 * 10**9, o, h, low, c, float(spreads[i])
            )
            for event in book.on_bar(bar, line=line):
                if event.kind is EventKind.CLOSED and event.trade is not None:
                    trades.append(event.trade)
                    if event.trade.exit_reason is ExitReason.STOP:
                        stopped[event.trade.direction] = event.trade.exit_time
        for candidate in candidates:
            if not start_ns <= to_ns(candidate.time) < end_ns:
                continue
            if not book.idle:
                outcomes["busy"] += 1
                continue
            gate = logic.gate(candidate)
            if not gate.passed:
                outcomes[gate.reason or "gate"] += 1
                continue
            last = stopped.get(candidate.direction)
            if last is not None and candidate.time - last < cooldown:
                outcomes["cooldown"] += 1
                continue
            here = (
                LadderCosts.of(spec, costs_cfg, float(points[i]))
                if costs
                else LadderCosts(0.0, 0.0, 0.0)
            )
            number += 1
            made = logic.plan(
                candidate, here, plan_id=f"{logic.prefix}-{number:05d}", session=gate.session
            )
            if made.plan is None:
                outcomes["no_plan"] += 1
                rejections[made.reason.split(" (")[0]] += 1
                continue
            event = book.submit(
                made.plan,
                candidate,
                spread_points=float(points[i]) if costs else 0.0,
                median_spread_points=float(median[i]) if costs else None,
            )
            if event.kind is EventKind.PLACED:
                outcomes["traded"] += 1
                break  # one scalp at a time
            outcomes[event.outcome.value if event.outcome else "rejected"] += 1
    if not book.idle:
        last_bar = len(m1) - 1
        quote = float(closes[last_bar])
        for event in book.close_everything(
            ensure_utc(times[last_bar]) + dt.timedelta(minutes=1),
            quote,
            quote + float(spreads[last_bar]),
            ExitReason.END_OF_DATA,
            "end of data",
        ):
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trades.append(event.trade)
    in_period = m1.index[(m1.index >= pd.Timestamp(start)) & (m1.index < pd.Timestamp(end))]
    days = len(set(in_period.date))
    return ModeRun(mode, costs, trades, outcomes, rejections, summarize(trades, balance), days)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _metrics_json(m: Metrics) -> dict[str, object]:
    return {
        key: (round(value, 6) if isinstance(value, float) else value)
        for key, value in m.as_dict().items()
    }


def _rule(trade: Trade) -> str:
    return str(getattr(trade.payload, "rule", "?"))


def breakdowns(run: ModeRun, balance: float) -> dict[str, dict[str, dict[str, object]]]:
    keys = {
        "rule": _rule,
        "session": lambda t: t.plan.session or "none",
        "direction": lambda t: t.plan.direction.value,
        "exit": lambda t: t.exit_reason.value,
        "year": lambda t: str(t.fill_time.year),
    }
    return {
        name: {
            str(group): {
                "trades": m.trades,
                "win_rate": m.win_rate,
                "expectancy_r": m.expectancy_r,
                "total_r": m.total_r,
            }
            for group, m in breakdown(run.trades, key, balance).items()
        }
        for name, key in keys.items()
    }


def halves(
    run: ModeRun, start: dt.datetime, end: dt.datetime, balance: float
) -> tuple[Metrics, Metrics]:
    """The first two-thirds of the period and the last third (reported, not a gate)."""
    split = start + (end - start) * 2 / 3
    first = [t for t in run.trades if t.fill_time < split]
    last = [t for t in run.trades if t.fill_time >= split]
    return summarize(first, balance), summarize(last, balance)


def line(name: str, run: ModeRun) -> str:
    m = run.metrics
    won = "n/a" if m.win_rate is None else f"{m.win_rate * 100:.1f}%"
    pf = "n/a" if m.profit_factor is None else f"{m.profit_factor:.2f}"
    return (
        f"{name:<26} {m.trades:>6} {run.per_day:>6.2f} {won:>7} {pf:>6} {_r(m.expectancy_r):>7} "
        f"{m.total_r:>+9.1f} {m.return_pct:>+8.1f}% {m.max_drawdown_pct:>6.1f}%"
    )


HEADER = (
    f"{'run':<26} {'trades':>6} {'a day':>6} {'won':>7} {'PF':>6} {'R/trade':>7} "
    f"{'total R':>9} {'return':>9} {'max DD':>7}"
)


def write_report(
    folder: Path,
    net: ModeRun,
    gross: ModeRun,
    *,
    command: str,
    period: tuple[str, str],
    start: dt.datetime,
    end: dt.datetime,
    balance: float,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    first, last = halves(net, start, end, balance)
    summary = {
        "command": command,
        "mode": net.mode,
        "period": {"from": period[0], "to": period[1]},
        "with_costs": _metrics_json(net.metrics),
        "without_costs": _metrics_json(gross.metrics),
        "per_day": round(net.per_day, 4),
        "first_two_thirds": _metrics_json(first),
        "last_third": _metrics_json(last),
        "outcomes": dict(sorted(net.outcomes.items())),
        "no_plan": dict(sorted(net.rejections.items())),
        "breakdowns": breakdowns(net, balance),
        "limitations": list(LIMITATIONS),
        "disclaimer": DISCLAIMER,
    }
    path = folder / "summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8")
    with (folder / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ("id", "rule", "direction", "session", "fill_time", "fill_price", "stop_loss",
             "tp1", "tp2", "tp3", "lots", "exit_reason", "exit_time", "swap", "net_pnl",
             "r_multiple")
        )  # fmt: skip
        for t in net.trades:
            writer.writerow(
                (
                    t.plan.id,
                    _rule(t),
                    t.plan.direction.value,
                    t.plan.session,
                    f"{t.fill_time:%Y-%m-%d %H:%M}",
                    f"{t.fill_price:.3f}",
                    f"{t.stop_loss:.3f}",
                    f"{t.take_profit_1:.3f}",
                    "" if t.take_profit_2 is None else f"{t.take_profit_2:.3f}",
                    "" if t.take_profit_3 is None else f"{t.take_profit_3:.3f}",
                    f"{t.lots:g}",
                    t.exit_reason.value,
                    f"{t.exit_time:%Y-%m-%d %H:%M}",
                    f"{getattr(t, 'swap', 0.0):.2f}",
                    f"{t.net_pnl:.2f}",
                    f"{t.r_multiple:.3f}",
                )
            )
    return path


def _day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m goldsweep.scalping.mode_backtest")
    parser.add_argument("--mode", choices=sorted(LOGICS), required=True)
    parser.add_argument("--from", dest="start", type=_day, required=True)
    parser.add_argument("--to", dest="end", type=_day, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--out", type=Path)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(arguments)
    settings = load_settings(config_path=args.config)
    config = settings.config
    start = dt.datetime.combine(args.start, dt.time(0), tzinfo=dt.UTC)
    end = dt.datetime.combine(args.end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC)
    warmup = dt.timedelta(days=max(config.backtest.warmup_days, 7))
    with Database(settings.db_path) as db:
        spec = load_symbol_spec(db)
        if spec is None:
            print("No stored history: run the main backtest once to download it.", file=out)
            return 1
        m1 = db.load_candles(spec.name, Timeframe.M1, start=start - warmup, end=end)
    if m1.empty:
        print("No stored M1 bars for that period.", file=out)
        return 1
    print(
        f"Replaying {len(m1):,} M1 bars through the {args.mode} mode (with and without costs)...",
        file=out,
        flush=True,
    )
    net = simulate(settings, m1, spec, args.mode, start, end, costs=True)
    gross = simulate(settings, m1, spec, args.mode, start, end, costs=False)
    period = (f"{args.start:%Y-%m-%d}", f"{args.end:%Y-%m-%d}")
    folder = args.out or settings.config_path.parent / "reports" / (
        f"scalper_{args.mode}_{period[0]}_{period[1]}"
    )
    command = "python -m goldsweep.scalping.mode_backtest " + " ".join(arguments)
    balance = config.backtest.initial_balance
    path = write_report(
        folder, net, gross, command=command, period=period, start=start, end=end, balance=balance
    )
    first, last = halves(net, start, end, balance)
    print(f"GoldSweep scalper {args.mode} mode {spec.name} {period[0]} .. {period[1]}", file=out)
    print(HEADER, file=out)
    print(line("with costs", net), file=out)
    print(line("without costs", gross), file=out)
    print(
        f"First two-thirds {first.trades} trades {_r(first.expectancy_r)} R; last third "
        f"{last.trades} trades {_r(last.expectancy_r)} R",
        file=out,
    )
    for name, groups in breakdowns(net, balance).items():
        text = " | ".join(
            f"{key} {value['trades']} ({_r(value['expectancy_r'])} R)"  # type: ignore[arg-type]
            for key, value in groups.items()
        )
        print(f"  by {name}: {text}", file=out)
    print(f"  outcomes: {dict(sorted(net.outcomes.items()))}", file=out)
    print(f"  no plan: {dict(sorted(net.rejections.items()))}", file=out)
    print(f"Report: {path}", file=out)
    print("Reported, not a gate (D111): paper and the demo account judge the mode.", file=out)
    print(DISCLAIMER, file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

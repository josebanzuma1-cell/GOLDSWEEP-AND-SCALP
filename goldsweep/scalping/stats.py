"""The scalper's results per mode, on paper and on the demo account (S7, D113).

One source for Telegram ``/stats``, the dashboard's Scalper tab and the chart panel.
Results are after costs; each mode is shown next to its backtest's R per trade
(D111, D112), the figure its paper and demo results are judged against. Past results
do not predict future ones.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from goldsweep.core.clock import ensure_utc
from goldsweep.execution.executor import SCALP_LIVE_TABLE, SCALP_PAPER_TABLE
from goldsweep.storage.db import Database

#: Plan-id prefixes and their modes (``<date>-<prefix>-<n>``).
PREFIX_MODES = {"HFT": "hft", "INT": "intraday", "SWS": "swing"}
MODE_ORDER = ("hft", "intraday", "swing")
MODE_LABELS = {"hft": "HFT", "intraday": "intraday", "swing": "swing scalping"}
#: R a trade after costs in the 2024-01-01..2026-10-31 backtests (D111, D112).
BACKTEST_R = {"hft": -0.12, "intraday": -0.04, "swing": -0.07}
TABLES = {"paper": SCALP_PAPER_TABLE, "demo": SCALP_LIVE_TABLE}
DISCLAIMER = "Past results do not predict future ones; every trade can lose."


def mode_of(scalp_id: str) -> str:
    """``2026-11-22-INT-004`` -> ``intraday`` (``?`` for an id of no mode)."""
    parts = scalp_id.split("-")
    return PREFIX_MODES.get(parts[3], "?") if len(parts) >= 5 else "?"


@dataclass(frozen=True, slots=True)
class ClosedScalp:
    """A finished scalp, as stored."""

    id: str
    mode: str
    where: str  # paper / demo
    direction: str
    session: str | None
    signal_time: dt.datetime
    fill_time: dt.datetime
    exit_time: dt.datetime
    fill_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    lots: float
    exit_reason: str
    net_pnl: float
    r_multiple: float
    legs: tuple[dict[str, Any], ...]

    @property
    def take_profit_3(self) -> float | None:
        """Every scalper ladder has equal steps: TP3 is one more step past TP2."""
        return self.target(3)

    @property
    def parts(self) -> int:
        """How many parts (and targets) the scalp had: one per stored leg (up to five)."""
        return max(1, min(5, len(self.legs))) if self.legs else 3

    def target(self, number: int) -> float | None:
        """TP ``number`` (1-5) of the equal-step ladder, or None past the last part."""
        if number == 1:
            return self.take_profit_1
        if self.take_profit_2 is None or number > max(self.parts, 3):
            return None
        return self.take_profit_1 + (number - 1) * (self.take_profit_2 - self.take_profit_1)


def _utc(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(epoch), tz=dt.UTC)


def closed_scalps(
    db: Database, since: dt.datetime, until: dt.datetime | None = None
) -> list[ClosedScalp]:
    """Scalps of every mode closed in ``[since, until)``, oldest first."""
    start = int(ensure_utc(since).timestamp())
    end = int(ensure_utc(until).timestamp()) if until is not None else 2**62
    found: list[ClosedScalp] = []
    for where, table in TABLES.items():
        rows = db.query(
            "SELECT id, direction, session, signal_time, fill_time, exit_time, fill_price, "
            "stop_loss, take_profit_1, take_profit_2, lots, exit_reason, net_pnl, r_multiple, "
            f"legs FROM {table} WHERE exit_time >= ? AND exit_time < ?",  # noqa: S608
            (start, end),
        )
        for r in rows:
            found.append(
                ClosedScalp(
                    id=r[0],
                    mode=mode_of(r[0]),
                    where=where,
                    direction=r[1],
                    session=r[2],
                    signal_time=_utc(r[3]),
                    fill_time=_utc(r[4]),
                    exit_time=_utc(r[5]),
                    fill_price=float(r[6]),
                    stop_loss=float(r[7]),
                    take_profit_1=float(r[8]),
                    take_profit_2=None if r[9] is None else float(r[9]),
                    lots=float(r[10]),
                    exit_reason=r[11],
                    net_pnl=float(r[12]),
                    r_multiple=float(r[13]),
                    legs=tuple(json.loads(r[14] or "[]")),
                )
            )
    return sorted(found, key=lambda s: (s.exit_time, s.id))


class _Result(Protocol):
    @property
    def net_pnl(self) -> float: ...

    @property
    def r_multiple(self) -> float: ...


@dataclass(frozen=True, slots=True)
class Stats:
    """Results of a group of trades, after costs."""

    label: str
    trades: int
    wins: int
    losses: int
    net: float
    total_r: float
    win_rate: float | None
    #: The win rate at which the average win and the average loss cancel out.
    breakeven_win_rate: float | None
    profit_factor: float | None
    expectancy_r: float | None
    backtest_r: float | None = None  # the mode's backtest, R a trade


def summarize(label: str, trades: Iterable[_Result], backtest_r: float | None = None) -> Stats:
    results = list(trades)
    count = len(results)
    if not count:
        return Stats(label, 0, 0, 0, 0.0, 0.0, None, None, None, None, backtest_r)
    r = [t.r_multiple for t in results]
    pnl = [t.net_pnl for t in results]
    won = [v for v in r if v > 0]
    lost = [v for v in r if v < 0]
    breakeven = None
    if won and lost:
        average_win, average_loss = sum(won) / len(won), sum(lost) / len(lost)
        breakeven = -average_loss / (average_win - average_loss)
    gross_win = sum(v for v in pnl if v > 0)
    gross_loss = -sum(v for v in pnl if v < 0)
    return Stats(
        label=label,
        trades=count,
        wins=len(won),
        losses=len(lost),
        net=sum(pnl),
        total_r=sum(r),
        win_rate=len(won) / count,
        breakeven_win_rate=breakeven,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else None,
        expectancy_r=sum(r) / count,
        backtest_r=backtest_r,
    )


def by_mode(scalps: Sequence[ClosedScalp]) -> list[Stats]:
    """One line per mode and account that has scalps: hft, intraday, swing; paper, demo."""
    found: list[Stats] = []
    for mode in MODE_ORDER:
        for where in TABLES:
            group = [s for s in scalps if s.mode == mode and s.where == where]
            if group:
                label = f"{MODE_LABELS[mode]}, {where}"
                found.append(summarize(label, group, BACKTEST_R[mode]))
    return found


def _money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def stats_line(stats: Stats, noun: str = "scalps") -> str:
    """``HFT, paper: 12 scalps, won 58% (break-even 65%), PF 0.71, -1.40 R (-0.12 R each;
    backtest -0.12), -$35.00``."""
    if not stats.trades:
        return f"{stats.label}: no {noun}"
    pf = "n/a" if stats.profit_factor is None else f"{stats.profit_factor:.2f}"
    each = f"{stats.expectancy_r:+.2f} R each" if stats.expectancy_r is not None else ""
    if stats.backtest_r is not None:
        each += f"; backtest {stats.backtest_r:+.2f}"
    return (
        f"{stats.label}: {stats.trades} {noun}, won {_pct(stats.win_rate)} (break-even "
        f"{_pct(stats.breakeven_win_rate)}), PF {pf}, {stats.total_r:+.2f} R ({each}), "
        f"{_money(stats.net)}"
    )


def stats_message(days: int, scalpers: Sequence[Stats], main: Stats | None) -> str:
    """The ``/stats`` reply."""
    lines = [f"📈 Results, last {days} day{'s' if days != 1 else ''} (after costs)"]
    if scalpers:
        lines += [f"Scalper {stats_line(s)}" for s in scalpers]
    else:
        lines.append("Scalper: no scalps closed in this period.")
    if main is not None:
        lines.append(stats_line(main, "trades"))
    lines.append(DISCLAIMER)
    return "\n".join(lines)

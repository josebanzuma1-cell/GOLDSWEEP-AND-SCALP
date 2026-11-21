"""Performance statistics of a list of trades (spec 9.3).

All figures are *net*: after spread, slippage and commission. R is the result
divided by the amount risked when the entry filled, so +2.0 R means twice the
risk was won and -1.0 R means the full stop was lost.

These numbers describe the past, on the data and costs given. They are not a
forecast; past expectancy can and does change.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import asdict, dataclass

import numpy as np

from goldsweep.execution.book import Trade

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    win_rate: float | None  # share of trades with a net profit, 0..1
    profit_factor: float | None  # gross profit / gross loss; None without losses or trades
    expectancy_r: float | None  # average R per trade
    total_r: float
    average_win_r: float | None
    average_loss_r: float | None
    average_planned_rr: float | None  # the signals' R:R to TP1
    net_profit: float  # account currency
    return_pct: float  # net profit against the starting balance
    max_drawdown: float  # account currency, peak to trough of closed-trade equity
    max_drawdown_pct: float  # of the peak
    longest_losing_streak: int
    average_hold_minutes: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def _mean(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if len(values) else None


def equity_points(
    trades: Sequence[Trade], initial_balance: float, start: dt.datetime | None = None
) -> list[tuple[dt.datetime, float]]:
    """Closed-trade equity after each trade, in exit order (with the start point first)."""
    ordered = sorted(trades, key=lambda trade: trade.exit_time)
    points: list[tuple[dt.datetime, float]] = []
    if start is not None:
        points.append((start, initial_balance))
    equity = initial_balance
    for trade in ordered:
        equity += trade.net_pnl
        points.append((trade.exit_time, equity))
    return points


def drawdown(values: Sequence[float]) -> tuple[float, float]:
    """Largest peak-to-trough fall: (amount, fraction of the peak)."""
    if not len(values):
        return 0.0, 0.0
    series = np.asarray(values, dtype="float64")
    peaks = np.maximum.accumulate(series)
    falls = peaks - series
    worst = int(np.argmax(falls))
    amount = float(falls[worst])
    return amount, (float(amount / peaks[worst]) if peaks[worst] > 0 else 0.0)


def summarize(trades: Sequence[Trade], initial_balance: float) -> Metrics:
    """Every headline statistic for ``trades``."""
    pnl = [trade.net_pnl for trade in trades]
    r = [trade.r_multiple for trade in trades]
    wins = [value for value in r if value > 0]
    losses = [value for value in r if value < 0]
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value < 0)
    streak = longest = 0
    for trade in sorted(trades, key=lambda t: t.exit_time):
        streak = streak + 1 if trade.net_pnl < 0 else 0
        longest = max(longest, streak)
    equity = [value for _time, value in equity_points(trades, initial_balance)]
    amount, fraction = drawdown([initial_balance, *equity])
    holds = [(trade.exit_time - trade.fill_time).total_seconds() / 60.0 for trade in trades]
    net = float(sum(pnl))
    return Metrics(
        trades=len(trades),
        wins=sum(1 for value in pnl if value > 0),
        losses=sum(1 for value in pnl if value < 0),
        win_rate=(sum(1 for value in pnl if value > 0) / len(pnl)) if pnl else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        expectancy_r=_mean(r),
        total_r=float(sum(r)),
        average_win_r=_mean(wins),
        average_loss_r=_mean(losses),
        average_planned_rr=_mean([trade.plan.risk_reward_1 for trade in trades]),
        net_profit=net,
        return_pct=net / initial_balance * 100.0,
        max_drawdown=amount,
        max_drawdown_pct=fraction * 100.0,
        longest_losing_streak=longest,
        average_hold_minutes=_mean(holds),
    )


def breakdown(
    trades: Iterable[Trade],
    key: Callable[[Trade], Hashable],
    initial_balance: float,
    order: Sequence[Hashable] | None = None,
) -> dict[Hashable, Metrics]:
    """``summarize`` per group; groups in ``order`` first, then any others."""
    groups: dict[Hashable, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)
    keys = [k for k in (order or ()) if k in groups] + [
        k for k in groups if order is None or k not in order
    ]
    return {k: summarize(groups[k], initial_balance) for k in keys}


def by_session(trade: Trade) -> str:
    return trade.plan.session or "none"


def by_weekday(trade: Trade) -> str:
    return WEEKDAYS[trade.plan.time.weekday()]


def by_direction(trade: Trade) -> str:
    return trade.direction.value


def by_pressure(trade: Trade) -> str:
    """Did the buyer/seller meter lean the same way as the trade when it was signalled?"""
    reading = getattr(trade.record, "pressure", None)
    if reading is None:
        return "no reading"
    agrees = reading.agrees_with(trade.direction)
    if agrees is None:
        return "meter balanced"
    return "meter agreed" if agrees else "meter disagreed"

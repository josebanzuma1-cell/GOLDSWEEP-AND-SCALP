"""Position sizing and pre-trade limits (spec section 6).

The same rules apply in the backtester now and to paper and live trading later,
so a backtest trades the way the bot will. Everything here is plain state plus
arithmetic: the caller passes the time, nothing reads a clock.

Limits checked before every new trade:

* open positions (a pending entry order counts as one) below ``max_open_positions``
* trades this trading day below ``max_trades_per_day``, this session below
  ``max_trades_per_session``
* realised loss today above ``-daily_loss_limit_pct`` of the day's opening equity,
  this week above ``-weekly_loss_limit_pct`` of the week's opening equity
* not paused after ``max_consecutive_losses`` losses in a row (24 hours)
* spread no more than ``max_spread_multiple`` times its usual (median) value
* no high-impact news event within the blackout window

Sizing never rounds up: a stop too wide for the minimum lot at the configured
risk means no trade, not a bigger risk.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from goldsweep.analysis.periods import trading_day_of
from goldsweep.core.clock import ensure_utc

if TYPE_CHECKING:
    from goldsweep.core.settings import DayBoundaryConfig, NewsConfig, RiskConfig
    from goldsweep.data.broker import SymbolSpec

PAUSE_AFTER_LOSSES = dt.timedelta(hours=24)
HIGH_IMPACT = "high"
_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class NewsEvent:
    """A scheduled economic release, as the risk rules see it."""

    time: dt.datetime  # UTC
    title: str
    impact: str = HIGH_IMPACT


@dataclass(frozen=True, slots=True)
class PositionSize:
    """Lots for one trade, and what they risk."""

    lots: float
    risk_amount: float  # account currency lost if the stop is hit (before costs)
    reason: str | None = None  # why no size could be given, when lots == 0

    @property
    def ok(self) -> bool:
        return self.lots > 0


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()


def position_size(
    equity: float, entry: float, stop: float, spec: SymbolSpec, risk_pct: float
) -> PositionSize:
    """Lots that lose ``risk_pct`` of ``equity`` if the stop is hit, rounded *down*.

    The loss per lot comes from the broker's tick value and tick size, never from
    an assumed contract size.
    """
    distance = abs(entry - stop)
    if distance <= 0:
        return PositionSize(0.0, 0.0, "the stop-loss is at the entry price")
    if equity <= 0:
        return PositionSize(0.0, 0.0, "no equity left")
    loss_per_lot = distance * spec.value_per_price_unit
    budget = equity * risk_pct / 100.0
    step = spec.volume_step
    steps = math.floor(budget / loss_per_lot / step + _TOLERANCE)
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    lots = round(min(steps * step, spec.volume_max), decimals)
    if lots < spec.volume_min - _TOLERANCE:
        needed = spec.volume_min * loss_per_lot / equity * 100.0
        return PositionSize(
            0.0,
            0.0,
            f"the stop is too wide: the minimum {spec.volume_min:g} lots would risk "
            f"{needed:.2f}% of equity, above the {risk_pct:g}% allowed",
        )
    return PositionSize(lots, lots * loss_per_lot)


def blackout_event(
    now: dt.datetime, events: Sequence[NewsEvent], *, before_min: int, after_min: int
) -> NewsEvent | None:
    """The high-impact event whose blackout window contains ``now``, if any."""
    instant = ensure_utc(now)
    for event in events:
        if event.impact != HIGH_IMPACT:
            continue
        start = event.time - dt.timedelta(minutes=before_min)
        end = event.time + dt.timedelta(minutes=after_min)
        if start <= instant <= end:
            return event
    return None


class RiskManager:
    """Tracks equity, open positions and realised results; answers "may I trade now?"."""

    def __init__(
        self,
        risk: RiskConfig,
        news: NewsConfig,
        boundary: DayBoundaryConfig,
        *,
        equity: float,
    ) -> None:
        if equity <= 0:
            raise ValueError("equity must be positive")
        self._risk = risk
        self._news = news
        self._boundary = boundary
        self._equity = float(equity)
        self._open = 0
        self._day_trades: dict[dt.date, int] = {}
        self._session_trades: dict[tuple[dt.date, str], int] = {}
        self._day_pnl: dict[dt.date, float] = {}
        self._week_pnl: dict[dt.date, float] = {}
        self._day_open_equity: dict[dt.date, float] = {}
        self._week_open_equity: dict[dt.date, float] = {}
        self._losses_in_a_row = 0
        self._paused_until: dt.datetime | None = None

    # -- state -------------------------------------------------------------- #

    @property
    def equity(self) -> float:
        """Realised equity: the starting balance plus every booked result."""
        return self._equity

    @property
    def open_positions(self) -> int:
        return self._open

    @property
    def paused_until(self) -> dt.datetime | None:
        return self._paused_until

    def _day(self, moment: dt.datetime) -> dt.date:
        return trading_day_of(moment, self._boundary)

    @staticmethod
    def _week(day: dt.date) -> dt.date:
        return day - dt.timedelta(days=day.weekday())

    def _touch(self, moment: dt.datetime) -> tuple[dt.date, dt.date]:
        """Note the opening equity of the day and week the first time each is seen."""
        day = self._day(moment)
        week = self._week(day)
        self._day_open_equity.setdefault(day, self._equity - self._day_pnl.get(day, 0.0))
        self._week_open_equity.setdefault(week, self._equity - self._week_pnl.get(week, 0.0))
        return day, week

    # -- the check ---------------------------------------------------------- #

    def check(
        self,
        now: dt.datetime,
        *,
        session: str | None,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> RiskDecision:
        """May a new trade be opened at ``now``? Every failing rule is listed."""
        instant = ensure_utc(now)
        risk = self._risk
        day, week = self._touch(instant)
        reasons: list[str] = []
        if self._paused_until is not None and instant < self._paused_until:
            reasons.append(
                f"paused after {risk.max_consecutive_losses} losses in a row until "
                f"{self._paused_until:%Y-%m-%d %H:%M} UTC"
            )
        if self._open >= risk.max_open_positions:
            reasons.append(f"{self._open} position(s) or entry order(s) already open")
        if self._day_trades.get(day, 0) >= risk.max_trades_per_day:
            reasons.append(f"already {risk.max_trades_per_day} trades this trading day")
        if session is not None and (
            self._session_trades.get((day, session), 0) >= risk.max_trades_per_session
        ):
            reasons.append(f"already {risk.max_trades_per_session} trades this {session} session")
        day_limit = -risk.daily_loss_limit_pct / 100.0 * self._day_open_equity[day]
        if self._day_pnl.get(day, 0.0) <= day_limit + _TOLERANCE and day_limit < 0:
            reasons.append(f"daily loss limit of {risk.daily_loss_limit_pct:g}% reached")
        week_limit = -risk.weekly_loss_limit_pct / 100.0 * self._week_open_equity[week]
        if self._week_pnl.get(week, 0.0) <= week_limit + _TOLERANCE and week_limit < 0:
            reasons.append(f"weekly loss limit of {risk.weekly_loss_limit_pct:g}% reached")
        if median_spread_points is not None and median_spread_points > 0:
            ceiling = risk.max_spread_multiple * median_spread_points
            if spread_points > ceiling:
                reasons.append(
                    f"spread {spread_points:g} points is above {ceiling:g} "
                    f"({risk.max_spread_multiple:g}x the usual {median_spread_points:g})"
                )
        event = blackout_event(
            instant,
            events,
            before_min=self._news.blackout_before_min,
            after_min=self._news.blackout_after_min,
        )
        if event is not None:
            reasons.append(f"news blackout: {event.title} at {event.time:%H:%M} UTC")
        return RiskDecision(not reasons, tuple(reasons))

    def rebase(self, equity: float) -> None:
        """Live trading: the broker's balance is the truth for sizing.

        Results already booked today and this week stay counted for the loss limits.
        """
        if equity > 0:
            self._equity = float(equity)

    def size(self, entry: float, stop: float, spec: SymbolSpec) -> PositionSize:
        return position_size(self._equity, entry, stop, spec, self._risk.risk_per_trade_pct)

    # -- events ------------------------------------------------------------- #

    def order_placed(self) -> None:
        """An entry order was sent: it holds a position slot until filled or cancelled."""
        self._open += 1

    def order_cancelled(self) -> None:
        self._open = max(0, self._open - 1)

    def position_opened(self, now: dt.datetime, session: str | None) -> None:
        """The entry filled: it now counts as a trade of the day and the session."""
        day, _week = self._touch(ensure_utc(now))
        self._day_trades[day] = self._day_trades.get(day, 0) + 1
        if session is not None:
            key = (day, session)
            self._session_trades[key] = self._session_trades.get(key, 0) + 1

    def book(self, now: dt.datetime, pnl: float) -> None:
        """A realised result (a full or partial close), after costs."""
        day, week = self._touch(ensure_utc(now))
        self._equity += pnl
        self._day_pnl[day] = self._day_pnl.get(day, 0.0) + pnl
        self._week_pnl[week] = self._week_pnl.get(week, 0.0) + pnl

    def position_closed(self, now: dt.datetime, total_pnl: float) -> None:
        """The whole position is flat. A net loss counts toward the losing streak."""
        self._open = max(0, self._open - 1)
        if total_pnl < 0:
            self._losses_in_a_row += 1
            if self._losses_in_a_row >= self._risk.max_consecutive_losses:
                self._paused_until = ensure_utc(now) + PAUSE_AFTER_LOSSES
                self._losses_in_a_row = 0
        else:
            self._losses_in_a_row = 0

    def status(self, now: dt.datetime) -> dict[str, object]:
        """Today's and this week's figures next to their limits (for status displays)."""
        risk = self._risk
        day = self._day(ensure_utc(now))
        week = self._week(day)
        day_open = self._day_open_equity.get(day, self._equity - self._day_pnl.get(day, 0.0))
        week_open = self._week_open_equity.get(week, self._equity - self._week_pnl.get(week, 0.0))
        paused = self._paused_until
        return {
            "equity": self._equity,
            "open": self._open,
            "max_open_positions": risk.max_open_positions,
            "trades_today": self._day_trades.get(day, 0),
            "max_trades_per_day": risk.max_trades_per_day,
            "day_pnl": self._day_pnl.get(day, 0.0),
            "day_limit": -risk.daily_loss_limit_pct / 100.0 * day_open,
            "week_pnl": self._week_pnl.get(week, 0.0),
            "week_limit": -risk.weekly_loss_limit_pct / 100.0 * week_open,
            "losses_in_a_row": self._losses_in_a_row,
            "max_consecutive_losses": risk.max_consecutive_losses,
            "paused_until": paused.isoformat() if paused and paused > ensure_utc(now) else None,
            "risk_per_trade_pct": risk.risk_per_trade_pct,
        }

    # -- saving and restoring (paper trading survives restarts) ---------------- #

    def to_state(self) -> dict[str, object]:
        """Everything the limits depend on, as JSON-friendly values."""

        def days(values: dict[dt.date, float] | dict[dt.date, int]) -> dict[str, float]:
            return {day.isoformat(): value for day, value in values.items()}

        return {
            "equity": self._equity,
            "open": self._open,
            "day_trades": days(self._day_trades),
            "session_trades": {
                f"{day.isoformat()}|{session}": count
                for (day, session), count in self._session_trades.items()
            },
            "day_pnl": days(self._day_pnl),
            "week_pnl": days(self._week_pnl),
            "day_open_equity": days(self._day_open_equity),
            "week_open_equity": days(self._week_open_equity),
            "losses_in_a_row": self._losses_in_a_row,
            "paused_until": self._paused_until.isoformat() if self._paused_until else None,
        }

    def restore(self, state: dict[str, object]) -> None:
        def days(raw: object) -> dict[dt.date, float]:
            return {dt.date.fromisoformat(k): v for k, v in dict(raw or {}).items()}  # type: ignore[call-overload]

        self._equity = float(state["equity"])  # type: ignore[arg-type]
        self._open = int(state["open"])  # type: ignore[call-overload]
        self._day_trades = {k: int(v) for k, v in days(state["day_trades"]).items()}
        self._session_trades = {}
        for key, count in dict(state["session_trades"] or {}).items():  # type: ignore[call-overload]
            day, _, session = str(key).partition("|")
            self._session_trades[(dt.date.fromisoformat(day), session)] = int(count)
        self._day_pnl = days(state["day_pnl"])
        self._week_pnl = days(state["week_pnl"])
        self._day_open_equity = days(state["day_open_equity"])
        self._week_open_equity = days(state["week_open_equity"])
        self._losses_in_a_row = int(state["losses_in_a_row"])  # type: ignore[call-overload]
        paused = state.get("paused_until")
        self._paused_until = dt.datetime.fromisoformat(str(paused)) if paused else None

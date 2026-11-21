"""The scalper's swing mode (S4, D110): daily trend rules, held for days to weeks.

Fed one closed daily bar at a time (weekend stubs left out). The research behind it
(``docs/SCALPER_MODES.md``): trend following over weeks to months is the best
documented pattern across futures, gold among them, weaker after 2009.

``donchian_breakout``
    A close above the highest high (below the lowest low) of the previous
    ``breakout_days`` days.
``momentum``
    On the first day of each month, the sign of the return over the last
    ``momentum_months`` month-ends decides the side; the position is closed when the
    next month begins (``close_by``) and the rule decides again.
``pullback``
    With price above the ``trend_days`` average and the 50-day above it (or both
    below, for shorts), a close back above (below) the ``pullback_days`` average after
    a close beyond it the other way.

Every setup's first stop is ``stop_atr`` daily ATRs away. Breakouts and pullbacks
then trail the stop behind the last ``exit_days`` days' low (high); it only tightens.
Overnight swap is charged in every test of this mode.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import ScalperConfig, SwingConfig
from goldsweep.execution.book import Management, TradePlan
from goldsweep.scalping.plan import LadderCosts, ScalpPlan
from goldsweep.scalping.trail import RollingAtr

DONCHIAN, MOMENTUM, PULLBACK = "donchian_breakout", "momentum", "pullback"
RULES = (DONCHIAN, MOMENTUM, PULLBACK)
TREND_FAST_DAYS = 50
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class SwingSetup:
    """A swing entry found on a closed daily bar."""

    index: int
    time: dt.datetime  # the day's close (UTC): the order goes in from here
    direction: Direction
    rule: str
    reference: float  # the day's close: the planned (market) entry
    atr: float  # the daily ATR
    close_by: dt.datetime | None  # momentum: the start of the next month
    detail: str


def _average(values: deque[float], count: int) -> float:
    if len(values) < count:
        return math.nan
    return sum(list(values)[-count:]) / count


def _next_month(day: dt.date) -> dt.datetime:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return dt.datetime(year, month, 1, tzinfo=dt.UTC)


class SwingEngine:
    """Feeds closed daily bars to the swing rules; returns their setups."""

    def __init__(self, config: ScalperConfig, *, rules: tuple[str, ...] | None = None) -> None:
        self._cfg: SwingConfig = config.swing
        self._rules = rules if rules is not None else self._cfg.rules
        unknown = set(self._rules) - set(RULES)
        if unknown:
            raise ValueError(f"unknown swing rules: {sorted(unknown)}")
        cfg = self._cfg
        self._atr = RollingAtr(cfg.atr_days)
        longest = max(cfg.breakout_days, cfg.exit_days, cfg.trend_days, TREND_FAST_DAYS) + 2
        self._highs: deque[float] = deque(maxlen=longest)
        self._lows: deque[float] = deque(maxlen=longest)
        self._closes: deque[float] = deque(maxlen=longest)
        self._month_ends: deque[float] = deque(maxlen=cfg.momentum_months + 1)
        self._month: tuple[int, int] | None = None
        self._index = -1
        self.atr: float | None = None
        self.channel_low = math.nan  # the last exit_days days' low, this day included
        self.channel_high = math.nan

    def on_day(
        self, opened: dt.datetime, open_: float, high: float, low: float, close: float
    ) -> list[SwingSetup]:
        """Apply one closed daily bar (``opened`` is its start, 00:00 UTC)."""
        del open_
        self._index += 1
        closed = opened + dt.timedelta(days=1)
        atr = self._atr.update(high, low, close)
        self.atr = atr
        month = (opened.year, opened.month)
        new_month = self._month is not None and month != self._month
        if new_month and self._closes:
            self._month_ends.append(self._closes[-1])  # the previous month's last close
        self._month = month
        found: list[SwingSetup] = []
        if atr is not None:
            if DONCHIAN in self._rules:
                found += self._donchian(closed, close, atr)
            if MOMENTUM in self._rules and new_month:
                found += self._momentum(opened, closed, close, atr)
            if PULLBACK in self._rules:
                found += self._pullback(closed, close, atr)
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        days = self._cfg.exit_days
        if len(self._lows) >= days:
            self.channel_low = min(list(self._lows)[-days:])
            self.channel_high = max(list(self._highs)[-days:])
        return found

    def _setup(
        self,
        closed: dt.datetime,
        direction: Direction,
        rule: str,
        close: float,
        atr: float,
        detail: str,
        close_by: dt.datetime | None = None,
    ) -> SwingSetup:
        return SwingSetup(self._index, closed, direction, rule, close, atr, close_by, detail)

    def _donchian(self, closed: dt.datetime, close: float, atr: float) -> list[SwingSetup]:
        days = self._cfg.breakout_days
        if len(self._highs) < days:
            return []
        top, bottom = max(list(self._highs)[-days:]), min(list(self._lows)[-days:])
        if close > top:
            text = f"closed above the {days}-day high {top:.2f}"
            return [self._setup(closed, Direction.LONG, DONCHIAN, close, atr, text)]
        if close < bottom:
            text = f"closed below the {days}-day low {bottom:.2f}"
            return [self._setup(closed, Direction.SHORT, DONCHIAN, close, atr, text)]
        return []

    def _momentum(
        self, opened: dt.datetime, closed: dt.datetime, close: float, atr: float
    ) -> list[SwingSetup]:
        months = self._cfg.momentum_months
        if len(self._month_ends) < months + 1:
            return []
        latest, earlier = self._month_ends[-1], self._month_ends[0]
        if latest == earlier:
            return []
        direction = Direction.LONG if latest > earlier else Direction.SHORT
        change = (latest / earlier - 1.0) * 100.0
        text = f"{months}-month return {change:+.1f}%: {direction.value.lower()} for the month"
        return [
            self._setup(closed, direction, MOMENTUM, close, atr, text, _next_month(opened.date()))
        ]

    def _pullback(self, closed: dt.datetime, close: float, atr: float) -> list[SwingSetup]:
        cfg = self._cfg
        if len(self._closes) < cfg.trend_days:
            return []
        trend = _average(self._closes, cfg.trend_days)
        fast = _average(self._closes, TREND_FAST_DAYS)
        before = _average(self._closes, cfg.pullback_days)  # the average up to yesterday
        today = (sum(list(self._closes)[-(cfg.pullback_days - 1) :]) + close) / cfg.pullback_days
        previous = self._closes[-1]
        if close > trend and fast > trend and previous < before and close > today:
            text = f"back above the {cfg.pullback_days}-day average {today:.2f} in an uptrend"
            return [self._setup(closed, Direction.LONG, PULLBACK, close, atr, text)]
        if close < trend and fast < trend and previous > before and close < today:
            text = f"back below the {cfg.pullback_days}-day average {today:.2f} in a downtrend"
            return [self._setup(closed, Direction.SHORT, PULLBACK, close, atr, text)]
        return []


def swing_plan(
    setup: SwingSetup,
    config: SwingConfig,
    ladder: tuple[float, ...],
    costs: LadderCosts,
    *,
    cost_multiple: float,
    plan_id: str,
    bar: dt.timedelta,
) -> ScalpPlan:
    """The market order for ``setup``: stop ``stop_atr`` daily ATRs away, targets on ``ladder``."""
    sign = setup.direction.sign
    distance = config.stop_atr * setup.atr
    stop = setup.reference - distance if sign > 0 else setup.reference + distance + costs.spread
    risk = abs(setup.reference - stop)
    if risk <= 0:
        return ScalpPlan(None, "no stop distance")
    if ladder[0] * risk < cost_multiple * costs.round_trip - _EPSILON:
        return ScalpPlan(None, "TP1 does not clear the costs")
    targets: list[float | None] = [setup.reference + sign * step * risk for step in ladder]
    targets += [None] * (3 - len(targets))
    first = targets[0]
    assert first is not None
    plan = TradePlan(
        id=plan_id,
        time=setup.time,
        direction=setup.direction,
        session=None,
        entry_mode="market",
        entry=setup.reference,
        stop_loss=stop,
        take_profit_1=first,
        take_profit_2=targets[1],
        take_profit_3=targets[2],
        expires_at=setup.time + bar,
        score=60.0,
        risk_reward_1=ladder[0],
        close_by=setup.close_by,
    )
    return ScalpPlan(plan, "", ladder[0])


def swing_management(
    config: ScalperConfig, breakeven_offset: float, *, trail: bool, swap: bool = True
) -> Management:
    """Breakeven+ at TP1, the stop to TP1 after TP2, the channel trail (``trail``) and
    the overnight swap; no time stop."""
    return Management(
        split=config.split,
        breakeven_offset=breakeven_offset,
        stop_to_tp1_after_tp2=True,
        step_trail=trail,
        trail_buffer_atr=0.0,
        time_stop=None,
        swap=swap,
    )

"""The scalper's intraday mode (S3, D109): three entry rules on closed M5 bars.

Every position is flat by the New York rollover (the plan's ``close_by``), one at a
time, with the scalper's risk limits; every rule is fed one closed bar at a time, so
a backtest and the live bot see the same thing.

``asian_breakout``
    The Asian (Tokyo) range is the high and low of the Asia window. In the London
    window, the first bar that closes beyond it by ``breakout_buffer_atr`` x ATR is a
    setup in that direction; the stop goes beyond the range's middle. One a day.

``opening_range``
    The New York opening range is the high and low of the first
    ``opening_range_minutes`` after ``ny_open`` (New York time). Until
    ``breakout_until`` the first bar closing beyond it (by the same buffer) is a
    setup; the stop goes beyond the range's middle. One a day.

``vwap_trend``
    A volume-weighted average price (tick volume: spot gold has no exchange volume)
    from the London open. In the London and New York windows, a bar that closes
    across it (by the buffer) while the VWAP itself moves that way over the last
    ``vwap_slope_bars`` bars is a setup; the stop goes beyond the extreme of the last
    ``swing_bars`` bars, and the VWAP then trails it (it only ever tightens). One per
    window and direction a day.

The research behind these (``docs/SCALPER_MODES.md``) found no independent evidence
that they beat costs on gold; S3 measures them before anything trades.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import LONDON, NEW_YORK, ROLLOVER, SessionCalculator, local_to_utc
from goldsweep.core.settings import IntradayConfig, ScalperConfig
from goldsweep.execution.book import Management, TradePlan
from goldsweep.scalping.plan import LadderCosts, ScalpPlan
from goldsweep.scalping.trail import RollingAtr

ASIAN_BREAKOUT, OPENING_RANGE, VWAP_TREND = "asian_breakout", "opening_range", "vwap_trend"
RULES = (ASIAN_BREAKOUT, OPENING_RANGE, VWAP_TREND)
_NEW_YORK = ZoneInfo("America/New_York")
#: An Asian range needs at least this share of its window's bars (a gap is no range).
_MIN_RANGE_COVER = 0.5
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class IntradaySetup:
    """An intraday entry found on a closed bar."""

    index: int  # the engine's bar count
    time: dt.datetime  # the bar's close (UTC): the order goes in from here
    direction: Direction
    rule: str
    reference: float  # the bar's close: the planned (market) entry
    anchor: float  # what the stop goes beyond (a range's middle, a swing extreme)
    atr: float
    session: str  # london or newyork
    close_by: dt.datetime  # flat by the New York rollover
    detail: str


@dataclass
class _Range:
    high: float = -math.inf
    low: float = math.inf
    bars: int = 0

    def add(self, high: float, low: float) -> None:
        self.high, self.low, self.bars = max(self.high, high), min(self.low, low), self.bars + 1

    @property
    def middle(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class _Day:
    """What one UTC day has seen so far."""

    asia: _Range = field(default_factory=_Range)
    opening: _Range = field(default_factory=_Range)
    done: set[str] = field(default_factory=set)  # rules (or rule:window:side) used today
    volume: float = 0.0
    weighted: float = 0.0


class IntradayEngine:
    """Feeds closed bars to the intraday rules; returns their setups."""

    def __init__(
        self,
        config: ScalperConfig,
        sessions: SessionCalculator,
        *,
        bar: dt.timedelta,
        rules: tuple[str, ...] | None = None,
    ) -> None:
        self._cfg: IntradayConfig = config.intraday
        self._sessions = sessions
        self._bar = bar
        self._rules = rules if rules is not None else self._cfg.rules
        unknown = set(self._rules) - set(RULES)
        if unknown:
            raise ValueError(f"unknown intraday rules: {sorted(unknown)}")
        self._atr = RollingAtr(config.atr_period)
        self._index = -1
        self._day: dt.date | None = None
        self._today = _Day()
        self._spans: dict[tuple[str, dt.date], tuple[dt.datetime, dt.datetime]] = {}
        self._lows: deque[float] = deque(maxlen=self._cfg.swing_bars)
        self._highs: deque[float] = deque(maxlen=self._cfg.swing_bars)
        self._vwaps: deque[float] = deque(maxlen=self._cfg.vwap_slope_bars + 1)
        self._previous_close: float | None = None
        self.vwap = math.nan
        self.atr: float | None = None

    # -- the clock ------------------------------------------------------------- #

    def _span(self, name: str, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
        key = (name, day)
        if key not in self._spans:
            span = self._sessions.span(name, day)
            self._spans[key] = (span.start, span.end)
            if len(self._spans) > 64:
                self._spans.pop(next(iter(self._spans)))
        return self._spans[key]

    def _rollover(self, moment: dt.datetime) -> dt.datetime:
        """The start of the New York rollover on the New York date of ``moment``."""
        return self._span(ROLLOVER, moment.astimezone(_NEW_YORK).date())[0]

    # -- each bar ---------------------------------------------------------------- #

    def on_bar(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[IntradaySetup]:
        """Apply one closed bar (``opened`` is its open time, UTC)."""
        self._index += 1
        day = opened.date()
        if day != self._day:
            self._day, self._today = day, _Day()
        atr = self._atr.update(high, low, close)
        self.atr = atr
        closed = opened + self._bar
        found: list[IntradaySetup] = []
        london_start, london_end = self._span(LONDON, day)
        self._update_vwap(opened, london_start, high, low, close, volume)
        if atr is not None:
            if ASIAN_BREAKOUT in self._rules:
                found += self._asian_breakout(opened, closed, close, atr, london_start, london_end)
            if OPENING_RANGE in self._rules:
                found += self._opening_range(opened, closed, close, atr)
            if VWAP_TREND in self._rules:
                found += self._vwap_trend(opened, closed, close, atr, london_start, london_end)
        self._track_ranges(opened, high, low)  # after the rules: a bar is not its own range
        self._lows.append(low)
        self._highs.append(high)
        self._previous_close = close
        return found

    def _track_ranges(self, opened: dt.datetime, high: float, low: float) -> None:
        asia_start, asia_end = self._span("asia", opened.date())
        if asia_start <= opened < asia_end:
            self._today.asia.add(high, low)
        start, end = self._opening_window(opened)
        if start <= opened < end:
            self._today.opening.add(high, low)

    def _opening_window(self, opened: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        ny_day = opened.astimezone(_NEW_YORK).date()
        start = local_to_utc(ny_day, self._cfg.ny_open, _NEW_YORK)
        return start, start + dt.timedelta(minutes=self._cfg.opening_range_minutes)

    def _update_vwap(
        self,
        opened: dt.datetime,
        anchor: dt.datetime,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        today = self._today
        if opened >= anchor:
            weight = max(float(volume), 1.0)
            today.volume += weight
            today.weighted += weight * (high + low + close) / 3.0
            self.vwap = today.weighted / today.volume
        else:
            self.vwap = math.nan
        self._vwaps.append(self.vwap)

    # -- the rules ------------------------------------------------------------ #

    def _breakout(
        self,
        rule: str,
        span: _Range,
        closed: dt.datetime,
        close: float,
        atr: float,
        session: str,
        what: str,
    ) -> list[IntradaySetup]:
        buffer = self._cfg.breakout_buffer_atr * atr
        if close > span.high + buffer:
            direction = Direction.LONG
        elif close < span.low - buffer:
            direction = Direction.SHORT
        else:
            return []
        self._today.done.add(rule)
        side = "above" if direction is Direction.LONG else "below"
        return [
            IntradaySetup(
                index=self._index,
                time=closed,
                direction=direction,
                rule=rule,
                reference=close,
                anchor=span.middle,
                atr=atr,
                session=session,
                close_by=self._rollover(closed),
                detail=(
                    f"closed {side} the {what} {span.low:.2f}-{span.high:.2f}; stop beyond "
                    f"its middle {span.middle:.2f}"
                ),
            )
        ]

    def _asian_breakout(
        self,
        opened: dt.datetime,
        closed: dt.datetime,
        close: float,
        atr: float,
        london_start: dt.datetime,
        london_end: dt.datetime,
    ) -> list[IntradaySetup]:
        if ASIAN_BREAKOUT in self._today.done or not london_start <= opened < london_end:
            return []
        asia_start, asia_end = self._span("asia", opened.date())
        expected = (asia_end - asia_start) / self._bar
        if self._today.asia.bars < _MIN_RANGE_COVER * expected:
            return []
        return self._breakout(
            ASIAN_BREAKOUT, self._today.asia, closed, close, atr, LONDON, "Asian range"
        )

    def _opening_range(
        self, opened: dt.datetime, closed: dt.datetime, close: float, atr: float
    ) -> list[IntradaySetup]:
        if OPENING_RANGE in self._today.done:
            return []
        start, end = self._opening_window(opened)
        until = local_to_utc(
            opened.astimezone(_NEW_YORK).date(), self._cfg.breakout_until, _NEW_YORK
        )
        expected = self._cfg.opening_range_minutes / (self._bar.total_seconds() / 60.0)
        if not (end <= opened and closed <= until) or self._today.opening.bars < expected:
            return []
        return self._breakout(
            OPENING_RANGE, self._today.opening, closed, close, atr, NEW_YORK, "opening range"
        )

    def _vwap_trend(
        self,
        opened: dt.datetime,
        closed: dt.datetime,
        close: float,
        atr: float,
        london_start: dt.datetime,
        london_end: dt.datetime,
    ) -> list[IntradaySetup]:
        ny_start, ny_end = self._span(NEW_YORK, opened.astimezone(_NEW_YORK).date())
        if london_start <= opened < london_end:
            window = LONDON
        elif ny_start <= opened < ny_end:
            window = NEW_YORK
        else:
            return []
        vwaps = list(self._vwaps)
        if len(vwaps) < self._cfg.vwap_slope_bars + 1 or any(math.isnan(v) for v in vwaps):
            return []
        if self._previous_close is None or len(self._lows) < self._cfg.swing_bars:
            return []
        now, before, earlier = vwaps[-1], vwaps[-2], vwaps[0]
        buffer = self._cfg.breakout_buffer_atr * atr
        if self._previous_close <= before and close > now + buffer and now > earlier:
            direction, anchor = Direction.LONG, min(self._lows)
        elif self._previous_close >= before and close < now - buffer and now < earlier:
            direction, anchor = Direction.SHORT, max(self._highs)
        else:
            return []
        key = f"{VWAP_TREND}:{window}:{direction.value}"
        if key in self._today.done:
            return []
        self._today.done.add(key)
        side = "above" if direction is Direction.LONG else "below"
        return [
            IntradaySetup(
                index=self._index,
                time=closed,
                direction=direction,
                rule=VWAP_TREND,
                reference=close,
                anchor=anchor,
                atr=atr,
                session=window,
                close_by=self._rollover(closed),
                detail=f"closed {side} the VWAP {now:.2f} with it moving that way",
            )
        ]


# --------------------------------------------------------------------------- #
# The order and its management
# --------------------------------------------------------------------------- #


def intraday_plan(
    setup: IntradaySetup,
    config: IntradayConfig,
    ladder: tuple[float, ...],
    costs: LadderCosts,
    *,
    cost_multiple: float,
    plan_id: str,
    bar: dt.timedelta,
) -> ScalpPlan:
    """The market order for ``setup``: stop beyond its anchor, targets on ``ladder``."""
    sign = setup.direction.sign
    buffer = config.sl_buffer_atr * setup.atr
    if setup.direction is Direction.LONG:
        stop = setup.anchor - buffer
    else:
        stop = setup.anchor + buffer + costs.spread  # a short is stopped on the ask
    risk = (setup.reference - stop) * sign
    if risk <= 0:
        return ScalpPlan(None, "the stop is on the wrong side")
    atr_multiple = risk / setup.atr
    if not config.sl_min_atr - _EPSILON <= atr_multiple <= config.sl_max_atr + _EPSILON:
        return ScalpPlan(
            None, f"stop {atr_multiple:.2f} ATR away, outside {config.sl_min_atr:g}-"
            f"{config.sl_max_atr:g} ATR"
        )  # fmt: skip
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
        session=setup.session,
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


def intraday_management(
    config: ScalperConfig, breakeven_offset: float, *, trail: bool
) -> Management:
    """Breakeven+ at TP1 and the stop to TP1 after TP2 for every rule; the VWAP trails
    the stop for ``vwap_trend`` (``trail``). No time stop: the rollover ends the day."""
    return Management(
        split=config.split,
        breakeven_offset=breakeven_offset,
        stop_to_tp1_after_tp2=True,
        step_trail=trail,
        trail_buffer_atr=config.intraday.sl_buffer_atr,
        time_stop=None,
    )

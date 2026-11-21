"""The scalper's BUY/SELL setups, from one closed bar at a time (prompt section 4.3).

A SELL (a BUY mirrors it) on a closed bar, by strategy mode:

``sweep_reclaim`` (default)
    the step trail is down (or flips down on this bar); a high was swept and
    reclaimed (:mod:`~goldsweep.scalping.levels`) within ``sweep_window`` bars; and
    this bar is bearish and closes below the low of the sweep candle (the micro
    structure shift). Each sweep gives at most one setup.

``trail_rejection``
    the step trail is down and did not flip on this bar; the bar's high touched the
    line in force during the bar while its body stayed below it; and the bar is
    bearish and closes below the previous bar's low. (The prompt names this mode
    without its details; this reading is recorded in D100.)

The engine knows nothing of sessions as trading windows, news, the H1 bias, the
spread or risk: the caller checks those (:mod:`~goldsweep.scalping.backtest`, and
the live bot from S2), so the same setups serve every test.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import ScalperConfig
from goldsweep.scalping.levels import HIGH, Level, MicroLiquidity, SessionRanges, Sweep
from goldsweep.scalping.trail import DOWN, NOT_READY, UP, RollingAtr, StepTrail, TrailPoint

SWEEP_RECLAIM, TRAIL_REJECTION = "sweep_reclaim", "trail_rejection"
MODES = (SWEEP_RECLAIM, TRAIL_REJECTION)


@dataclass(frozen=True, slots=True)
class ScalpSetup:
    """A scalp the engine found on a closed bar; the plan is made from it."""

    index: int  # the engine's bar count
    time: dt.datetime  # the signal bar's close (UTC): the order goes in from here
    direction: Direction
    mode: str
    reference: float  # the signal bar's close: the planned (market) entry
    extreme: float  # the stop's anchor: the sweep's X, or the rejection wick
    trail_line: float  # the step line after this bar
    atr: float
    level_price: float | None  # the swept level (sweep_reclaim)
    level_kind: str | None  # swing, twin, asia, london; None for trail_rejection
    flipped: bool  # the step trail flipped on this bar
    sweep_time: dt.datetime | None  # open time of the sweep candle (the X)

    @property
    def score(self) -> float:
        """A label, not a filter: 60, +10 for a twin-bar or session-range level."""
        bonus = 10.0 if self.level_kind not in (None, "swing") else 0.0
        return 60.0 + bonus

    @property
    def reason(self) -> str:
        side = "high" if self.direction is Direction.SHORT else "low"
        if self.mode == SWEEP_RECLAIM:
            return (
                f"{self.level_kind} {side} {self.level_price:.2f} swept to {self.extreme:.2f}, "
                "closed back inside, structure shift"
            )
        return f"rejected the step trail at {self.extreme:.2f}, structure shift"


class ScalpEngine:
    """Feeds closed bars to the step trail and the micro liquidity; returns setups."""

    def __init__(
        self,
        config: ScalperConfig,
        *,
        bar: dt.timedelta,
        sessions: SessionCalculator | None = None,
        modes: tuple[str, ...] | None = None,
    ) -> None:
        self._cfg = config
        self._bar = bar
        self._modes = modes if modes is not None else (config.strategy,)
        unknown = set(self._modes) - set(MODES)
        if unknown:
            raise ValueError(f"unknown scalper modes: {sorted(unknown)}")
        self._atr = RollingAtr(config.atr_period)
        self._trail = StepTrail(config.step_trail.atr_period, config.step_trail.multiplier)
        self._liquidity = MicroLiquidity(
            swing_bars=config.swing_bars,
            micro_lookback=config.micro_lookback,
            sweep_min_atr=config.sweep_min_atr,
            twin_tolerance_atr=config.twin_bars_tolerance_atr,
        )
        self._ranges = SessionRanges(sessions) if sessions is not None else None
        self._recent: list[tuple[Sweep, dt.datetime]] = []
        self._opened: dict[int, dt.datetime] = {}
        self._index = -1
        self._previous: tuple[float, float] | None = None  # the previous bar's high, low
        self.point: TrailPoint | None = None
        self.atr: float | None = None

    @property
    def levels(self) -> list[Level]:
        return self._liquidity.levels

    @property
    def line(self) -> float:
        """The step line now (NaN before it is ready)."""
        return self.point.line if self.point is not None else math.nan

    def on_bar(
        self, opened: dt.datetime, open_: float, high: float, low: float, close: float
    ) -> list[ScalpSetup]:
        """Apply one closed bar (``opened`` is its open time, UTC)."""
        self._index += 1
        index = self._index
        self._opened[index] = opened
        self._opened.pop(index - self._cfg.sweep_window - 3, None)
        atr = self._atr.update(high, low, close)
        point = self._trail.update(high, low, close)
        self.point, self.atr = point, atr
        if self._ranges is not None:
            self._liquidity.add(self._ranges.update(index, opened, high, low))
        for sweep in self._liquidity.update(index, high, low, close, atr):
            self._recent.append((sweep, self._opened.get(sweep.bar, opened)))
        window = self._cfg.sweep_window
        self._recent = [(s, t) for s, t in self._recent if index - s.bar <= window]
        found: list[ScalpSetup] = []
        if atr is not None and point.trend != NOT_READY:
            closed_at = opened + self._bar
            if SWEEP_RECLAIM in self._modes:
                setup = self._sweep_reclaim(index, closed_at, open_, close, point, atr)
                if setup is not None:
                    found.append(setup)
            if TRAIL_REJECTION in self._modes:
                setup = self._trail_rejection(index, closed_at, open_, high, low, close, point, atr)
                if setup is not None:
                    found.append(setup)
        self._previous = (high, low)
        return found

    def _sweep_reclaim(
        self,
        index: int,
        closed_at: dt.datetime,
        open_: float,
        close: float,
        point: TrailPoint,
        atr: float,
    ) -> ScalpSetup | None:
        for position in range(len(self._recent) - 1, -1, -1):
            sweep, swept_at = self._recent[position]
            if sweep.reclaimed > index:
                continue
            if sweep.side == HIGH:
                if point.trend != DOWN or not (close < open_ and close < sweep.bar_low):
                    continue
                direction = Direction.SHORT
            else:
                if point.trend != UP or not (close > open_ and close > sweep.bar_low):
                    continue
                direction = Direction.LONG
            del self._recent[position]  # one setup per sweep
            return ScalpSetup(
                index=index,
                time=closed_at,
                direction=direction,
                mode=SWEEP_RECLAIM,
                reference=close,
                extreme=sweep.extreme,
                trail_line=point.line,
                atr=atr,
                level_price=sweep.level.price,
                level_kind=sweep.level.kind,
                flipped=point.flipped,
                sweep_time=swept_at,
            )
        return None

    def _trail_rejection(
        self,
        index: int,
        closed_at: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        point: TrailPoint,
        atr: float,
    ) -> ScalpSetup | None:
        line = point.previous_line
        if point.flipped or self._previous is None or math.isnan(line):
            return None
        previous_high, previous_low = self._previous
        if point.trend == DOWN:
            touched = high >= line and max(open_, close) < line
            if not (touched and close < open_ and close < previous_low):
                return None
            direction, extreme = Direction.SHORT, high
        elif point.trend == UP:
            touched = low <= line and min(open_, close) > line
            if not (touched and close > open_ and close > previous_high):
                return None
            direction, extreme = Direction.LONG, low
        else:
            return None
        return ScalpSetup(
            index=index,
            time=closed_at,
            direction=direction,
            mode=TRAIL_REJECTION,
            reference=close,
            extreme=extreme,
            trail_line=point.line,
            atr=atr,
            level_price=None,
            level_kind=None,
            flipped=False,
            sweep_time=None,
        )

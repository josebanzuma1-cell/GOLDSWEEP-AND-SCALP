"""Micro liquidity and its sweeps, one closed bar at a time (prompt section 4.2).

Levels where stops rest on the scalping timeframe:

* **swing** points: a fractal high (low) strictly above (below) the ``swing_bars``
  bars before it and at least as high (low) as the ``swing_bars`` bars after it,
  the rule of :func:`goldsweep.analysis.structure.swing_indices`; usable once those
  bars have closed, for ``micro_lookback`` bars from the pivot;
* **twin** bars: two consecutive bars with equal highs (or lows) within
  ``twin_tolerance_atr`` x ATR, a micro double top (bottom); the level is the
  higher high (lower low), usable from the next bar, for ``micro_lookback`` bars;
* **session ranges**: the high and low of the Asian and London windows, usable
  once the window has ended (:class:`SessionRanges`).

A **sweep** of a high: a bar trades at least ``sweep_min_atr`` x ATR above the
level and that bar or the next one closes back below it. The sweep's extreme (the
"X") is the highest high of those bars. A level is used once: swept or broken
(no close back inside within the two bars), it is gone. Lows mirror highs.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field

from goldsweep.core.clock import SessionCalculator, SessionSpan

HIGH, LOW = "high", "low"
SWING, TWIN = "swing", "twin"
#: A session range that ends while no bars arrive (a weekend) is not used afterwards.
_STALE_RANGE = dt.timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Level:
    """A price where stops are likely to rest."""

    price: float
    side: str  # HIGH or LOW
    kind: str  # SWING, TWIN, or a session name ("asia", "london")
    formed: int  # index of the bar the level belongs to
    ready: int  # first bar index that may sweep it
    expires: int  # last bar index on which it counts


@dataclass(frozen=True, slots=True)
class Sweep:
    """A level raided and closed back inside: the setup's liquidity grab."""

    side: str  # HIGH: a raid above (bearish), LOW: a raid below (bullish)
    level: Level  # the farthest level swept
    extreme: float  # the X: the highest high (lowest low) of the sweep bars
    bar: int  # index of the bar that went beyond the level
    bar_low: float  # that bar's low (its high for a LOW sweep): the structure line
    reclaimed: int  # index of the bar whose close came back inside

    @property
    def kinds(self) -> str:
        return self.level.kind


@dataclass
class _Pending:
    side: str
    levels: list[Level]
    extreme: float
    bar: int
    bar_low: float


class MicroLiquidity:
    """Tracks micro levels and reports each completed sweep once."""

    def __init__(
        self,
        *,
        swing_bars: int,
        micro_lookback: int,
        sweep_min_atr: float,
        twin_tolerance_atr: float,
    ) -> None:
        if swing_bars < 1:
            raise ValueError("swing_bars must be at least 1")
        self._k = swing_bars
        self._lookback = micro_lookback
        self._sweep_min_atr = sweep_min_atr
        self._twin_tolerance_atr = twin_tolerance_atr
        self._highs: deque[float] = deque(maxlen=2 * swing_bars + 1)
        self._lows: deque[float] = deque(maxlen=2 * swing_bars + 1)
        self._levels: list[Level] = []
        self._pending: dict[str, _Pending] = {}

    @property
    def levels(self) -> list[Level]:
        """The levels that can still be swept."""
        return list(self._levels)

    def add(self, levels: list[Level]) -> None:
        """Levels found elsewhere (session ranges)."""
        self._levels.extend(levels)

    def update(
        self, index: int, high: float, low: float, close: float, atr: float | None
    ) -> list[Sweep]:
        """Apply one closed bar; return the sweeps completed on it."""
        self._expire(index)
        done = self._resolve_pending(index, high, low, close)
        if atr is not None:
            done += self._new_sweeps(index, high, low, close, atr)
        self._find_swings(index, high, low)
        if atr is not None:
            self._find_twins(index, high, low, atr)
        return done

    # -- levels --------------------------------------------------------------- #

    def _expire(self, index: int) -> None:
        self._levels = [level for level in self._levels if level.expires >= index]

    def _find_swings(self, index: int, high: float, low: float) -> None:
        k = self._k
        self._highs.append(high)
        self._lows.append(low)
        if len(self._highs) < 2 * k + 1:
            return
        highs, lows = list(self._highs), list(self._lows)
        pivot = index - k
        centre_high, centre_low = highs[k], lows[k]
        if centre_high > max(highs[:k]) and centre_high >= max(highs[k + 1 :]):
            self._levels.append(
                Level(centre_high, HIGH, SWING, pivot, index + 1, pivot + self._lookback)
            )
        if centre_low < min(lows[:k]) and centre_low <= min(lows[k + 1 :]):
            self._levels.append(
                Level(centre_low, LOW, SWING, pivot, index + 1, pivot + self._lookback)
            )

    def _find_twins(self, index: int, high: float, low: float, atr: float) -> None:
        if len(self._highs) < 2:
            return
        tolerance = self._twin_tolerance_atr * atr
        previous_high, previous_low = self._highs[-2], self._lows[-2]
        if abs(high - previous_high) <= tolerance:
            price = max(high, previous_high)
            self._levels.append(Level(price, HIGH, TWIN, index, index + 1, index + self._lookback))
        if abs(low - previous_low) <= tolerance:
            price = min(low, previous_low)
            self._levels.append(Level(price, LOW, TWIN, index, index + 1, index + self._lookback))

    # -- sweeps --------------------------------------------------------------- #

    def _new_sweeps(
        self, index: int, high: float, low: float, close: float, atr: float
    ) -> list[Sweep]:
        reach = self._sweep_min_atr * atr
        done: list[Sweep] = []
        for side in (HIGH, LOW):
            if side in self._pending:
                continue
            if side == HIGH:
                hit = [
                    lv
                    for lv in self._levels
                    if lv.side == HIGH and lv.ready <= index and high >= lv.price + reach
                ]
            else:
                hit = [
                    lv
                    for lv in self._levels
                    if lv.side == LOW and lv.ready <= index and low <= lv.price - reach
                ]
            if not hit:
                continue
            self._levels = [lv for lv in self._levels if lv not in hit]
            extreme = high if side == HIGH else low
            structure = low if side == HIGH else high
            pending = _Pending(side, hit, extreme, index, structure)
            sweep = self._reclaim(pending, index, close)
            if sweep is not None:
                done.append(sweep)
            else:
                self._pending[side] = pending
        return done

    def _resolve_pending(self, index: int, high: float, low: float, close: float) -> list[Sweep]:
        done: list[Sweep] = []
        for side, pending in list(self._pending.items()):
            del self._pending[side]
            pending.extreme = (
                max(pending.extreme, high) if side == HIGH else min(pending.extreme, low)
            )
            sweep = self._reclaim(pending, index, close)
            if sweep is not None:
                done.append(sweep)
            # otherwise the level broke: it is gone, no sweep
        return done

    @staticmethod
    def _reclaim(pending: _Pending, index: int, close: float) -> Sweep | None:
        if pending.side == HIGH:
            farthest = max(pending.levels, key=lambda lv: lv.price)
            back_inside = close < farthest.price
        else:
            farthest = min(pending.levels, key=lambda lv: lv.price)
            back_inside = close > farthest.price
        if not back_inside:
            return None
        return Sweep(pending.side, farthest, pending.extreme, pending.bar, pending.bar_low, index)


@dataclass
class _Range:
    span: SessionSpan
    high: float
    low: float


@dataclass
class SessionRanges:
    """The high and low of session windows; each becomes a level when its window ends."""

    sessions: SessionCalculator
    names: tuple[str, ...] = ("asia", "london")
    life_bars: int = 720
    _spans: dict[str, SessionSpan] = field(default_factory=dict)
    _ranges: dict[str, _Range] = field(default_factory=dict)

    def _span(self, name: str, moment: dt.datetime) -> SessionSpan:
        """The window ``name`` containing ``moment``, or the next one."""
        span = self._spans.get(name)
        while span is None or span.end <= moment:
            day = (
                span.local_date + dt.timedelta(days=1) if span else moment.date() - dt.timedelta(1)
            )
            span = self.sessions.span(name, day)
        self._spans[name] = span
        return span

    def update(self, index: int, opened: dt.datetime, high: float, low: float) -> list[Level]:
        """Apply one bar (``opened`` is its open time); return ranges that just ended."""
        found: list[Level] = []
        for name in self.names:
            current = self._ranges.get(name)
            if current is not None and opened >= current.span.end:
                del self._ranges[name]
                if opened - current.span.end <= _STALE_RANGE:
                    expires = index + self.life_bars
                    found.append(Level(current.high, HIGH, name, index - 1, index, expires))
                    found.append(Level(current.low, LOW, name, index - 1, index, expires))
            span = self._span(name, opened)
            if span.start <= opened < span.end:
                current = self._ranges.get(name)
                if current is None or current.span.start != span.start:
                    self._ranges[name] = _Range(span, high, low)
                else:
                    current.high, current.low = max(current.high, high), min(current.low, low)
        return found

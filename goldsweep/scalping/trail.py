"""The step-trail line and a rolling ATR, fed one closed bar at a time.

The step trail is the scalper's trend and trailing-stop engine (prompt section
4.1). On each closed bar, with ``hl2 = (high + low) / 2``:

* ``upper = hl2 + multiplier x ATR`` and ``lower = hl2 - multiplier x ATR``;
* in an uptrend the line is ``max(previous line, lower)``, in a downtrend
  ``min(previous line, upper)``, so it only ever moves with the trend (the stairs);
* the trend flips when a bar *closes* through the line that was in force during
  that bar; the new line starts at the other band.

The value after a bar is what the next bar sees, so nothing looks ahead.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

UP, DOWN, NOT_READY = 1, -1, 0


class RollingAtr:
    """The simple-average ATR (as ``analysis.indicators.atr_values`` with "sma"), incrementally.

    The first bar's true range is its high minus its low; after that the previous
    close counts. ``None`` until ``period`` bars have been seen.
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be at least 1")
        self._period = period
        self._ranges: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._previous_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._previous_close is None:
            true_range = high - low
        else:
            true_range = max(high, self._previous_close) - min(low, self._previous_close)
        self._previous_close = close
        if len(self._ranges) == self._period:
            self._sum -= self._ranges[0]
        self._ranges.append(true_range)
        self._sum += true_range
        self.value = self._sum / self._period if len(self._ranges) == self._period else None
        return self.value


@dataclass(frozen=True, slots=True)
class TrailPoint:
    """The step trail after one closed bar."""

    line: float  # NaN until the ATR is ready
    trend: int  # UP, DOWN or NOT_READY
    flipped: bool  # the trend changed on this bar
    previous_line: float  # the line in force during this bar (NaN when none)


class StepTrail:
    """The step-trail line, one closed bar at a time."""

    def __init__(self, atr_period: int, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        self._atr = RollingAtr(atr_period)
        self._multiplier = multiplier
        self.line = math.nan
        self.trend = NOT_READY

    def update(self, high: float, low: float, close: float) -> TrailPoint:
        atr = self._atr.update(high, low, close)
        previous = self.line
        if atr is None:
            return TrailPoint(math.nan, NOT_READY, False, previous)
        middle = (high + low) / 2.0
        upper = middle + self._multiplier * atr
        lower = middle - self._multiplier * atr
        flipped = False
        if self.trend == NOT_READY:
            self.trend = UP if close >= middle else DOWN
            self.line = lower if self.trend == UP else upper
        elif self.trend == UP:
            if close < previous:
                self.trend, self.line, flipped = DOWN, upper, True
            else:
                self.line = max(previous, lower)
        elif close > previous:
            self.trend, self.line, flipped = UP, lower, True
        else:
            self.line = min(previous, upper)
        return TrailPoint(self.line, self.trend, flipped, previous)


def step_trail(candles: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    """The step trail over a whole frame: columns ``line``, ``trend``, ``flipped``."""
    trail = StepTrail(atr_period, multiplier)
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    close = candles["close"].to_numpy(dtype="float64")
    lines = np.full(len(candles), np.nan)
    trends = np.zeros(len(candles), dtype="int64")
    flips = np.zeros(len(candles), dtype=bool)
    for i in range(len(candles)):
        point = trail.update(float(high[i]), float(low[i]), float(close[i]))
        lines[i], trends[i], flips[i] = point.line, point.trend, point.flipped
    return pd.DataFrame({"line": lines, "trend": trends, "flipped": flips}, index=candles.index)

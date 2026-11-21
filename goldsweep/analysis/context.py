"""Trend, structure and regime context of a setup (Phase 10, D86).

Tags added to a signal after the engine, from closed bars only, so they can be
measured as A+ conditions by the playbook analysis. They never change the score.

* ``d1_trend`` / ``h4_trend``: the daily / four-hour swing structure (the last two
  swing highs and lows, :func:`~goldsweep.analysis.structure.classify_trend`) points
  the trade's way: higher highs and lows for a long, lower ones for a short;
* ``ema_trend``: the last closed H1 bar is on the trade's side of the H1 200 EMA;
* ``weekly_value``: a long's entry is in the lower half (discount) of the previous
  trading week's range, a short's in the upper half (premium);
* ``high_vol``: the H1 ATR is more than 1.2 times its median of the last 120 hours.

The four-hour and daily bars are built from H1 bars anchored to the 17:00 New York
trading day, the same way in backtests (from stored bars) and in live trading (from
the broker's H1 bars).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from goldsweep.analysis.indicators import atr_values
from goldsweep.analysis.periods import aggregate_anchored, closed_as_of, trading_weeks
from goldsweep.analysis.structure import Direction, Trend, classify_trend, find_swings
from goldsweep.core.types import Timeframe

if TYPE_CHECKING:
    from goldsweep.analysis.signals import Signal
    from goldsweep.core.settings import DayBoundaryConfig

CONTEXT_TAGS = ("d1_trend", "h4_trend", "ema_trend", "weekly_value", "high_vol")
EMA_PERIOD = 200
SWING_LOOKBACK = 2
VOL_WINDOW = 120
VOL_RATIO = 1.2


@dataclass(frozen=True)
class ContextFrames:
    """H1 bars and the H4 / D1 bars built from them."""

    hourly: pd.DataFrame
    h4: pd.DataFrame
    daily: pd.DataFrame
    boundary: DayBoundaryConfig

    @classmethod
    def from_hourly(cls, hourly: pd.DataFrame, boundary: DayBoundaryConfig) -> ContextFrames:
        return cls(
            hourly,
            aggregate_anchored(hourly, Timeframe.H4, boundary),
            aggregate_anchored(hourly, Timeframe.D1, boundary),
            boundary,
        )


def _trend_agrees(frame: pd.DataFrame, direction: Direction) -> bool:
    if len(frame) < 2 * SWING_LOOKBACK + 3:
        return False
    trend = classify_trend(find_swings(frame, SWING_LOOKBACK))
    return trend is (Trend.UP if direction is Direction.LONG else Trend.DOWN)


def context_tags(signal: Signal, frames: ContextFrames) -> tuple[str, ...]:
    """The context tags of ``signal``, from bars closed by its time."""
    now = signal.time
    long = signal.direction is Direction.LONG
    tags: list[str] = []
    daily = closed_as_of(frames.daily, Timeframe.D1, now).tail(60)
    if _trend_agrees(daily, signal.direction):
        tags.append("d1_trend")
    h4 = closed_as_of(frames.h4, Timeframe.H4, now).tail(120)
    if _trend_agrees(h4, signal.direction):
        tags.append("h4_trend")
    hourly = closed_as_of(frames.hourly, Timeframe.H1, now)
    if len(hourly) >= EMA_PERIOD:
        close = hourly["close"]
        ema = float(close.ewm(span=EMA_PERIOD, adjust=False).mean().iloc[-1])
        if (float(close.iloc[-1]) - ema) * signal.direction.sign > 0:
            tags.append("ema_trend")
    if len(hourly):
        weeks = trading_weeks(hourly.index, frames.boundary)
        current = weeks[-1]
        earlier = np.unique(weeks[weeks < current])
        if len(earlier):
            last_week = hourly[weeks == earlier[-1]]
            middle = (float(last_week["high"].max()) + float(last_week["low"].min())) / 2.0
            if (signal.entry < middle) if long else (signal.entry > middle):
                tags.append("weekly_value")
    if len(hourly) > VOL_WINDOW:
        tail = hourly.tail(VOL_WINDOW + 20)
        atr = atr_values(
            tail["high"].to_numpy(dtype="float64"),
            tail["low"].to_numpy(dtype="float64"),
            tail["close"].to_numpy(dtype="float64"),
            14,
        )
        recent = atr[-VOL_WINDOW:]
        recent = recent[np.isfinite(recent)]
        if len(recent) and atr[-1] > VOL_RATIO * float(np.median(recent)):
            tags.append("high_vol")
    return tuple(tags)


def with_context(signal: Signal, frames: ContextFrames) -> Signal:
    """``signal`` with its context tags added."""
    extra = tuple(t for t in context_tags(signal, frames) if t not in signal.tags)
    if not extra:
        return signal
    return dataclasses.replace(signal, tags=(*signal.tags, *extra))

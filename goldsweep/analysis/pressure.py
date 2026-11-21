"""Buyer/seller pressure meter: who is pushing harder, as an estimate from 0 to 100%.

What it can and cannot know
---------------------------
Spot gold trades over the counter, with no central exchange, so nobody sees the
real volume of buy and sell orders. MetaTrader 5's *tick volume* counts how many
times the price changed during a bar: a measure of activity, not of contracts.
The meter therefore **estimates** pressure from what the bars and the news show.
It describes the recent past and present; it is not a forecast, and a market
that reads "strong buyers" can still fall.

The four readings, each from -1 (sellers) to +1 (buyers)
--------------------------------------------------------
* **volume**: where each bar closed within its range (the close-location value:
  +1 on the high, -1 on the low), weighted by the bar's tick volume. Busy bars
  that close near their highs mean buyers were in control of the activity.
* **candles**: the bodies of up-candles against the bodies of down-candles. Big
  green bodies and small red ones mean buyers are moving price more easily.
* **structure**: the H1 swing structure. Higher highs and higher lows +1, lower
  lows and lower highs -1, a range 0.
* **news**: the news bias, built in Phase 4 from recent headlines and the
  outlook for upcoming scheduled events. 0 (neutral) until then.

Their weighted sum is the score. ``buyers = 50 * (1 + score)`` percent and
sellers are the rest, so 50/50 means balanced.

The meter is shown with alerts and on the dashboard. It does not change signal
scores: the backtester reports how signals did when the meter agreed with them
and when it did not, so its value is measured before anyone relies on it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from goldsweep.analysis.structure import Direction, Trend, classify_trend, find_swings

if TYPE_CHECKING:
    from goldsweep.core.settings import PressureConfig

STRONG_BUYERS = "strong buyers"
BUYERS = "buyers"
BALANCED = "balanced"
SELLERS = "sellers"
STRONG_SELLERS = "strong sellers"

#: How many H1 bars the structure reading looks back over (five trading days).
STRUCTURE_BARS = 120


@dataclass(frozen=True, slots=True)
class PressureReading:
    """One reading of the meter."""

    time: dt.datetime  # close of the last bar used (UTC)
    buyers: float  # 0..100 percent
    sellers: float  # 100 - buyers
    score: float  # -1..+1, positive = buyers stronger
    label: str  # strong buyers / buyers / balanced / sellers / strong sellers
    components: Mapping[str, float]  # each reading, -1..+1
    bars: int  # how many bars the volume and candle readings used

    @property
    def leaning(self) -> Direction | None:
        """The side with the upper hand, or ``None`` when balanced."""
        if self.label == BALANCED:
            return None
        return Direction.LONG if self.score > 0 else Direction.SHORT

    def agrees_with(self, direction: Direction) -> bool | None:
        """Does the meter lean the same way as a trade? ``None`` when balanced."""
        leaning = self.leaning
        return None if leaning is None else leaning is direction

    def describe(self) -> str:
        return f"Buyers {self.buyers:.0f}% / Sellers {self.sellers:.0f}% ({self.label})"


def volume_reading(candles: pd.DataFrame) -> float:
    """Tick-volume-weighted close location, -1..+1 (0 when there is nothing to weigh)."""
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    close = candles["close"].to_numpy(dtype="float64")
    volume = candles["tick_volume"].to_numpy(dtype="float64")
    span = high - low
    location = np.divide(
        (close - low) - (high - close), span, out=np.zeros_like(span), where=span > 0
    )
    total = volume.sum()
    return float((location * volume).sum() / total) if total > 0 else 0.0


def candle_reading(candles: pd.DataFrame) -> float:
    """Up-candle bodies against down-candle bodies, -1..+1."""
    body = candles["close"].to_numpy(dtype="float64") - candles["open"].to_numpy(dtype="float64")
    total = np.abs(body).sum()
    return float(body.sum() / total) if total > 0 else 0.0


def structure_reading(hourly: pd.DataFrame | None, swing_lookback: int) -> float:
    """+1 for an H1 uptrend, -1 for a downtrend, 0 for a range or too little data."""
    if hourly is None or hourly.empty:
        return 0.0
    recent = hourly.iloc[-STRUCTURE_BARS:] if len(hourly) > STRUCTURE_BARS else hourly
    trend = classify_trend(find_swings(recent, swing_lookback))
    return {Trend.UP: 1.0, Trend.DOWN: -1.0, Trend.RANGE: 0.0}[trend]


def pressure_meter(
    candles: pd.DataFrame,
    config: PressureConfig,
    *,
    hourly: pd.DataFrame | None = None,
    news_bias: float = 0.0,
    swing_lookback: int = 3,
) -> PressureReading | None:
    """Read the meter from closed bars of ``config.timeframe``.

    Args:
        candles: closed bars; the last ``config.window`` are used.
        config: the ``pressure`` section of config.yaml.
        hourly: closed H1 bars for the structure reading (optional).
        news_bias: the news layer's score, -1..+1 (values outside are clipped).
        swing_lookback: fractal lookback for the structure reading.

    Returns ``None`` when there are no bars to read.
    """
    if candles.empty:
        return None
    recent = candles.iloc[-config.window :] if len(candles) > config.window else candles
    components = {
        "volume": volume_reading(recent),
        "candles": candle_reading(recent),
        "structure": structure_reading(hourly, swing_lookback),
        "news": max(-1.0, min(1.0, float(news_bias))),
    }
    weights = config.weights
    score = (
        weights.volume * components["volume"]
        + weights.candles * components["candles"]
        + weights.structure * components["structure"]
        + weights.news * components["news"]
    )
    score = max(-1.0, min(1.0, score))
    buyers = round(50.0 * (1.0 + score), 1)
    return PressureReading(
        time=recent.index[-1].to_pydatetime() + config.timeframe.duration,
        buyers=buyers,
        sellers=round(100.0 - buyers, 1),
        score=round(score, 4),
        label=_label(score, config),
        components={name: round(value, 4) for name, value in components.items()},
        bars=len(recent),
    )


def _label(score: float, config: PressureConfig) -> str:
    size = abs(score)
    if size < config.balanced_band:
        return BALANCED
    if score > 0:
        return STRONG_BUYERS if size >= config.strong_level else BUYERS
    return STRONG_SELLERS if size >= config.strong_level else SELLERS

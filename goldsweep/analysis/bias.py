"""Higher-timeframe bias (spec 5.1).

Three ingredients, each between -1 (bearish) and +1 (bullish):

* **Structure**: the H1 swing structure. Higher highs and higher lows is +1,
  lower lows and lower highs is -1, anything else 0.
* **Location**: where price is against the previous day's midpoint. Below it is
  *discount*, where one looks to buy (+1); above it is *premium*, where one
  looks to sell (-1).
* **News**: the time-decayed news score from the news layer (Phase 4; 0 until
  then).

Their weighted sum decides: at or beyond the threshold the bias is LONG or
SHORT, otherwise NEUTRAL. A LONG bias allows only longs, a SHORT bias only
shorts; NEUTRAL allows both but asks for a higher signal score.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import pandas as pd

from goldsweep.analysis.structure import Direction, Trend, classify_trend, find_swings

if TYPE_CHECKING:
    from goldsweep.core.settings import BiasConfig


class Bias(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"

    def allows(self, direction: Direction) -> bool:
        """May a trade in ``direction`` be taken under this bias?"""
        return self is Bias.NEUTRAL or self.value == direction.value


@dataclass(frozen=True, slots=True)
class BiasResult:
    bias: Bias
    score: float  # -1 .. +1, positive is bullish
    trend: Trend  # H1 swing structure
    structure: float  # +1 uptrend, -1 downtrend, 0 range
    location: float  # +1 discount, -1 premium, 0 unknown
    news: float  # -1 .. +1 as supplied
    previous_mid: float | None  # the previous day's midpoint used for location


def compute_bias(
    hourly: pd.DataFrame,
    *,
    price: float,
    previous_mid: float | None,
    news_bias: float,
    config: BiasConfig,
    swing_lookback: int,
) -> BiasResult:
    """Combine structure, location and news into LONG, SHORT or NEUTRAL.

    Args:
        hourly: closed bias-timeframe (H1) bars.
        price: the current price (last trigger close).
        previous_mid: midpoint of the previous day's range, if known.
        news_bias: news score in -1..+1; values outside are clipped.
        config: weights and threshold.
        swing_lookback: fractal lookback for the swing structure.
    """
    trend = classify_trend(find_swings(hourly, swing_lookback)) if len(hourly) else Trend.RANGE
    structure = {Trend.UP: 1.0, Trend.DOWN: -1.0, Trend.RANGE: 0.0}[trend]
    if previous_mid is None or price == previous_mid:
        location = 0.0
    else:
        location = 1.0 if price < previous_mid else -1.0
    news = max(-1.0, min(1.0, float(news_bias)))
    score = (
        config.structure_weight * structure
        + config.location_weight * location
        + config.news_weight * news
    )
    if score >= config.threshold:
        bias = Bias.LONG
    elif score <= -config.threshold:
        bias = Bias.SHORT
    else:
        bias = Bias.NEUTRAL
    return BiasResult(
        bias=bias,
        score=score,
        trend=trend,
        structure=structure,
        location=location,
        news=news,
        previous_mid=previous_mid,
    )

"""Average True Range.

The default method is a simple moving average of the true range, which is what
MetaTrader 5's built-in ATR indicator draws, so thresholds expressed in ATRs
match what is visible on the chart. Wilder's smoothing is available as an option.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

AtrMethod = Literal["sma", "wilder"]


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True range per bar. The first bar has no previous close, so it is high minus low."""
    if len(high) == 0:
        return np.array([], dtype="float64")
    previous_close = np.concatenate(([close[0]], close[:-1]))
    result = np.maximum(high, previous_close) - np.minimum(low, previous_close)
    result[0] = high[0] - low[0]
    return result


def atr_values(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
    method: AtrMethod = "sma",
) -> np.ndarray:
    """ATR per bar, ``NaN`` until ``period`` bars exist.

    The value at position ``i`` uses bars up to and including ``i`` only.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    count = len(high)
    out = np.full(count, np.nan, dtype="float64")
    if count < period:
        return out
    ranges = true_range(
        np.asarray(high, dtype="float64"),
        np.asarray(low, dtype="float64"),
        np.asarray(close, dtype="float64"),
    )
    if method == "sma":
        cumulative = np.cumsum(ranges)
        window_sum = cumulative[period - 1 :] - np.concatenate(([0.0], cumulative[:-period]))
        out[period - 1 :] = window_sum / period
    elif method == "wilder":
        out[period - 1] = ranges[:period].mean()
        for i in range(period, count):
            out[i] = (out[i - 1] * (period - 1) + ranges[i]) / period
    else:
        raise ValueError(f"unknown ATR method {method!r}")
    return out


def atr(candles: pd.DataFrame, period: int = 14, method: AtrMethod = "sma") -> pd.Series:
    """ATR as a series aligned with ``candles``."""
    values = atr_values(
        candles["high"].to_numpy(dtype="float64"),
        candles["low"].to_numpy(dtype="float64"),
        candles["close"].to_numpy(dtype="float64"),
        period,
        method,
    )
    return pd.Series(values, index=candles.index, name="atr")


def latest_atr(candles: pd.DataFrame, period: int = 14, method: AtrMethod = "sma") -> float | None:
    """The most recent ATR value, or ``None`` when there are not enough bars yet."""
    if len(candles) < period:
        return None
    # One extra bar gives the oldest true range its previous close; Wilder's
    # smoothing needs a longer run-in to settle.
    window = period * 10 if method == "wilder" else period + 1
    recent = candles.iloc[-window:] if len(candles) > window else candles
    value = float(atr(recent, period, method).iloc[-1])
    return None if np.isnan(value) else value

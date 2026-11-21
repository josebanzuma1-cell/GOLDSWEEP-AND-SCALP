"""Candle (OHLC bar) frame helpers.

Project-wide candle contract
----------------------------
A candle frame is a pandas ``DataFrame`` with:

* index: bar OPEN time as a timezone-aware UTC ``DatetimeIndex`` named
  ``time``, strictly increasing;
* columns: ``open``, ``high``, ``low``, ``close`` (float), ``tick_volume``
  (int) and ``spread`` (int, in broker points).

Every broker, the database and (from Phase 2) the analysis engine use this one
shape, so the same pure functions work live and in backtests.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from goldsweep.core.types import Timeframe

CANDLE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "tick_volume", "spread")
_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
_ONE_SECOND = pd.Timedelta(seconds=1)


def empty_candles() -> pd.DataFrame:
    """A candle frame with no rows and the right columns, dtypes and index."""
    data: dict[str, np.ndarray] = {
        column: np.array([], dtype="float64") for column in _PRICE_COLUMNS
    }
    data["tick_volume"] = np.array([], dtype="int64")
    data["spread"] = np.array([], dtype="int64")
    return pd.DataFrame(data, index=pd.DatetimeIndex([], tz="UTC", name="time"))


def validate_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Check a frame against the candle contract and return a normalised copy.

    A timezone-aware index in another zone is converted to UTC (aware instants
    are unambiguous). A naive index is rejected: guessing its zone could shift
    every session window.

    Raises:
        ValueError: wrong index type, naive index, missing columns, unsorted or
            duplicated times, missing prices, or a bar whose high is below its low.
    """
    if not isinstance(candles.index, pd.DatetimeIndex):
        raise ValueError("candles must be indexed by bar open time (DatetimeIndex)")
    if candles.index.tz is None:
        raise ValueError(
            "candle index must be timezone-aware (UTC); naive timestamps are not accepted"
        )
    missing = [column for column in CANDLE_COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"candles are missing columns: {', '.join(missing)}")

    out = candles.loc[:, list(CANDLE_COLUMNS)].copy()
    out.index = out.index.tz_convert("UTC").rename("time")
    if not out.index.is_monotonic_increasing or not out.index.is_unique:
        raise ValueError("candle index must be strictly increasing (sorted, no duplicates)")
    if out.loc[:, list(_PRICE_COLUMNS)].isna().to_numpy().any():
        raise ValueError("candles contain missing prices")
    if (out["high"] < out["low"]).any():
        raise ValueError("candles contain a bar whose high is below its low")
    for column in _PRICE_COLUMNS:
        out[column] = out[column].astype("float64")
    out["tick_volume"] = out["tick_volume"].astype("int64")
    out["spread"] = out["spread"].astype("int64")
    return out


def to_epoch_seconds(index: pd.DatetimeIndex) -> np.ndarray:
    """Whole seconds since 1970-01-01 UTC for each timestamp of an aware index."""
    if index.tz is None:
        raise ValueError("expected a timezone-aware index")
    return np.asarray((index - _EPOCH) // _ONE_SECOND, dtype="int64")


def from_epoch_seconds(seconds: object) -> pd.DatetimeIndex:
    """Inverse of :func:`to_epoch_seconds`: a UTC ``DatetimeIndex`` named ``time``."""
    values = np.asarray(seconds, dtype="int64")
    return pd.DatetimeIndex(pd.to_datetime(values, unit="s", utc=True), name="time")


def aggregate_candles(candles: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
    """Build higher-timeframe bars from lower-timeframe ones.

    Buckets are aligned to UTC (an H1 bar starts on the hour, a D1 bar at 00:00
    UTC). Buckets that contain no source bar (weekend, daily break) are left out
    rather than filled, exactly as a broker does.
    """
    if candles.empty:
        return empty_candles()
    resampled = candles.resample(timeframe.pandas_freq, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "tick_volume": "sum",
            "spread": "max",
        }
    )
    out = resampled.dropna(subset=["open"]).copy()
    out["tick_volume"] = out["tick_volume"].astype("int64")
    out["spread"] = out["spread"].astype("int64")
    out.index = out.index.rename("time")
    return out.loc[:, list(CANDLE_COLUMNS)]

"""Trading days, trading weeks and anchored higher-timeframe candles.

Gold's trading day ends at 17:00 New York time. Days, weeks and 4-hour candles
are all counted from that close. The arithmetic is done in New York local time,
so daylight saving comes from the time-zone database and never from a fixed
offset.

A 4-hour candle anchored this way opens at 17:00, 21:00, 01:00, 05:00, 09:00 and
13:00 New York time, which is how charts on a "New York close" broker draw them.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldsweep.core.clock import ensure_utc, local_to_utc
from goldsweep.core.types import Timeframe
from goldsweep.data.candles import CANDLE_COLUMNS, empty_candles

if TYPE_CHECKING:
    from goldsweep.core.settings import DayBoundaryConfig

_ONE_DAY = pd.Timedelta(days=1)


def _local_wall_clock(index: pd.DatetimeIndex, tz: str) -> pd.DatetimeIndex:
    """Naive local wall-clock times for an aware index."""
    return index.tz_convert(ZoneInfo(tz)).tz_localize(None)


def _boundary_offset(boundary: DayBoundaryConfig) -> pd.Timedelta:
    return pd.Timedelta(hours=boundary.time.hour, minutes=boundary.time.minute)


def trading_days(index: pd.DatetimeIndex, boundary: DayBoundaryConfig) -> pd.DatetimeIndex:
    """The trading day each bar belongs to, as naive midnight timestamps.

    A bar that opens at or after the boundary belongs to the next calendar day's
    trading day: Sunday 18:00 New York is part of Monday.
    """
    shift = (_ONE_DAY - _boundary_offset(boundary)) % _ONE_DAY
    return (_local_wall_clock(index, boundary.tz) + shift).normalize()


def trading_day_of(moment: dt.datetime, boundary: DayBoundaryConfig) -> dt.date:
    """The trading day that contains the instant ``moment``."""
    local = ensure_utc(moment).astimezone(ZoneInfo(boundary.tz)).replace(tzinfo=None)
    shift = (_ONE_DAY - _boundary_offset(boundary)) % _ONE_DAY
    return (local + shift.to_pytimedelta()).date()


def trading_day_start(day: dt.date, boundary: DayBoundaryConfig) -> dt.datetime:
    """UTC instant at which trading day ``day`` begins."""
    if boundary.time == dt.time(0, 0):
        return local_to_utc(day, boundary.time, boundary.tz)
    return local_to_utc(day - dt.timedelta(days=1), boundary.time, boundary.tz)


def trading_day_end(day: dt.date, boundary: DayBoundaryConfig) -> dt.datetime:
    """UTC instant at which trading day ``day`` ends (and the next one begins)."""
    return trading_day_start(day + dt.timedelta(days=1), boundary)


def trading_weeks(index: pd.DatetimeIndex, boundary: DayBoundaryConfig) -> np.ndarray:
    """An integer per bar identifying its trading week (ISO year * 100 + ISO week)."""
    iso = trading_days(index, boundary).isocalendar()
    return (iso["year"].to_numpy(dtype="int64") * 100 + iso["week"].to_numpy(dtype="int64")).astype(
        "int64"
    )


def bucket_starts(
    index: pd.DatetimeIndex, timeframe: Timeframe, boundary: DayBoundaryConfig
) -> pd.DatetimeIndex:
    """UTC open time of the anchored ``timeframe`` candle each bar falls into."""
    offset = _boundary_offset(boundary)
    local = _local_wall_clock(index, boundary.tz)
    start_local = (local - offset).floor(timeframe.pandas_freq) + offset
    localized = start_local.tz_localize(
        ZoneInfo(boundary.tz),
        ambiguous=np.zeros(len(start_local), dtype=bool),
        nonexistent="shift_forward",
    )
    return localized.tz_convert("UTC")


def aggregate_anchored(
    candles: pd.DataFrame, timeframe: Timeframe, boundary: DayBoundaryConfig
) -> pd.DataFrame:
    """Build ``timeframe`` candles whose day starts at the trading-day boundary.

    Unlike :func:`goldsweep.data.candles.aggregate_candles`, which aligns buckets
    to UTC, this aligns them to the 17:00 New York close all year round.
    """
    if candles.empty:
        return empty_candles()
    starts = bucket_starts(candles.index, timeframe, boundary)
    if starts.is_monotonic_increasing:
        return _aggregate_runs(candles, starts)
    out = candles.groupby(starts, sort=True).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "tick_volume": "sum",
            "spread": "max",
        }
    )
    out["tick_volume"] = out["tick_volume"].astype("int64")
    out["spread"] = out["spread"].astype("int64")
    out.index = pd.DatetimeIndex(out.index, name="time")
    return out.loc[:, list(CANDLE_COLUMNS)]


def _aggregate_runs(candles: pd.DataFrame, starts: pd.DatetimeIndex) -> pd.DataFrame:
    """Aggregate consecutive bars that share a bucket start (``starts`` is sorted)."""
    stamps = starts.asi8
    breaks = np.flatnonzero(np.diff(stamps)) + 1
    first = np.concatenate(([0], breaks))
    last = np.concatenate((breaks - 1, [len(stamps) - 1]))
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    volume = candles["tick_volume"].to_numpy(dtype="int64")
    spread = candles["spread"].to_numpy(dtype="int64")
    return pd.DataFrame(
        {
            "open": candles["open"].to_numpy(dtype="float64")[first],
            "high": np.maximum.reduceat(high, first),
            "low": np.minimum.reduceat(low, first),
            "close": candles["close"].to_numpy(dtype="float64")[last],
            "tick_volume": np.add.reduceat(volume, first).astype("int64"),
            "spread": np.maximum.reduceat(spread, first).astype("int64"),
        },
        index=pd.DatetimeIndex(starts[first], name="time"),
    )


def closed_as_of(candles: pd.DataFrame, timeframe: Timeframe, now: dt.datetime) -> pd.DataFrame:
    """Only the bars of ``timeframe`` that have fully closed by ``now``.

    ``candles`` must be sorted by time, as every candle frame is.
    """
    if candles.empty:
        return candles
    cutoff = pd.Timestamp(ensure_utc(now)) - timeframe.duration
    position = int(candles.index.searchsorted(cutoff, side="right"))
    return candles if position == len(candles) else candles.iloc[:position]

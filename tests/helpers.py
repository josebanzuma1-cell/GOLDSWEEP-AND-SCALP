"""Shared helpers for the test suite."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np
import pandas as pd


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> dt.datetime:
    """An aware UTC datetime."""
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC)


def make_candles(
    start: dt.datetime, periods: int, *, minutes: int = 5, base: float = 2650.0
) -> pd.DataFrame:
    """A small, steadily rising candle frame starting at the aware instant ``start``."""
    index = pd.date_range(start, periods=periods, freq=f"{minutes}min", name="time")
    step = np.arange(periods, dtype="float64")
    return pd.DataFrame(
        {
            "open": base + step,
            "high": base + step + 1.5,
            "low": base + step - 1.0,
            "close": base + step + 0.5,
            "tick_volume": (100 + step).astype("int64"),
            "spread": np.full(periods, 21, dtype="int64"),
        },
        index=index,
    )


def assert_candles_equal(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    """Same columns, same bar times and same values (index resolution may differ)."""
    assert list(actual.columns) == list(expected.columns)
    assert len(actual) == len(expected)
    assert (actual.index == expected.index).all()
    np.testing.assert_allclose(actual.to_numpy(dtype="float64"), expected.to_numpy(dtype="float64"))


Row = tuple[float, float, float, float]


def bars(
    rows: Sequence[Row],
    start: dt.datetime,
    *,
    minutes: int = 5,
    spread: int = 20,
    volume: int = 100,
) -> pd.DataFrame:
    """A candle frame from hand-written ``(open, high, low, close)`` rows."""
    for number, (open_, high, low, close) in enumerate(rows):
        assert high >= max(open_, close) and low <= min(open_, close), (
            f"row {number} is not a candle"
        )
    index = pd.date_range(start, periods=len(rows), freq=f"{minutes}min", name="time")
    data = np.asarray(rows, dtype="float64").reshape(len(rows), 4)
    return pd.DataFrame(
        {
            "open": data[:, 0],
            "high": data[:, 1],
            "low": data[:, 2],
            "close": data[:, 3],
            "tick_volume": np.full(len(rows), volume, dtype="int64"),
            "spread": np.full(len(rows), spread, dtype="int64"),
        },
        index=index,
    )


def mirror(rows: Sequence[Row], centre: float) -> list[Row]:
    """Flip rows upside down around ``centre``: every long fixture becomes a short one."""
    return [
        (2 * centre - open_, 2 * centre - low, 2 * centre - high, 2 * centre - close)
        for open_, high, low, close in rows
    ]


def quiet(count: int, price: float, *, half_range: float = 0.2) -> list[Row]:
    """``count`` small, featureless candles around ``price``."""
    return [(price, price + half_range, price - half_range, price) for _ in range(count)]

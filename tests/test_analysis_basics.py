"""Trading-day periods and ATR: the plumbing under the analysis engine."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from goldsweep.analysis.indicators import atr, atr_values, latest_atr, true_range
from goldsweep.analysis.periods import (
    aggregate_anchored,
    bucket_starts,
    closed_as_of,
    trading_day_end,
    trading_day_of,
    trading_day_start,
    trading_days,
    trading_weeks,
)
from goldsweep.core.settings import DayBoundaryConfig
from goldsweep.core.types import Timeframe
from goldsweep.data.candles import empty_candles
from tests.helpers import make_candles, utc

NY_CLOSE = DayBoundaryConfig()  # 17:00 America/New_York
H4 = Timeframe.H4


def stamps(*moments: dt.datetime) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([pd.Timestamp(moment) for moment in moments])


# --------------------------------------------------------------------------- #
# Trading days and weeks
# --------------------------------------------------------------------------- #


def test_trading_day_rolls_over_at_5pm_new_york_in_winter() -> None:
    index = stamps(
        utc(2026, 11, 15, 23, 0),  # Sun 18:00 EST: the week opens, part of Monday
        utc(2026, 11, 16, 21, 59),  # Mon 16:59 EST
        utc(2026, 11, 16, 22, 0),  # Mon 17:00 EST: Tuesday begins
        utc(2026, 11, 20, 21, 55),  # Fri 16:55 EST
    )
    days = [stamp.date() for stamp in trading_days(index, NY_CLOSE)]
    assert days == [
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 17),
        dt.date(2026, 11, 20),
    ]


def test_trading_day_follows_us_daylight_saving() -> None:
    index = stamps(utc(2026, 7, 13, 20, 59), utc(2026, 7, 13, 21, 0))  # 16:59 and 17:00 EDT
    days = [stamp.date() for stamp in trading_days(index, NY_CLOSE)]
    assert days == [dt.date(2026, 7, 13), dt.date(2026, 7, 14)]


def test_trading_day_bounds() -> None:
    monday = dt.date(2026, 11, 16)
    assert trading_day_start(monday, NY_CLOSE) == utc(2026, 11, 15, 22, 0)
    assert trading_day_end(monday, NY_CLOSE) == utc(2026, 11, 16, 22, 0)
    assert trading_day_start(dt.date(2026, 7, 14), NY_CLOSE) == utc(2026, 7, 13, 21, 0)
    assert trading_day_of(utc(2026, 11, 16, 8, 0), NY_CLOSE) == monday
    assert trading_day_of(utc(2026, 11, 16, 22, 0), NY_CLOSE) == dt.date(2026, 11, 17)
    with pytest.raises(ValueError, match="naive"):
        trading_day_of(dt.datetime(2026, 11, 16, 8, 0), NY_CLOSE)


def test_midnight_boundary_means_calendar_days() -> None:
    utc_days = DayBoundaryConfig(tz="UTC", time="00:00")
    index = stamps(utc(2026, 11, 16, 0, 0), utc(2026, 11, 16, 23, 59))
    assert [stamp.date() for stamp in trading_days(index, utc_days)] == [dt.date(2026, 11, 16)] * 2
    assert trading_day_start(dt.date(2026, 11, 16), utc_days) == utc(2026, 11, 16)
    assert trading_day_end(dt.date(2026, 11, 16), utc_days) == utc(2026, 11, 17)


def test_trading_week_runs_from_sunday_evening_to_friday() -> None:
    index = stamps(
        utc(2026, 11, 15, 23, 0),  # Sunday evening: already the new week
        utc(2026, 11, 18, 12, 0),
        utc(2026, 11, 20, 21, 55),  # Friday
        utc(2026, 11, 22, 23, 0),  # next Sunday evening
    )
    weeks = trading_weeks(index, NY_CLOSE).tolist()
    assert weeks[0] == weeks[1] == weeks[2]
    assert weeks[3] == weeks[0] + 1


# --------------------------------------------------------------------------- #
# Anchored higher-timeframe candles
# --------------------------------------------------------------------------- #


def test_four_hour_candles_open_at_new_york_hours() -> None:
    winter = stamps(utc(2026, 11, 16, 5, 59), utc(2026, 11, 16, 6, 0), utc(2026, 11, 16, 9, 59))
    assert list(bucket_starts(winter, H4, NY_CLOSE)) == [
        utc(2026, 11, 16, 2, 0),  # the 21:00 New York candle
        utc(2026, 11, 16, 6, 0),  # the 01:00 New York candle
        utc(2026, 11, 16, 6, 0),
    ]
    summer = stamps(utc(2026, 7, 14, 4, 59), utc(2026, 7, 14, 5, 0), utc(2026, 7, 14, 13, 0))
    assert list(bucket_starts(summer, H4, NY_CLOSE)) == [
        utc(2026, 7, 14, 1, 0),
        utc(2026, 7, 14, 5, 0),  # 01:00 New York is 05:00 UTC in summer
        utc(2026, 7, 14, 13, 0),  # 09:00 New York
    ]


def test_hourly_bars_aggregate_into_anchored_four_hour_candles() -> None:
    hourly = make_candles(utc(2026, 11, 16, 2, 0), 8, minutes=60)  # 02:00 .. 09:00 UTC
    four_hour = aggregate_anchored(hourly, H4, NY_CLOSE)
    assert list(four_hour.index) == [utc(2026, 11, 16, 2, 0), utc(2026, 11, 16, 6, 0)]
    first, second = hourly.iloc[:4], hourly.iloc[4:]
    assert four_hour["open"].tolist() == [first["open"].iloc[0], second["open"].iloc[0]]
    assert four_hour["high"].tolist() == [first["high"].max(), second["high"].max()]
    assert four_hour["low"].tolist() == [first["low"].min(), second["low"].min()]
    assert four_hour["close"].tolist() == [first["close"].iloc[-1], second["close"].iloc[-1]]
    assert four_hour["tick_volume"].tolist() == [
        first["tick_volume"].sum(),
        second["tick_volume"].sum(),
    ]
    assert four_hour.index.name == "time"


def test_fast_and_general_aggregation_agree() -> None:
    hourly = make_candles(utc(2026, 11, 16, 2, 0), 30, minutes=60)
    fast = aggregate_anchored(hourly, H4, NY_CLOSE)  # sorted input: the numpy path
    shuffled = hourly.iloc[[*range(15, 30), *range(15)]]  # out of order: the groupby path
    general = aggregate_anchored(shuffled.sort_index(), H4, NY_CLOSE)
    pd.testing.assert_frame_equal(fast, general)
    reordered = aggregate_anchored(shuffled, H4, NY_CLOSE)
    assert list(reordered.index) == list(fast.index)
    assert reordered["high"].tolist() == fast["high"].tolist()
    assert reordered["tick_volume"].tolist() == fast["tick_volume"].tolist()


def test_daily_candles_follow_the_trading_day() -> None:
    hourly = make_candles(utc(2026, 11, 16, 20, 0), 4, minutes=60)  # 20:00 .. 23:00 UTC
    daily = aggregate_anchored(hourly, Timeframe.D1, NY_CLOSE)
    # 22:00 UTC is 17:00 in New York: the bars split into Monday's and Tuesday's candle.
    assert list(daily.index) == [utc(2026, 11, 15, 22, 0), utc(2026, 11, 16, 22, 0)]
    assert daily["close"].tolist() == [hourly["close"].iloc[1], hourly["close"].iloc[3]]


def test_empty_input_and_closed_bars_filter() -> None:
    assert aggregate_anchored(empty_candles(), H4, NY_CLOSE).empty
    hourly = make_candles(utc(2026, 11, 16, 6, 0), 3, minutes=60)  # 06:00, 07:00, 08:00
    closed = closed_as_of(hourly, Timeframe.H1, utc(2026, 11, 16, 8, 30))
    assert list(closed.index) == [utc(2026, 11, 16, 6, 0), utc(2026, 11, 16, 7, 0)]
    assert len(closed_as_of(hourly, Timeframe.H1, utc(2026, 11, 16, 9, 0))) == 3
    assert closed_as_of(empty_candles(), Timeframe.H1, utc(2026, 11, 16, 9, 0)).empty


# --------------------------------------------------------------------------- #
# ATR
# --------------------------------------------------------------------------- #

HIGH = np.array([10.0, 11.0, 10.5, 9.0])
LOW = np.array([8.0, 9.0, 7.0, 8.0])
CLOSE = np.array([9.0, 10.0, 8.0, 8.5])


def test_true_range_uses_the_previous_close() -> None:
    assert true_range(HIGH, LOW, CLOSE).tolist() == [2.0, 2.0, 3.5, 1.0]
    assert true_range(np.array([]), np.array([]), np.array([])).tolist() == []


def test_atr_simple_average_matches_hand_calculation() -> None:
    values = atr_values(HIGH, LOW, CLOSE, period=2, method="sma")
    assert np.isnan(values[0])
    assert values[1:].tolist() == [2.0, 2.75, 2.25]


def test_atr_wilder_smoothing() -> None:
    values = atr_values(HIGH, LOW, CLOSE, period=2, method="wilder")
    assert np.isnan(values[0])
    assert values[1:].tolist() == [2.0, 2.75, 1.875]


def test_atr_needs_enough_bars_and_valid_arguments() -> None:
    assert np.isnan(atr_values(HIGH, LOW, CLOSE, period=5)).all()
    with pytest.raises(ValueError, match="period"):
        atr_values(HIGH, LOW, CLOSE, period=0)
    with pytest.raises(ValueError, match="unknown ATR method"):
        atr_values(HIGH, LOW, CLOSE, period=2, method="ema")  # type: ignore[arg-type]


def test_atr_never_looks_ahead() -> None:
    frame = make_candles(utc(2026, 11, 16, 8, 0), 40)
    frame["high"] = frame["high"] + np.linspace(0.0, 3.0, 40)  # ranges that keep changing
    full = atr(frame, 14)
    shorter = atr(frame.iloc[:30], 14)
    np.testing.assert_allclose(full.iloc[:30].to_numpy(), shorter.to_numpy(), equal_nan=True)
    assert full.index.equals(frame.index)


@pytest.mark.parametrize("method", ["sma", "wilder"])
def test_latest_atr_matches_the_full_series(method: str) -> None:
    frame = make_candles(utc(2026, 11, 16, 8, 0), 200)
    frame["high"] = frame["high"] + np.abs(np.sin(np.arange(200))) * 2.0
    expected = float(atr(frame, 14, method).iloc[-1])  # type: ignore[arg-type]
    assert latest_atr(frame, 14, method) == pytest.approx(expected, rel=1e-6)  # type: ignore[arg-type]
    assert latest_atr(frame.iloc[:10], 14) is None
    assert latest_atr(frame.iloc[:14], 14) == pytest.approx(
        float(atr(frame.iloc[:14], 14).iloc[-1])
    )

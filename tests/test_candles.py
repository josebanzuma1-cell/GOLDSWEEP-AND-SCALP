"""The candle frame contract: validation, epoch conversion and aggregation."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from goldsweep.core.types import Timeframe
from goldsweep.data.candles import (
    CANDLE_COLUMNS,
    aggregate_candles,
    empty_candles,
    from_epoch_seconds,
    to_epoch_seconds,
    validate_candles,
)
from tests.helpers import assert_candles_equal, make_candles, utc

START = utc(2026, 11, 16, 8, 0)


def test_empty_candles_has_the_contract_shape() -> None:
    frame = empty_candles()
    assert frame.empty
    assert list(frame.columns) == list(CANDLE_COLUMNS)
    assert frame.index.name == "time"
    assert frame.index.tz is not None
    assert str(frame["open"].dtype) == "float64"
    assert str(frame["tick_volume"].dtype) == "int64"
    assert_candles_equal(validate_candles(frame), frame)


def test_valid_frame_passes_and_is_copied() -> None:
    frame = make_candles(START, 5)
    checked = validate_candles(frame)
    assert checked is not frame
    assert_candles_equal(checked, frame)
    assert checked.index.name == "time"


def test_extra_columns_are_dropped_and_order_is_fixed() -> None:
    frame = make_candles(START, 3)
    frame["real_volume"] = 0
    shuffled = frame[["close", "real_volume", "spread", "open", "tick_volume", "low", "high"]]
    assert list(validate_candles(shuffled).columns) == list(CANDLE_COLUMNS)


def test_other_time_zones_are_converted_to_utc() -> None:
    kampala = make_candles(START, 3).tz_convert("Africa/Kampala")
    checked = validate_candles(kampala)
    assert checked.index[0] == START
    assert checked.index[0].utcoffset() == dt.timedelta(0)


def test_naive_index_is_rejected() -> None:
    naive = make_candles(START, 3).tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_candles(naive)


def test_non_datetime_index_is_rejected() -> None:
    with pytest.raises(ValueError, match="DatetimeIndex"):
        validate_candles(make_candles(START, 3).reset_index(drop=True))


def test_missing_column_is_rejected() -> None:
    with pytest.raises(ValueError, match="spread"):
        validate_candles(make_candles(START, 3).drop(columns=["spread"]))


def test_unsorted_or_duplicated_times_are_rejected() -> None:
    frame = make_candles(START, 3)
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_candles(frame.iloc[::-1])
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_candles(pd.concat([frame, frame.iloc[[2]]]))


def test_missing_price_is_rejected() -> None:
    frame = make_candles(START, 3)
    frame.loc[frame.index[1], "close"] = float("nan")
    with pytest.raises(ValueError, match="missing prices"):
        validate_candles(frame)


def test_high_below_low_is_rejected() -> None:
    frame = make_candles(START, 3)
    frame.loc[frame.index[1], "high"] = frame["low"].iloc[1] - 1.0
    with pytest.raises(ValueError, match="high is below"):
        validate_candles(frame)


def test_epoch_seconds_round_trip() -> None:
    index = make_candles(START, 3).index
    seconds = to_epoch_seconds(index)
    first = int(START.timestamp())
    assert seconds.tolist() == [first, first + 300, first + 600]
    restored = from_epoch_seconds(seconds)
    assert restored.name == "time"
    assert (restored == index).all()


def test_epoch_seconds_need_an_aware_index() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_epoch_seconds(make_candles(START, 3).tz_localize(None).index)


def test_m1_bars_aggregate_into_m5() -> None:
    m1 = make_candles(START, 10, minutes=1)
    m5 = aggregate_candles(m1, Timeframe.M5)
    assert list(m5.index) == [START, START + dt.timedelta(minutes=5)]
    assert list(m5.columns) == list(CANDLE_COLUMNS)
    for position, chunk in enumerate((m1.iloc[:5], m1.iloc[5:])):
        bar = m5.iloc[position]
        assert bar["open"] == chunk["open"].iloc[0]
        assert bar["high"] == chunk["high"].max()
        assert bar["low"] == chunk["low"].min()
        assert bar["close"] == chunk["close"].iloc[-1]
        assert bar["tick_volume"] == chunk["tick_volume"].sum()
        assert bar["spread"] == chunk["spread"].max()
    assert str(m5["tick_volume"].dtype) == "int64"
    assert_candles_equal(validate_candles(m5), m5)


def test_empty_buckets_are_left_out() -> None:
    morning = make_candles(START, 5, minutes=1)
    later = make_candles(START + dt.timedelta(minutes=30), 5, minutes=1, base=2700.0)
    m5 = aggregate_candles(pd.concat([morning, later]), Timeframe.M5)
    assert list(m5.index) == [START, START + dt.timedelta(minutes=30)]


def test_partial_bucket_is_built_from_what_exists() -> None:
    m5 = aggregate_candles(make_candles(START, 7, minutes=1), Timeframe.M5)
    assert len(m5) == 2
    assert m5["tick_volume"].iloc[1] == 105 + 106  # only two one-minute bars in the bucket


def test_buckets_are_aligned_to_the_clock() -> None:
    m1 = make_candles(utc(2026, 11, 16, 8, 55), 10, minutes=1)  # 08:55 .. 09:04
    h1 = aggregate_candles(m1, Timeframe.H1)
    assert list(h1.index) == [utc(2026, 11, 16, 8, 0), utc(2026, 11, 16, 9, 0)]
    m15 = aggregate_candles(m1, Timeframe.M15)
    assert list(m15.index) == [utc(2026, 11, 16, 8, 45), utc(2026, 11, 16, 9, 0)]


def test_same_timeframe_and_empty_input() -> None:
    m1 = make_candles(START, 4, minutes=1)
    assert_candles_equal(aggregate_candles(m1, Timeframe.M1), m1)
    assert aggregate_candles(empty_candles(), Timeframe.M5).empty

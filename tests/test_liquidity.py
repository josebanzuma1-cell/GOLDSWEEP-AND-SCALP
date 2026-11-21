"""Liquidity pools and sweeps on hand-made candles."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from goldsweep.analysis.liquidity import (
    POOL_STRENGTH,
    Pool,
    PoolKind,
    Scope,
    Side,
    SweepStatus,
    day_levels,
    day_week_pools,
    detect_sweep,
    make_pool,
    merge_pools,
    round_number_pools,
    session_pools,
    swing_pools,
)
from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import DayBoundaryConfig, LiquidityConfig
from goldsweep.data.candles import empty_candles
from goldsweep.data.mock_broker import market_open_mask
from tests.helpers import Row, bars, mirror, quiet, utc

NY_CLOSE = DayBoundaryConfig()
MONDAY_8AM = utc(2026, 11, 16, 8, 0)


def flat(start: dt.datetime, end: dt.datetime, minutes: int, price: float = 2650.0) -> pd.DataFrame:
    """Featureless candles for every market-open slot in ``[start, end)``."""
    index = pd.date_range(start, end, freq=f"{minutes}min", inclusive="left", name="time")
    index = index[market_open_mask(index)]
    count = len(index)
    return pd.DataFrame(
        {
            "open": np.full(count, price),
            "high": np.full(count, price + 1.0),
            "low": np.full(count, price - 1.0),
            "close": np.full(count, price),
            "tick_volume": np.full(count, 100, dtype="int64"),
            "spread": np.full(count, 20, dtype="int64"),
        },
        index=index,
    )


def by_kind(pools: list[Pool]) -> dict[PoolKind, Pool]:
    return {pool.kind: pool for pool in pools}


# --------------------------------------------------------------------------- #
# Previous day and week
# --------------------------------------------------------------------------- #


@pytest.fixture
def hourly() -> pd.DataFrame:
    """Hourly bars from Thursday 12 November to Monday 16 November 2026, 08:00 UTC."""
    frame = flat(utc(2026, 11, 12), MONDAY_8AM, 60)
    frame.loc[pd.Timestamp(utc(2026, 11, 12, 5)), "low"] = 2610.0  # Thursday: the week's low
    frame.loc[pd.Timestamp(utc(2026, 11, 12, 10)), "high"] = 2690.0  # Thursday: the week's high
    frame.loc[pd.Timestamp(utc(2026, 11, 13, 3)), "low"] = 2630.0  # Friday's low
    frame.loc[pd.Timestamp(utc(2026, 11, 13, 14)), "high"] = 2670.0  # Friday's high
    frame.loc[pd.Timestamp(utc(2026, 11, 15, 23)), "high"] = 2699.0  # Sunday evening: Monday's bar
    return frame


def test_previous_day_on_a_monday_is_friday(hourly: pd.DataFrame) -> None:
    levels = day_levels(hourly, MONDAY_8AM, NY_CLOSE)
    assert levels.day == dt.date(2026, 11, 16)
    assert (levels.previous_high, levels.previous_low) == (2670.0, 2630.0)
    assert levels.previous_mid == 2650.0
    assert (levels.day_start, levels.day_end) == (utc(2026, 11, 15, 22), utc(2026, 11, 16, 22))


def test_previous_week_high_and_low(hourly: pd.DataFrame) -> None:
    levels = day_levels(hourly, MONDAY_8AM, NY_CLOSE)
    assert (levels.previous_week_high, levels.previous_week_low) == (2690.0, 2610.0)
    assert (levels.week_start, levels.week_end) == (utc(2026, 11, 15, 22), utc(2026, 11, 20, 22))


def test_previous_day_changes_at_the_5pm_new_york_close(hourly: pd.DataFrame) -> None:
    friday = hourly.loc[hourly.index < pd.Timestamp(utc(2026, 11, 13, 22))]
    before_close = day_levels(friday, utc(2026, 11, 13, 21, 59), NY_CLOSE)
    assert before_close.day == dt.date(2026, 11, 13)
    assert (before_close.previous_high, before_close.previous_low) == (2690.0, 2610.0)  # Thursday
    after_close = day_levels(friday, utc(2026, 11, 13, 22, 0), NY_CLOSE)
    assert after_close.day == dt.date(2026, 11, 14)
    assert (after_close.previous_high, after_close.previous_low) == (2670.0, 2630.0)  # Friday


def test_day_levels_without_history(hourly: pd.DataFrame) -> None:
    nothing = day_levels(empty_candles(), MONDAY_8AM, NY_CLOSE)
    assert nothing.previous_high is None and nothing.previous_mid is None
    only_today = hourly.loc[hourly.index >= pd.Timestamp(utc(2026, 11, 15, 22))]
    levels = day_levels(only_today, MONDAY_8AM, NY_CLOSE)
    assert levels.previous_high is None and levels.previous_week_high is None
    assert day_week_pools(levels) == []


def test_day_and_week_pools(hourly: pd.DataFrame) -> None:
    pools = by_kind(day_week_pools(day_levels(hourly, MONDAY_8AM, NY_CLOSE)))
    pdh, pdl = pools[PoolKind.PREVIOUS_DAY_HIGH], pools[PoolKind.PREVIOUS_DAY_LOW]
    assert (pdh.price, pdh.side, pdh.scope) == (2670.0, Side.BUY_SIDE, Scope.EXTERNAL)
    assert (pdl.price, pdl.side) == (2630.0, Side.SELL_SIDE)
    assert (pdh.created, pdh.expires) == (utc(2026, 11, 15, 22), utc(2026, 11, 16, 22))
    assert pdh.label == "previous day high" and not pdh.swept
    pwh, pwl = pools[PoolKind.PREVIOUS_WEEK_HIGH], pools[PoolKind.PREVIOUS_WEEK_LOW]
    assert (pwh.price, pwl.price) == (2690.0, 2610.0)
    assert pwh.expires == utc(2026, 11, 20, 22)
    assert pwh.strength == 1.0 > pdh.strength


# --------------------------------------------------------------------------- #
# Session ranges
# --------------------------------------------------------------------------- #


@pytest.fixture
def quarter_hourly() -> pd.DataFrame:
    """M15 bars for Monday 16 November 2026 (winter time), Sunday 22:00 to Monday 16:00 UTC."""
    frame = flat(utc(2026, 11, 15, 22), utc(2026, 11, 16, 16), 15)
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 2, 15)), "high"] = 2660.0  # Asia high
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 4, 30)), "low"] = 2645.0  # Asia low
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 9, 0)), "high"] = 2672.0  # London high
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 11, 45)), "low"] = 2648.0  # London low (after 10:00)
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 14, 0)), "high"] = 2680.0  # New York high
    frame.loc[pd.Timestamp(utc(2026, 11, 16, 15, 30)), "low"] = 2640.0  # New York low
    return frame


def session_kinds(
    frame: pd.DataFrame, sessions: SessionCalculator, now: dt.datetime
) -> set[PoolKind]:
    visible = frame.loc[frame.index < pd.Timestamp(now)]
    return {pool.kind for pool in session_pools(visible, sessions, now, NY_CLOSE)}


def test_asian_range_exists_once_the_asia_session_has_ended(
    quarter_hourly: pd.DataFrame, sessions: SessionCalculator
) -> None:
    assert session_kinds(quarter_hourly, sessions, utc(2026, 11, 16, 5, 45)) == set()
    now = utc(2026, 11, 16, 6, 0)
    visible = quarter_hourly.loc[quarter_hourly.index < pd.Timestamp(now)]
    pools = by_kind(session_pools(visible, sessions, now, NY_CLOSE))
    assert set(pools) == {PoolKind.ASIAN_HIGH, PoolKind.ASIAN_LOW}
    high, low = pools[PoolKind.ASIAN_HIGH], pools[PoolKind.ASIAN_LOW]
    assert (high.price, high.side) == (2660.0, Side.BUY_SIDE)
    assert (low.price, low.side) == (2645.0, Side.SELL_SIDE)
    assert (high.created, high.expires) == (now, utc(2026, 11, 16, 22))
    assert high.label == "Asian high" and high.is_external


def test_london_range_is_ready_when_new_york_opens(
    quarter_hourly: pd.DataFrame, sessions: SessionCalculator
) -> None:
    asian = {PoolKind.ASIAN_HIGH, PoolKind.ASIAN_LOW}
    assert session_kinds(quarter_hourly, sessions, utc(2026, 11, 16, 12, 45)) == asian
    now = utc(2026, 11, 16, 13, 0)
    visible = quarter_hourly.loc[quarter_hourly.index < pd.Timestamp(now)]
    pools = by_kind(session_pools(visible, sessions, now, NY_CLOSE))
    assert set(pools) == asian | {PoolKind.LONDON_HIGH, PoolKind.LONDON_LOW}
    # The London range runs until New York opens, so the 11:45 low counts.
    assert (pools[PoolKind.LONDON_HIGH].price, pools[PoolKind.LONDON_LOW].price) == (2672.0, 2648.0)
    assert pools[PoolKind.LONDON_LOW].created == now


def test_new_york_range_after_its_kill_zone(
    quarter_hourly: pd.DataFrame, sessions: SessionCalculator
) -> None:
    pools = by_kind(session_pools(quarter_hourly, sessions, utc(2026, 11, 16, 16, 0), NY_CLOSE))
    assert len(pools) == 6
    assert (pools[PoolKind.NEWYORK_HIGH].price, pools[PoolKind.NEWYORK_LOW].price) == (
        2680.0,
        2640.0,
    )


def test_session_ranges_need_the_whole_window_and_end_with_the_day(
    quarter_hourly: pd.DataFrame, sessions: SessionCalculator
) -> None:
    late_start = quarter_hourly.loc[quarter_hourly.index >= pd.Timestamp(utc(2026, 11, 16, 1, 0))]
    assert session_kinds(late_start, sessions, utc(2026, 11, 16, 6, 0)) == set()
    # After 22:00 UTC the next trading day has begun: Monday's ranges are history.
    assert session_pools(quarter_hourly, sessions, utc(2026, 11, 16, 22, 30), NY_CLOSE) == []
    assert session_pools(empty_candles(), sessions, utc(2026, 11, 16, 6, 0), NY_CLOSE) == []


# --------------------------------------------------------------------------- #
# Swing points, equal highs and trendlines
# --------------------------------------------------------------------------- #

START = utc(2026, 11, 16, 7, 0)
SWINGS: list[Row] = [
    (100.0, 100.5, 99.5, 100.2),  # 0
    (100.2, 102.0, 100.0, 101.5),  # 1  swing high 102.0
    (101.5, 101.6, 100.4, 100.6),  # 2
    (100.6, 100.9, 100.1, 100.7),  # 3  swing low 100.1
    (100.7, 102.1, 100.6, 101.8),  # 4  swing high 102.1: equal to the first, within 0.3
    (101.8, 101.9, 100.9, 101.0),  # 5
    (101.0, 101.2, 100.5, 101.1),  # 6  swing low 100.5
    (101.1, 101.6, 100.9, 101.4),  # 7  swing high 101.6
    (101.4, 101.5, 101.0, 101.2),  # 8
    (101.2, 101.3, 100.9, 101.25),  # 9  swing low 100.9: third rising low on one line
    (101.25, 101.7, 101.2, 101.65),  # 10
]
SWING_CONFIG = LiquidityConfig(major_swing_lookback=3)
NOW = utc(2026, 11, 16, 12, 0)


def swing_levels(
    rows: list[Row], config: LiquidityConfig = SWING_CONFIG
) -> list[tuple[PoolKind, float]]:
    pools = swing_pools(
        bars(rows, START, minutes=15), NOW, config, lookback=1, tolerance=0.3, bar_minutes=15
    )
    return sorted((pool.kind, pool.price) for pool in pools)


def test_equal_highs_trendline_and_single_swings() -> None:
    assert swing_levels(SWINGS) == sorted(
        [
            (PoolKind.EQUAL_HIGHS, 102.1),
            (PoolKind.SWING_HIGH, 101.6),
            (PoolKind.TRENDLINE_LOW, 100.9),
            (PoolKind.SWING_LOW, 100.1),
            (PoolKind.SWING_LOW, 100.5),
        ]
    )


def test_equal_highs_pool_details() -> None:
    frame = bars(SWINGS, START, minutes=15)
    pools = swing_pools(frame, NOW, SWING_CONFIG, lookback=1, tolerance=0.3, bar_minutes=15)
    equal = by_kind(pools)[PoolKind.EQUAL_HIGHS]
    assert (equal.side, equal.scope) == (Side.BUY_SIDE, Scope.EXTERNAL)
    assert equal.label == "equal highs (2 touches)"
    # Known once the later of the two highs is confirmed: at the close of bar 5.
    assert equal.created == frame.index[5].to_pydatetime() + dt.timedelta(minutes=15)
    trend = by_kind(pools)[PoolKind.TRENDLINE_LOW]
    assert (trend.side, trend.label) == (Side.SELL_SIDE, "trendline lows (3 touches)")
    minor = next(pool for pool in pools if pool.kind is PoolKind.SWING_HIGH)
    assert (minor.scope, minor.strength, minor.label) == (Scope.INTERNAL, 0.4, "swing high")


def test_a_swing_point_that_price_ran_through_is_no_longer_a_pool() -> None:
    rows = SWINGS + [(101.65, 102.9, 101.6, 102.8)]  # trades well above 102.1
    levels = swing_levels(rows)
    assert not any(kind in (PoolKind.EQUAL_HIGHS, PoolKind.SWING_HIGH) for kind, _price in levels)
    assert (PoolKind.TRENDLINE_LOW, 100.9) in levels


def test_trendline_needs_a_straight_unbroken_line() -> None:
    bent = list(SWINGS)
    bent[6] = (101.0, 101.2, 100.15, 101.1)  # the middle low is 0.35 off the line
    assert not any(kind is PoolKind.TRENDLINE_LOW for kind, _price in swing_levels(bent))
    off = LiquidityConfig(major_swing_lookback=3, trendlines=False)
    assert not any(kind is PoolKind.TRENDLINE_LOW for kind, _price in swing_levels(SWINGS, off))
    assert (PoolKind.SWING_LOW, 100.9) in swing_levels(SWINGS, off)


def test_single_swings_can_be_switched_off() -> None:
    config = LiquidityConfig(major_swing_lookback=3, swing_points=False)
    assert swing_levels(SWINGS, config) == sorted(
        [(PoolKind.EQUAL_HIGHS, 102.1), (PoolKind.TRENDLINE_LOW, 100.9)]
    )


def test_major_swing_is_external() -> None:
    rows = quiet(4, 100.0) + [(100.0, 100.2, 97.0, 99.8)] + quiet(4, 100.0)  # a clear low at bar 4
    frame = bars(rows, START, minutes=15)
    (pool,) = swing_pools(frame, NOW, SWING_CONFIG, lookback=1, tolerance=0.3, bar_minutes=15)
    assert (pool.kind, pool.price, pool.scope) == (PoolKind.SWING_LOW, 97.0, Scope.EXTERNAL)
    assert (pool.strength, pool.label) == (POOL_STRENGTH[PoolKind.SWING_LOW], "major swing low")


def test_old_swing_points_expire() -> None:
    config = LiquidityConfig(major_swing_lookback=3, max_pool_age_hours=1)
    frame = bars(SWINGS, START, minutes=15)
    later = utc(2026, 11, 16, 12, 0)  # every swing is more than an hour old
    assert swing_pools(frame, later, config, lookback=1, tolerance=0.3, bar_minutes=15) == []
    assert (
        swing_pools(empty_candles(), later, config, lookback=1, tolerance=0.3, bar_minutes=15) == []
    )


# --------------------------------------------------------------------------- #
# Round numbers and merging
# --------------------------------------------------------------------------- #


def test_round_numbers_around_price() -> None:
    pools = round_number_pools(2654.3, NOW, LiquidityConfig())
    levels = sorted((pool.side.value, pool.price, pool.strength) for pool in pools)
    assert levels == [
        ("buy_side", 2660.0, 0.3),  # an ordinary $10 level
        ("buy_side", 2700.0, 0.5),  # the next $50 level
        ("sell_side", 2650.0, 0.5),  # 2650 is both the nearest $10 and the nearest $50 level
    ]
    assert all(pool.scope is Scope.INTERNAL for pool in pools)
    assert {pool.label for pool in pools} == {
        "round number 2660",
        "round number 2700",
        "round number 2650",
    }


def test_price_sitting_on_a_round_number() -> None:
    pools = round_number_pools(2650.0, NOW, LiquidityConfig())
    assert sorted(pool.price for pool in pools) == [2600.0, 2640.0, 2660.0, 2700.0]


def test_pools_at_the_same_level_merge_into_a_stronger_one() -> None:
    created = utc(2026, 11, 16, 6, 0)
    pdl = make_pool(PoolKind.PREVIOUS_DAY_LOW, Side.SELL_SIDE, 2630.0, utc(2026, 11, 15, 22))
    equal = make_pool(PoolKind.EQUAL_LOWS, Side.SELL_SIDE, 2630.2, created)
    far = make_pool(PoolKind.ASIAN_LOW, Side.SELL_SIDE, 2645.0, created)
    above = make_pool(PoolKind.ASIAN_HIGH, Side.BUY_SIDE, 2630.1, created)
    merged = merge_pools([equal, far, above, pdl], tolerance=0.3)
    assert len(merged) == 3
    combined = next(pool for pool in merged if pool.kind is PoolKind.PREVIOUS_DAY_LOW)
    assert combined.price == 2630.0  # sell-side liquidity rests below the lowest of them
    assert combined.strength == pytest.approx(1.0)
    assert combined.label == "previous day low + equal lows"
    assert combined.sources == (PoolKind.PREVIOUS_DAY_LOW, PoolKind.EQUAL_LOWS)
    assert combined.created == created
    assert {pool.kind for pool in merged} == {
        PoolKind.PREVIOUS_DAY_LOW,
        PoolKind.ASIAN_LOW,
        PoolKind.ASIAN_HIGH,
    }


def test_merged_buy_side_pool_sits_at_the_highest_level() -> None:
    created = utc(2026, 11, 16, 6, 0)
    swing = make_pool(
        PoolKind.SWING_HIGH, Side.BUY_SIDE, 2660.2, created, scope=Scope.INTERNAL, strength=0.4
    )
    asian = make_pool(PoolKind.ASIAN_HIGH, Side.BUY_SIDE, 2660.0, created)
    (merged,) = merge_pools([swing, asian], tolerance=0.3)
    assert (merged.kind, merged.price, merged.scope) == (
        PoolKind.ASIAN_HIGH,
        2660.2,
        Scope.EXTERNAL,
    )
    assert merged.key == ("asian_high", "buy_side", 2660.2)


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #

LOW_POOL = make_pool(PoolKind.ASIAN_LOW, Side.SELL_SIDE, 107.5, utc(2026, 11, 16, 6, 0))
FALL: list[Row] = [
    (109.5, 109.6, 108.2, 108.3),  # 0
    (108.3, 108.4, 107.0, 107.2),  # 1  trades 0.5 below the pool
    (107.2, 107.9, 106.6, 107.8),  # 2  the low, then a close back above 107.5
    (107.8, 110.2, 107.7, 110.0),  # 3
]


def sweep(rows: list[Row], pool: Pool = LOW_POOL, **options: int | float) -> object:
    settings: dict[str, int | float] = {"min_sweep": 0.1, "max_bars_back_inside": 3}
    settings.update(options)
    return detect_sweep(bars(rows, START), pool, **settings)  # type: ignore[arg-type]


def test_sweep_of_sell_side_liquidity() -> None:
    result = sweep(FALL)
    assert result.status is SweepStatus.SWEPT  # type: ignore[attr-defined]
    event = result.event  # type: ignore[attr-defined]
    assert event.direction is Direction.LONG
    assert (event.pierce_index, event.reclaim_index, event.bars_to_reclaim) == (1, 2, 1)
    assert (event.extreme, event.extreme_index) == (106.6, 2)
    assert event.depth == pytest.approx(0.9)
    assert event.pool.swept and not LOW_POOL.swept
    assert event.pierce_time == bars(FALL, START).index[1].to_pydatetime()


def test_a_single_wick_through_the_level_is_a_sweep() -> None:
    rows = [FALL[0], (108.3, 108.4, 106.9, 108.0)]  # dips below and closes back above in one bar
    event = sweep(rows).event  # type: ignore[attr-defined]
    assert (event.pierce_index, event.reclaim_index, event.bars_to_reclaim) == (1, 1, 0)
    assert event.extreme == 106.9


def test_no_sweep_when_price_does_not_go_far_enough() -> None:
    rows = [FALL[0], (108.3, 108.4, 107.45, 108.0)]  # only 0.05 below; 0.10 is required
    assert sweep(rows).status is SweepStatus.NONE  # type: ignore[attr-defined]
    assert sweep(rows, min_sweep=0.05).status is SweepStatus.SWEPT  # type: ignore[attr-defined]


def test_no_sweep_when_price_stays_below_the_level() -> None:
    below = (107.2, 107.4, 106.8, 107.0)
    broken = [FALL[0], FALL[1], below, below, below]  # four bars beyond: the level broke
    result = sweep(broken)
    assert result.status is SweepStatus.FAILED  # type: ignore[attr-defined]
    assert result.event is None and result.pierce_index == 1  # type: ignore[attr-defined]
    assert sweep(broken[:3]).status is SweepStatus.PENDING  # type: ignore[attr-defined]


def test_the_close_back_inside_must_come_within_three_bars() -> None:
    below = (107.2, 107.4, 106.8, 107.0)
    back = (107.0, 107.9, 106.9, 107.8)
    in_time = [FALL[0], FALL[1], below, below, back]  # pierce at 1, back inside at 4
    assert sweep(in_time).event.bars_to_reclaim == 3  # type: ignore[attr-defined]
    too_late = [FALL[0], FALL[1], below, below, below, back]
    assert sweep(too_late).status is SweepStatus.FAILED  # type: ignore[attr-defined]
    assert sweep(too_late, max_bars_back_inside=4).status is SweepStatus.SWEPT  # type: ignore[attr-defined]


def test_a_close_exactly_on_the_level_is_not_back_inside() -> None:
    rows = [FALL[0], (108.3, 108.4, 107.0, 107.5)]
    assert sweep(rows).status is SweepStatus.PENDING  # type: ignore[attr-defined]


def test_bars_before_the_pool_existed_are_ignored() -> None:
    assert sweep(FALL, start=3).status is SweepStatus.NONE  # type: ignore[attr-defined]


def test_sweep_of_buy_side_liquidity_points_short() -> None:
    high_pool = make_pool(PoolKind.ASIAN_HIGH, Side.BUY_SIDE, 112.5, utc(2026, 11, 16, 6, 0))
    event = sweep(mirror(FALL, 110.0), high_pool).event  # type: ignore[attr-defined]
    assert event.direction is Direction.SHORT
    assert (event.pierce_index, event.reclaim_index) == (1, 2)
    assert event.extreme == pytest.approx(113.4)
    assert event.depth == pytest.approx(0.9)

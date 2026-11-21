"""Market structure on hand-made candles: swings, trend, BOS, MSS, displacement."""

from __future__ import annotations

import numpy as np
import pytest

from goldsweep.analysis.structure import (
    Direction,
    DisplacementRule,
    StructureEventKind,
    SwingKind,
    Trend,
    classify_trend,
    displacement_atr,
    find_mss,
    find_swings,
    structure_events,
    swing_indices,
)
from goldsweep.core.settings import StrategyConfig
from tests.helpers import Row, bars, mirror, quiet, utc

START = utc(2026, 11, 16, 7, 0)
RULE = DisplacementRule()  # body >= 1.2 ATR, or >= 70% of a range of at least 1 ATR
BOS, MSS = StructureEventKind.BOS, StructureEventKind.MSS
LONG, SHORT = Direction.LONG, Direction.SHORT

# An uptrend (higher highs and higher lows) that breaks down, then a downtrend that
# breaks back up. Swings with a lookback of 1 are marked on the right.
STORY: list[Row] = [
    (100.0, 101.0, 99.0, 100.5),  # 0
    (100.5, 103.0, 100.0, 102.5),  # 1  swing high 103.0
    (102.5, 102.6, 101.0, 101.2),  # 2
    (101.2, 101.5, 100.8, 101.4),  # 3  swing low 100.8
    (101.4, 105.0, 101.3, 104.5),  # 4  closes above 103.0: first break
    (104.5, 106.0, 104.0, 105.5),  # 5  swing high 106.0 (higher high)
    (105.5, 105.6, 103.0, 103.2),  # 6
    (103.2, 103.5, 102.8, 103.4),  # 7  swing low 102.8 (higher low)
    (103.4, 108.0, 103.3, 107.5),  # 8  closes above 106.0: bullish BOS
    (107.5, 109.0, 107.0, 108.5),  # 9  swing high 109.0 (higher high)
    (108.5, 108.6, 105.0, 105.2),  # 10
    (105.2, 105.5, 104.8, 105.3),  # 11 swing low 104.8 (the last higher low)
    (105.3, 106.5, 105.2, 106.2),  # 12 swing high 106.5 (a lower high)
    (106.2, 106.3, 104.0, 104.2),  # 13 closes below 104.8: bearish MSS
    (104.2, 104.5, 102.0, 102.3),  # 14
    (102.3, 102.4, 101.5, 102.0),  # 15 swing low 101.5
    (102.0, 103.5, 101.9, 103.2),  # 16 swing high 103.5 (the last lower high)
    (103.2, 103.4, 100.0, 100.4),  # 17 closes below 101.5: bearish BOS
    (100.4, 100.9, 99.5, 100.6),  # 18 swing low 99.5 (lower low)
    (100.6, 104.5, 100.5, 104.2),  # 19 closes above 103.5: bullish MSS
]


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #


def test_swing_highs_and_lows_with_lookback_one() -> None:
    swings = find_swings(bars(STORY, START), lookback=1)
    highs = [(s.index, s.price) for s in swings if s.kind is SwingKind.HIGH]
    lows = [(s.index, s.price) for s in swings if s.kind is SwingKind.LOW]
    assert highs == [(1, 103.0), (5, 106.0), (9, 109.0), (12, 106.5), (16, 103.5)]
    assert lows == [(3, 100.8), (7, 102.8), (11, 104.8), (15, 101.5), (18, 99.5)]
    assert all(s.confirmed_index == s.index + 1 for s in swings)
    assert swings[0].time == START.replace(minute=5)  # bar 1 opens five minutes in


def test_larger_lookback_keeps_only_the_major_swings() -> None:
    swings = find_swings(bars(STORY, START), lookback=3)
    assert [(s.kind, s.index) for s in swings] == [(SwingKind.HIGH, 9)]
    assert swings[0].confirmed_index == 12


def test_equal_highs_give_one_pivot_the_first() -> None:
    high = np.array([1.0, 3.0, 3.0, 1.0])
    highs, _lows = swing_indices(high, high - 1.0, lookback=1)
    assert highs.tolist() == [1]


def test_too_few_bars_or_bad_lookback() -> None:
    high = np.array([1.0, 2.0])
    highs, lows = swing_indices(high, high - 1.0, lookback=1)
    assert highs.tolist() == [] and lows.tolist() == []
    with pytest.raises(ValueError, match="lookback"):
        swing_indices(high, high, lookback=0)


def test_a_swing_is_not_known_before_its_confirming_bars_close() -> None:
    frame = bars(STORY, START)
    # The swing high at bar 9 needs bar 10 (lookback 1) before it exists.
    assert 9 not in [s.index for s in find_swings(frame.iloc[:10], lookback=1)]
    assert 9 in [s.index for s in find_swings(frame.iloc[:11], lookback=1)]
    # Adding later bars never changes swings that were already known.
    early = find_swings(frame.iloc[:12], lookback=1)
    assert find_swings(frame, lookback=1)[: len(early)] == early


# --------------------------------------------------------------------------- #
# Trend, BOS and MSS
# --------------------------------------------------------------------------- #


def test_trend_from_the_last_two_highs_and_lows() -> None:
    frame = bars(STORY, START)
    assert classify_trend(find_swings(frame.iloc[:12], 1)) is Trend.UP  # HH + HL
    assert classify_trend(find_swings(frame, 1)) is Trend.DOWN  # LH + LL
    assert classify_trend(find_swings(frame.iloc[:14], 1)) is Trend.RANGE  # lower high, higher low
    assert classify_trend(find_swings(frame.iloc[:5], 1)) is Trend.RANGE  # not enough swings


def test_bos_continues_the_trend_and_mss_reverses_it() -> None:
    events = structure_events(bars(STORY, START), lookback=1)
    assert [(e.kind, e.direction, e.index, e.level) for e in events] == [
        (BOS, LONG, 4, 103.0),  # first break: nothing to reverse yet
        (BOS, LONG, 8, 106.0),  # uptrend continues above the previous higher high
        (MSS, SHORT, 13, 104.8),  # close below the last higher low: buyers lost control
        (BOS, SHORT, 17, 101.5),  # downtrend continues below the previous lower low
        (MSS, LONG, 19, 103.5),  # close above the last lower high: sellers lost control
    ]
    assert events[2].swing_index == 11
    assert events[2].time == bars(STORY, START).index[13].to_pydatetime()


def test_wick_through_a_level_is_not_a_break() -> None:
    rows = list(STORY[:13])
    rows.append((106.2, 106.3, 104.0, 105.0))  # wicks below 104.8 but closes above it
    events = structure_events(bars(rows, START), lookback=1)
    assert [e.kind for e in events] == [BOS, BOS]


def test_structure_events_on_a_flat_market() -> None:
    assert structure_events(bars(quiet(30, 100.0), START), lookback=2) == []


# --------------------------------------------------------------------------- #
# Displacement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("candle", "direction", "expected"),
    [
        ((100.0, 103.0, 99.9, 102.8), LONG, 1.4),  # body 2.8 >= 1.2 x ATR 2.0
        ((100.0, 102.1, 99.9, 102.0), LONG, 1.0),  # body 2.0 is 91% of a 2.2 range (>= 1 ATR)
        ((100.0, 100.5, 100.0, 100.5), LONG, None),  # all body, but a tiny candle
        ((100.0, 104.0, 98.0, 100.5), LONG, None),  # big range, almost no body
        ((102.8, 103.0, 99.9, 100.0), LONG, None),  # strong, but the wrong way
        ((102.8, 103.0, 99.9, 100.0), SHORT, 1.4),
        ((100.0, 100.0, 100.0, 100.0), LONG, None),  # no movement at all
    ],
)
def test_displacement(candle: Row, direction: Direction, expected: float | None) -> None:
    result = displacement_atr(*candle, atr_before=2.0, direction=direction, rule=RULE)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_displacement_needs_a_usable_atr() -> None:
    candle = (100.0, 103.0, 99.9, 102.8)
    assert displacement_atr(*candle, atr_before=float("nan"), direction=LONG, rule=RULE) is None
    assert displacement_atr(*candle, atr_before=0.0, direction=LONG, rule=RULE) is None


def test_displacement_rule_comes_from_config() -> None:
    config = StrategyConfig(
        displacement_atr=2.0, displacement_body_ratio=0.9, displacement_min_range_atr=1.5
    )
    assert DisplacementRule.from_config(config) == DisplacementRule(2.0, 0.9, 1.5)


# --------------------------------------------------------------------------- #
# The MSS that confirms a sweep
# --------------------------------------------------------------------------- #

# Price falls through a level at 107.5, closes back above it, then closes above the
# last lower high (109.8) with a strong candle.
SWEEP: list[Row] = [
    (110.0, 110.4, 109.6, 109.8),  # 0
    (109.0, 109.2, 108.5, 108.7),  # 1
    (108.7, 109.8, 108.6, 109.5),  # 2  swing high 109.8: the last lower high
    (109.5, 109.6, 108.2, 108.3),  # 3
    (108.3, 108.4, 107.0, 107.2),  # 4  trades below 107.5
    (107.2, 107.9, 106.6, 107.8),  # 5  sweep low 106.6, closes back above 107.5
    (107.8, 110.2, 107.7, 110.0),  # 6  body 2.2: closes above 109.8
    (110.0, 110.6, 109.9, 110.4),  # 7
]
ATR_ONE = np.ones(64)


def mss(rows: list[Row], direction: Direction = LONG, **overrides: int) -> object:
    frame = bars(rows, START)
    options = {
        "extreme_index": 5,
        "reclaim_index": 5,
        "earliest_swing_index": 0,
        "window": 12,
    }
    options.update(overrides)
    return find_mss(
        frame, ATR_ONE[: len(frame)], find_swings(frame, 1), direction, rule=RULE, **options
    )


def test_mss_after_a_sweep_of_lows() -> None:
    event = mss(SWEEP)
    assert event is not None
    assert (event.direction, event.index, event.level) == (LONG, 6, 109.8)  # type: ignore[attr-defined]
    assert event.swing_index == 2  # type: ignore[attr-defined]
    assert event.displacement_index == 6  # type: ignore[attr-defined]
    assert event.displacement_atr == pytest.approx(2.2)  # type: ignore[attr-defined]
    assert event.time == bars(SWEEP, START).index[6].to_pydatetime()  # type: ignore[attr-defined]


def test_mss_after_a_sweep_of_highs_is_the_mirror_image() -> None:
    event = mss(mirror(SWEEP, 110.0), SHORT)
    assert event is not None
    assert (event.direction, event.index) == (SHORT, 6)  # type: ignore[attr-defined]
    assert event.level == pytest.approx(110.2)  # type: ignore[attr-defined]


def test_no_mss_without_a_close_through_the_level() -> None:
    rows = list(SWEEP[:6]) + [(107.8, 110.2, 107.7, 109.7), (109.7, 109.9, 109.0, 109.6)]
    assert mss(rows) is None  # wicked above 109.8, never closed above it


def test_no_mss_without_displacement() -> None:
    rows = list(SWEEP[:6]) + [
        (107.8, 109.0, 107.4, 108.5),  # body 0.7 of a 1.6 range
        (108.5, 109.7, 108.0, 109.1),  # body 0.6
        (109.1, 110.3, 108.7, 109.9),  # closes above 109.8, but body is only 0.8 of 1.6
    ]
    assert mss(rows) is None


def test_displacement_may_come_earlier_in_the_leg() -> None:
    rows = list(SWEEP[:6]) + [
        (107.8, 109.7, 107.7, 109.6),  # the strong candle (body 1.8), still below the level
        (109.6, 110.1, 109.4, 109.9),  # a small candle makes the close above 109.8
    ]
    event = mss(rows)
    assert event is not None
    assert (event.index, event.displacement_index) == (7, 6)  # type: ignore[attr-defined]


def test_mss_must_come_within_the_window() -> None:
    late = list(SWEEP[:6]) + quiet(13, 107.9) + [SWEEP[6]]
    assert mss(late) is None  # the close above the level is 14 bars after the reclaim
    assert mss(late, window=14) is not None


def test_mss_needs_a_swing_to_break() -> None:
    assert mss(SWEEP, earliest_swing_index=3) is None  # the lower high is too old to count


def test_the_most_recent_swing_high_is_the_one_that_matters() -> None:
    rows = list(SWEEP[:6]) + [
        (107.8, 108.6, 107.6, 108.4),  # 6  a small bounce: swing high 108.6
        (108.4, 108.5, 107.7, 107.9),  # 7  dips again
        (107.9, 109.4, 107.8, 109.3),  # 8  body 1.4: closes above 108.6, still below 109.8
    ]
    event = mss(rows)
    assert event is not None
    assert (event.index, event.level, event.swing_index) == (8, 108.6, 6)  # type: ignore[attr-defined]

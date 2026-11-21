"""Fair value gaps, inversion gaps and order blocks on hand-made candles."""

from __future__ import annotations

import numpy as np
import pytest

from goldsweep.analysis.structure import Direction, DisplacementRule
from goldsweep.analysis.zones import (
    Zone,
    ZoneKind,
    find_fvgs,
    find_inversions,
    find_order_block,
    first_touch,
)
from tests.helpers import Row, bars, mirror, utc

START = utc(2026, 11, 16, 7, 0)
LONG, SHORT = Direction.LONG, Direction.SHORT
RULE = DisplacementRule()
ONES = np.ones(64)  # an ATR of 1.0 on every bar


def gaps(rows: list[Row], direction: Direction = LONG, **options: object) -> list[Zone]:
    frame = bars(rows, START)
    return find_fvgs(frame, ONES[: len(frame)], direction, **options)  # type: ignore[arg-type]


# The guide's picture of a valid bullish gap: a strong middle candle, and the third
# candle's low stays above the first candle's high.
BULLISH: list[Row] = [
    (100.0, 101.0, 99.5, 100.8),  # candle 1: high 101.0
    (100.8, 104.0, 100.7, 103.8),  # candle 2: body 3.0
    (103.8, 104.5, 102.0, 104.2),  # candle 3: low 102.0
]


# --------------------------------------------------------------------------- #
# Fair value gaps
# --------------------------------------------------------------------------- #


def test_bullish_gap_runs_from_candle_one_high_to_candle_three_low() -> None:
    (gap,) = gaps(BULLISH, rule=RULE)
    assert (gap.kind, gap.direction) == (ZoneKind.FVG, LONG)
    assert (gap.bottom, gap.top) == (101.0, 102.0)
    assert (gap.origin_index, gap.known_index) == (1, 2)
    assert gap.mid == 101.5 and gap.size == 1.0
    assert gap.displacement_atr == pytest.approx(3.0)
    assert gap.time == bars(BULLISH, START).index[1].to_pydatetime()


def test_no_gap_when_the_third_candle_overlaps_the_first() -> None:
    overlapping = [BULLISH[0], BULLISH[1], (103.8, 104.5, 100.9, 104.2)]  # low 100.9 <= 101.0
    assert gaps(overlapping, rule=RULE) == []
    touching = [BULLISH[0], BULLISH[1], (103.8, 104.5, 101.0, 104.2)]  # low exactly 101.0
    assert gaps(touching, rule=RULE) == []


def test_bearish_gap_runs_from_candle_three_high_to_candle_one_low() -> None:
    (gap,) = gaps(mirror(BULLISH, 102.0), SHORT, rule=RULE)
    assert gap.direction is SHORT
    assert (gap.bottom, gap.top) == (102.0, 103.0)  # candle 3 high .. candle 1 low
    assert gaps(mirror(BULLISH, 102.0), LONG) == []  # and it is not a bullish gap


def test_gap_left_by_a_weak_candle_is_not_valid() -> None:
    weak = [
        (100.0, 100.5, 99.5, 100.2),
        (101.0, 101.3, 100.9, 101.1),  # opens with a jump but has almost no body
        (101.2, 101.6, 101.0, 101.4),
    ]
    assert len(gaps(weak)) == 1  # the three candles do leave a gap ...
    assert gaps(weak, rule=RULE) == []  # ... but nothing displaced price, so it is rejected


def test_gap_too_small_to_matter_is_dropped() -> None:
    thin = [BULLISH[0], BULLISH[1], (103.8, 104.5, 101.03, 104.2)]  # a 0.03 gap
    assert len(gaps(thin, rule=RULE)) == 1
    assert gaps(thin, rule=RULE, min_size_atr=0.05) == []
    assert len(gaps(BULLISH, rule=RULE, min_size_atr=0.05)) == 1


def test_gap_exists_only_once_the_third_candle_has_closed() -> None:
    assert gaps(BULLISH[:2], rule=RULE) == []


def test_several_gaps_and_the_search_bounds() -> None:
    rows = BULLISH + [
        (104.2, 104.6, 103.9, 104.4),  # 3
        (104.4, 107.5, 104.3, 107.3),  # 4  second strong candle
        (107.3, 108.0, 105.5, 107.8),  # 5  low 105.5 > high[3] 104.6: second gap
    ]
    found = gaps(rows, rule=RULE)
    assert [(g.origin_index, g.bottom, g.top) for g in found] == [
        (1, 101.0, 102.0),
        (4, 104.6, 105.5),
    ]
    assert [g.origin_index for g in gaps(rows, rule=RULE, first=2)] == [4]
    assert [g.origin_index for g in gaps(rows, rule=RULE, last=3)] == [1]


def test_entry_prices_inside_a_zone() -> None:
    (gap,) = gaps(BULLISH, rule=RULE)
    assert gap.price_for("fvg_top") == 102.0
    assert gap.price_for("fvg_mid") == 101.5
    assert gap.price_for("fvg_bottom") == 101.0
    with pytest.raises(ValueError, match="market"):
        gap.price_for("market")


def test_first_touch_finds_the_bar_that_trades_back_into_the_zone() -> None:
    rows = BULLISH + [
        (104.2, 104.8, 103.0, 104.5),  # 3  stays above the gap
        (104.5, 104.6, 101.4, 102.5),  # 4  trades down to 101.4: through the 50% level
    ]
    frame = bars(rows, START)
    (gap,) = find_fvgs(frame, ONES[: len(frame)], LONG, rule=RULE)
    assert first_touch(frame, gap, gap.mid, start=gap.known_index + 1) == 4
    assert first_touch(frame, gap, gap.bottom, start=gap.known_index + 1) is None
    assert first_touch(frame.iloc[:4], gap, gap.mid, start=gap.known_index + 1) is None

    short_frame = bars(mirror(rows, 102.0), START)
    (short_gap,) = find_fvgs(short_frame, ONES[: len(short_frame)], SHORT, rule=RULE)
    assert first_touch(short_frame, short_gap, short_gap.mid, start=3) == 4


# --------------------------------------------------------------------------- #
# Inversion fair value gaps
# --------------------------------------------------------------------------- #

# Sellers leave a bearish gap between 103.0 and 104.0. Price falls, turns, and closes
# straight back above the gap: it has failed and now works as support.
INVERSION: list[Row] = [
    (105.5, 105.8, 104.0, 104.2),  # 0  candle 1: low 104.0
    (104.2, 104.3, 101.5, 101.8),  # 1  strong bearish candle
    (101.8, 103.0, 101.2, 101.5),  # 2  candle 3: high 103.0 -> bearish gap 103.0 .. 104.0
    (101.5, 101.9, 100.5, 101.6),  # 3  the low of the move
    (101.6, 104.9, 101.5, 104.7),  # 4  closes above 104.0: the gap inverts
    (104.7, 105.2, 104.4, 105.0),  # 5
]


def inversions(rows: list[Row], direction: Direction = LONG, after: int = 3) -> list[Zone]:
    frame = bars(rows, START)
    return find_inversions(
        frame, ONES[: len(frame)], direction, first=1, last=len(frame) - 2, after=after
    )


def test_bearish_gap_that_price_closes_above_becomes_bullish() -> None:
    (zone,) = inversions(INVERSION)
    assert (zone.kind, zone.direction) == (ZoneKind.IFVG, LONG)
    assert (zone.bottom, zone.top) == (103.0, 104.0)
    assert (zone.origin_index, zone.known_index) == (1, 4)  # usable from the bar that closed above


def test_a_wick_through_the_gap_does_not_invert_it() -> None:
    rows = INVERSION[:4] + [(101.6, 104.9, 101.5, 103.6), (103.6, 103.9, 103.0, 103.5)]
    assert inversions(rows) == []  # traded above 104.0 but closed back inside the gap


def test_an_old_inversion_is_ignored() -> None:
    assert (
        inversions(INVERSION, after=5) == []
    )  # it inverted at bar 4, before the move we care about


def test_bullish_gap_that_price_closes_below_becomes_bearish() -> None:
    (zone,) = inversions(mirror(INVERSION, 103.0), SHORT)
    assert (zone.kind, zone.direction) == (ZoneKind.IFVG, SHORT)
    assert (zone.bottom, zone.top) == (102.0, 103.0)
    assert inversions(mirror(INVERSION, 103.0), LONG) == []


# --------------------------------------------------------------------------- #
# Order blocks
# --------------------------------------------------------------------------- #

BLOCK: list[Row] = [
    (102.0, 102.3, 101.4, 101.6),  # 0  bearish
    (101.6, 101.9, 101.0, 101.7),  # 1  bullish
    (101.7, 101.8, 100.4, 100.6),  # 2  bearish: the last down candle before the move
    (100.6, 100.9, 100.2, 100.8),  # 3  bullish
    (100.8, 103.5, 100.7, 103.3),  # 4  displacement
]


def test_order_block_is_the_last_opposite_candle_before_displacement() -> None:
    block = find_order_block(bars(BLOCK, START), LONG, displacement_index=4, earliest=0)
    assert block is not None
    assert (block.kind, block.direction) == (ZoneKind.ORDER_BLOCK, LONG)
    assert (block.origin_index, block.known_index) == (2, 4)
    assert (block.bottom, block.top) == (100.4, 101.8)
    assert block.mid == pytest.approx(101.1)


def test_no_order_block_inside_the_search_range() -> None:
    assert find_order_block(bars(BLOCK, START), LONG, displacement_index=4, earliest=3) is None


def test_bearish_order_block_is_the_last_up_candle() -> None:
    block = find_order_block(
        bars(mirror(BLOCK, 102.0), START), SHORT, displacement_index=4, earliest=0
    )
    assert block is not None
    assert (block.direction, block.origin_index) == (SHORT, 2)
    assert (block.bottom, block.top) == (pytest.approx(102.2), pytest.approx(103.6))

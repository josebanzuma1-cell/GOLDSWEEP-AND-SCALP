"""Micro liquidity and its sweeps (S1, prompt section 4.2)."""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import Settings
from goldsweep.scalping.levels import HIGH, LOW, SWING, TWIN, Level, MicroLiquidity, SessionRanges
from tests.helpers import utc

Row = tuple[float, float, float, float]  # (open, high, low, close)


def liquidity(**changes: float) -> MicroLiquidity:
    values = {
        "swing_bars": 1,
        "micro_lookback": 15,
        "sweep_min_atr": 0.05,
        "twin_tolerance_atr": 0.05,
    } | changes
    return MicroLiquidity(**values)  # type: ignore[arg-type]


def feed(micro: MicroLiquidity, rows: list[Row], *, first: int = 0, atr: float = 1.0) -> list:
    """Every bar's completed sweeps, ATR 1.0 throughout."""
    return [
        micro.update(first + i, high, low, close, atr)
        for i, (_open, high, low, close) in enumerate(rows)
    ]


#: Highs 10, 11, 12, 11, 10.5: a swing high of 12 at bar 2, confirmed by bar 3.
PEAK: list[Row] = [
    (9.8, 10.0, 9.5, 9.9),
    (9.9, 11.0, 9.8, 10.8),
    (10.8, 12.0, 10.6, 11.0),
    (11.0, 11.0, 10.2, 10.4),
]


def highs(micro: MicroLiquidity) -> list[Level]:
    return [level for level in micro.levels if level.side == HIGH]


def test_a_swing_point_counts_once_its_bars_have_closed() -> None:
    micro = liquidity()
    feed(micro, PEAK[:3])
    assert highs(micro) == []  # bar 2 is not confirmed while bar 3 is open
    feed(micro, PEAK[3:], first=3)
    assert highs(micro) == [Level(12.0, HIGH, SWING, 2, 4, 2 + 15)]


def test_a_sweep_that_closes_back_inside_on_the_same_bar() -> None:
    micro = liquidity()
    feed(micro, PEAK)
    (sweep,) = micro.update(4, 12.2, 10.9, 11.5, 1.0)
    assert (sweep.side, sweep.extreme, sweep.bar, sweep.reclaimed) == (HIGH, 12.2, 4, 4)
    assert sweep.level.price == 12.0 and sweep.bar_low == 10.9 and sweep.kinds == SWING
    assert highs(micro) == []  # used once


def test_a_sweep_that_closes_back_inside_on_the_next_bar() -> None:
    micro = liquidity()
    feed(micro, PEAK)
    assert micro.update(4, 12.3, 11.4, 12.1, 1.0) == []  # closed above: not yet
    (sweep,) = micro.update(5, 12.4, 11.2, 11.8, 1.0)
    assert (sweep.extreme, sweep.bar, sweep.reclaimed, sweep.bar_low) == (12.4, 4, 5, 11.4)


def test_a_break_that_holds_is_no_sweep_and_the_level_is_gone() -> None:
    micro = liquidity()
    feed(micro, PEAK)
    assert micro.update(4, 12.3, 11.4, 12.1, 1.0) == []
    assert micro.update(5, 12.6, 12.0, 12.5, 1.0) == []
    assert highs(micro) == []


def test_a_shallow_poke_is_no_sweep_and_the_level_stays() -> None:
    micro = liquidity()
    feed(micro, PEAK)
    assert micro.update(4, 12.03, 11.0, 11.5, 1.0) == []  # less than 0.05 ATR beyond
    assert [level.price for level in highs(micro)] == [12.0]


def test_levels_expire_after_the_lookback() -> None:
    micro = liquidity(micro_lookback=4)
    feed(micro, PEAK)
    feed(micro, [(10.4, 10.6, 10.1, 10.3)] * 3, first=4)  # bars 4, 5, 6
    assert 12.0 in [level.price for level in highs(micro)]  # pivot 2 + 4: counts on bar 6
    micro.update(7, 10.6, 10.1, 10.3, 1.0)
    assert 12.0 not in [level.price for level in highs(micro)]  # not on bar 7


def test_twin_bars_are_a_level_and_lows_mirror_highs() -> None:
    micro = liquidity()
    rows: list[Row] = [(10.0, 10.5, 9.0, 9.4), (9.4, 10.0, 9.02, 9.6)]
    feed(micro, rows)
    twins = [level for level in micro.levels if level.kind == TWIN]
    assert twins == [Level(9.0, LOW, TWIN, 1, 2, 16)]  # lows 9.00 and 9.02: within 0.05
    (sweep,) = micro.update(2, 9.7, 8.9, 9.3, 1.0)  # under 9.00 by 0.10, closed back above
    assert (sweep.side, sweep.extreme, sweep.bar_low, sweep.kinds) == (LOW, 8.9, 9.7, TWIN)


def test_no_sweeps_or_twins_without_an_atr() -> None:
    micro = liquidity()
    feed(micro, PEAK)
    assert micro.update(4, 15.0, 10.0, 11.0, None) == []
    assert [level.price for level in highs(micro)] == [12.0]
    with pytest.raises(ValueError, match="at least 1"):
        liquidity(swing_bars=0)


def test_session_ranges_become_levels_when_their_window_ends(settings: Settings) -> None:
    ranges = SessionRanges(SessionCalculator(settings.config.sessions), names=("asia",))
    # Tokyo 09:00-15:00 is 00:00-06:00 UTC on Tuesday 2026-11-17.
    start = utc(2026, 11, 17, 5, 57)
    rows = [(2001.0, 2002.0), (2003.5, 2000.5), (2002.5, 2001.5), (2004.0, 2001.0)]
    found = []
    for i, (high, low) in enumerate(rows):
        found.append(ranges.update(i, start + dt.timedelta(minutes=i), high, low))
    assert found[:3] == [[], [], []]
    assert found[3] == [
        Level(2003.5, HIGH, "asia", 2, 3, 3 + 720),
        Level(2000.5, LOW, "asia", 2, 3, 3 + 720),
    ]


def test_a_range_that_ended_long_ago_is_not_used(settings: Settings) -> None:
    ranges = SessionRanges(SessionCalculator(settings.config.sessions), names=("asia",))
    assert ranges.update(0, utc(2026, 11, 17, 5, 59), 2002.0, 2001.0) == []
    assert ranges.update(1, utc(2026, 11, 17, 7, 30), 2003.0, 2000.0) == []  # 90 min later

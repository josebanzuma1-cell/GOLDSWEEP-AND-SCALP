"""Candle Range Theory and the higher-timeframe bias."""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.bias import Bias, compute_bias
from goldsweep.analysis.crt import active_crt, crt_pools, detect_crt, range_candles
from goldsweep.analysis.liquidity import PoolKind, Side
from goldsweep.analysis.structure import Direction, Trend
from goldsweep.core.settings import BiasConfig, DayBoundaryConfig
from goldsweep.core.types import Timeframe
from goldsweep.data.candles import empty_candles
from tests.helpers import Row, bars, make_candles, utc
from tests.test_structure import STORY

H4 = Timeframe.H4
LONG, SHORT = Direction.LONG, Direction.SHORT
FIRST_CANDLE = utc(2026, 11, 16, 2, 0)  # the 21:00 New York four-hour candle
FOUR_HOURS = dt.timedelta(hours=4)

RANGE: Row = (100.0, 105.0, 98.0, 103.0)  # candle 1: the range is 98 .. 105
TAKES_LOW: Row = (103.0, 104.0, 96.0, 101.0)  # candle 2: trades to 96, closes back at 101


def four_hour(rows: list[Row]):  # noqa: ANN201 - a candle frame
    return bars(rows, FIRST_CANDLE, minutes=240)


# --------------------------------------------------------------------------- #
# Candle Range Theory
# --------------------------------------------------------------------------- #


def test_candle_that_takes_the_low_and_closes_back_inside_is_bullish() -> None:
    (setup,) = detect_crt(four_hour([RANGE, TAKES_LOW]), H4)
    assert setup.direction is LONG
    assert (setup.range_low, setup.range_high) == (98.0, 105.0)
    assert setup.sweep_extreme == 96.0
    assert setup.target == 105.0  # the other side of the range
    assert setup.invalidation == 96.0
    assert setup.range_time == FIRST_CANDLE
    assert setup.sweep_time == FIRST_CANDLE + FOUR_HOURS
    assert setup.confirmed_at == FIRST_CANDLE + 2 * FOUR_HOURS  # known when candle 2 closes
    assert "took the range low" in setup.describe() and "105" in setup.describe()


def test_candle_that_takes_the_high_and_closes_back_inside_is_bearish() -> None:
    takes_high: Row = (103.0, 108.0, 102.0, 104.0)
    (setup,) = detect_crt(four_hour([RANGE, takes_high]), H4)
    assert setup.direction is SHORT
    assert (setup.sweep_extreme, setup.target) == (108.0, 98.0)
    assert "took the range high" in setup.describe()


@pytest.mark.parametrize(
    "second",
    [
        (103.0, 104.0, 99.0, 101.0),  # inside the range: nothing was taken
        (103.0, 107.0, 96.0, 101.0),  # took both sides: no direction
        (103.0, 104.0, 96.0, 97.0),  # took the low but closed below it: a break, not a raid
        (103.0, 108.0, 102.0, 106.0),  # took the high and closed above it
    ],
)
def test_candles_that_are_not_a_crt(second: Row) -> None:
    assert detect_crt(four_hour([RANGE, second]), H4) == []


def test_each_candle_is_judged_against_the_one_before_it() -> None:
    third: Row = (101.0, 106.0, 100.0, 102.0)  # takes candle 2's high (104), closes back inside
    setups = detect_crt(four_hour([RANGE, TAKES_LOW, third]), H4)
    assert [setup.direction for setup in setups] == [LONG, SHORT]
    assert setups[1].range_high == 104.0 and setups[1].target == 96.0


def test_range_of_the_last_closed_candle_becomes_two_pools() -> None:
    high, low = crt_pools(four_hour([RANGE, TAKES_LOW]), H4)
    assert (high.kind, high.side, high.price) == (PoolKind.CRT_HIGH, Side.BUY_SIDE, 104.0)
    assert (low.kind, low.side, low.price) == (PoolKind.CRT_LOW, Side.SELL_SIDE, 96.0)
    closed_at = FIRST_CANDLE + 2 * FOUR_HOURS
    assert (high.created, high.expires) == (closed_at, closed_at + FOUR_HOURS)
    assert high.label == "H4 CRT range high" and high.is_external
    assert crt_pools(empty_candles(), H4) == []


def test_crt_stays_in_play_until_target_invalidation_or_old_age() -> None:
    (setup,) = detect_crt(four_hour([RANGE, TAKES_LOW]), H4)
    after = setup.confirmed_at

    def lower(rows: list[Row]):  # noqa: ANN202 - five-minute bars after the CRT is confirmed
        return bars(rows, after)

    quiet = lower([(101.0, 102.0, 100.5, 101.5)] * 3)
    now = after + dt.timedelta(minutes=15)
    assert active_crt([setup], quiet, now, max_age_candles=2) is setup
    assert active_crt([setup], empty_candles(), now, max_age_candles=2) is setup
    assert (
        active_crt([setup], lower([(101.0, 105.2, 100.5, 105.0)]), now, max_age_candles=2) is None
    )
    assert active_crt([setup], lower([(101.0, 102.0, 95.5, 96.5)]), now, max_age_candles=2) is None
    much_later = after + 3 * FOUR_HOURS
    assert active_crt([setup], quiet, much_later, max_age_candles=2) is None
    assert active_crt([setup], quiet, after - dt.timedelta(minutes=5), max_age_candles=2) is None
    assert active_crt([], quiet, now, max_age_candles=2) is None


def test_bearish_crt_plays_out_downwards() -> None:
    takes_high: Row = (103.0, 108.0, 102.0, 104.0)
    (setup,) = detect_crt(four_hour([RANGE, takes_high]), H4)
    now = setup.confirmed_at + dt.timedelta(minutes=10)
    hit = bars([(104.0, 104.5, 97.9, 98.5)], setup.confirmed_at)
    failed = bars([(104.0, 108.4, 103.5, 108.0)], setup.confirmed_at)
    assert active_crt([setup], hit, now, max_age_candles=2) is None
    assert active_crt([setup], failed, now, max_age_candles=2) is None


def test_range_candles_are_closed_and_anchored_to_new_york() -> None:
    hourly = make_candles(utc(2026, 11, 16, 2, 0), 9, minutes=60)  # 02:00 .. 10:00 UTC
    boundary = DayBoundaryConfig()
    ranges = range_candles(hourly, H4, boundary, utc(2026, 11, 16, 10, 30))
    assert list(ranges.index) == [utc(2026, 11, 16, 2, 0), utc(2026, 11, 16, 6, 0)]
    still_forming = range_candles(hourly, H4, boundary, utc(2026, 11, 16, 9, 30))
    assert list(still_forming.index) == [utc(2026, 11, 16, 2, 0)]


# --------------------------------------------------------------------------- #
# Bias
# --------------------------------------------------------------------------- #

WEIGHTS = BiasConfig()  # structure 0.5, location 0.2, news 0.3, threshold 0.25
UPTREND = bars(STORY[:12], utc(2026, 11, 12, 0, 0), minutes=60)
DOWNTREND = bars(STORY, utc(2026, 11, 12, 0, 0), minutes=60)
MIXED = bars(STORY[:14], utc(2026, 11, 12, 0, 0), minutes=60)


def bias(hourly, price: float, news: float = 0.0, mid: float | None = 100.0):  # noqa: ANN001, ANN201
    return compute_bias(
        hourly, price=price, previous_mid=mid, news_bias=news, config=WEIGHTS, swing_lookback=1
    )


def test_uptrend_in_discount_is_a_long_bias() -> None:
    result = bias(UPTREND, price=99.0)
    assert (result.bias, result.trend) == (Bias.LONG, Trend.UP)
    assert (result.structure, result.location, result.news) == (1.0, 1.0, 0.0)
    assert result.score == pytest.approx(0.7)
    assert result.previous_mid == 100.0


def test_premium_weakens_a_long_bias_and_bad_news_can_cancel_it() -> None:
    assert bias(UPTREND, price=101.0).score == pytest.approx(0.3)
    assert bias(UPTREND, price=101.0).bias is Bias.LONG
    cancelled = bias(UPTREND, price=101.0, news=-1.0)
    assert cancelled.score == pytest.approx(0.0)
    assert cancelled.bias is Bias.NEUTRAL


def test_downtrend_in_premium_is_a_short_bias() -> None:
    result = bias(DOWNTREND, price=101.0)
    assert (result.bias, result.trend, result.score) == (
        Bias.SHORT,
        Trend.DOWN,
        pytest.approx(-0.7),
    )


def test_range_needs_news_to_tip_the_balance() -> None:
    assert bias(MIXED, price=99.0).bias is Bias.NEUTRAL  # 0.2 from location alone
    assert bias(MIXED, price=99.0, news=0.5).bias is Bias.LONG  # 0.2 + 0.15
    assert bias(MIXED, price=101.0, news=-0.5).bias is Bias.SHORT


def test_unknown_midpoint_and_out_of_range_news() -> None:
    no_mid = bias(UPTREND, price=99.0, mid=None)
    assert (no_mid.location, no_mid.score) == (0.0, pytest.approx(0.5))
    assert bias(UPTREND, price=100.0).location == 0.0  # exactly on the midpoint
    assert bias(MIXED, price=99.0, news=7.0).news == 1.0
    assert bias(MIXED, price=99.0, news=-7.0).news == -1.0
    assert bias(empty_candles(), price=99.0).trend is Trend.RANGE


def test_which_trades_a_bias_allows() -> None:
    assert Bias.LONG.allows(LONG) and not Bias.LONG.allows(SHORT)
    assert Bias.SHORT.allows(SHORT) and not Bias.SHORT.allows(LONG)
    assert Bias.NEUTRAL.allows(LONG) and Bias.NEUTRAL.allows(SHORT)

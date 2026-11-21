"""Position sizing and the pre-trade risk limits (spec section 6 and section 10)."""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.core.settings import DayBoundaryConfig, NewsConfig, RiskConfig
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.risk.risk_manager import (
    NewsEvent,
    RiskManager,
    blackout_event,
    position_size,
)
from tests.helpers import utc

GOLD = default_gold_spec()  # 100 oz per lot: a 1.00 move on 1.00 lot is 100.00
MONDAY_LONDON = utc(2026, 11, 16, 7, 30)
HOUR = dt.timedelta(hours=1)


def manager(**changes: object) -> RiskManager:
    risk = RiskConfig().model_copy(update=changes)
    return RiskManager(risk, NewsConfig(), DayBoundaryConfig(), equity=10_000.0)


def check(rm: RiskManager, when: dt.datetime = MONDAY_LONDON, **kwargs: object):  # noqa: ANN201
    arguments = {"session": "london", "spread_points": 20.0, "median_spread_points": 20.0}
    arguments.update(kwargs)
    return rm.check(when, **arguments)  # type: ignore[arg-type]


def trade(rm: RiskManager, when: dt.datetime, pnl: float, session: str = "london") -> None:
    """One whole trade: order, fill, close."""
    rm.order_placed()
    rm.position_opened(when, session)
    rm.book(when, pnl)
    rm.position_closed(when, pnl)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def test_size_risks_the_configured_share_rounded_down() -> None:
    # 0.5% of 10,000 = 50.00; a 2.81 stop costs 281.00 per lot -> 0.177 lots -> 0.17.
    size = position_size(10_000.0, 2030.0, 2027.19, GOLD, 0.5)
    assert size.ok and size.lots == 0.17
    assert size.risk_amount == pytest.approx(0.17 * 281.0)
    assert size.risk_amount <= 50.0  # never more than allowed


def test_size_uses_the_brokers_tick_value_not_a_guess() -> None:
    tiny_contract = default_gold_spec(tick_value=0.1)  # a 10-ounce contract
    assert position_size(10_000.0, 2030.0, 2027.19, tiny_contract, 0.5).lots == 1.77


def test_stop_too_wide_for_the_minimum_lot_means_no_trade() -> None:
    size = position_size(1_000.0, 2030.0, 2010.0, GOLD, 0.5)  # 0.01 lot would risk 2%
    assert not size.ok and size.lots == 0.0
    assert "too wide" in (size.reason or "") and "2.00%" in (size.reason or "")


def test_size_edge_cases() -> None:
    assert position_size(10_000.0, 2030.0, 2030.0, GOLD, 0.5).reason == (
        "the stop-loss is at the entry price"
    )
    assert position_size(0.0, 2030.0, 2029.0, GOLD, 0.5).reason == "no equity left"
    huge = position_size(100_000_000.0, 2030.0, 2029.0, GOLD, 2.0)
    assert huge.lots == GOLD.volume_max  # clamped to the broker's maximum
    whole_lots = default_gold_spec(volume_step=1.0, volume_min=1.0)
    assert position_size(1_000_000.0, 2030.0, 2027.0, whole_lots, 0.5).lots == 16.0


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #


def test_a_fresh_manager_allows_a_trade() -> None:
    decision = check(manager())
    assert decision.allowed and decision.reasons == ()


def test_one_position_at_a_time() -> None:
    rm = manager()
    rm.order_placed()  # a pending entry order already holds the slot
    assert "1 position(s) or entry order(s) already open" in check(rm).reasons
    rm.order_cancelled()
    assert check(rm).allowed
    assert rm.open_positions == 0


def test_max_trades_per_day_and_per_session() -> None:
    rm = manager()
    trade(rm, MONDAY_LONDON, 10.0)
    trade(rm, MONDAY_LONDON + HOUR, 10.0)
    assert "already 2 trades this london session" in check(rm).reasons
    assert check(rm, session="newyork").allowed
    trade(rm, MONDAY_LONDON + 6 * HOUR, 10.0, session="newyork")
    assert "already 3 trades this trading day" in check(rm, session="newyork").reasons
    tuesday = MONDAY_LONDON + 24 * HOUR
    assert check(rm, tuesday).allowed  # a new trading day


def test_daily_loss_limit_stops_trading_until_the_next_day() -> None:
    rm = manager()
    trade(rm, MONDAY_LONDON, -100.0)
    trade(rm, MONDAY_LONDON + HOUR, 50.0)
    assert check(rm, session="newyork").allowed  # -50 is not -2%
    rm.book(MONDAY_LONDON + 2 * HOUR, -150.0)  # -200 = -2% of the day's 10,000
    reasons = check(rm, session="newyork").reasons
    assert "daily loss limit of 2% reached" in reasons
    next_day = MONDAY_LONDON + 24 * HOUR
    assert check(rm, next_day).allowed
    assert rm.equity == pytest.approx(9_800.0)


def test_trading_day_ends_at_17_00_new_york() -> None:
    rm = manager()
    rm.book(utc(2026, 11, 16, 21, 0), -200.0)  # Monday 16:00 New York
    assert not check(rm, utc(2026, 11, 16, 21, 30)).allowed
    assert check(rm, utc(2026, 11, 16, 22, 30)).allowed  # Monday 17:30 New York: Tuesday


def test_weekly_loss_limit() -> None:
    rm = manager(daily_loss_limit_pct=10.0)
    rm.book(MONDAY_LONDON, -300.0)
    rm.book(MONDAY_LONDON + 24 * HOUR, -250.0)  # -550 over the week, -5.5%
    thursday = MONDAY_LONDON + 72 * HOUR
    assert "weekly loss limit of 5% reached" in check(rm, thursday).reasons
    next_monday = MONDAY_LONDON + 7 * 24 * HOUR
    assert check(rm, next_monday).allowed


def test_three_losses_in_a_row_pause_trading_for_a_day() -> None:
    rm = manager(max_trades_per_day=10, max_trades_per_session=10, daily_loss_limit_pct=20.0)
    for number in range(3):
        trade(rm, MONDAY_LONDON + number * HOUR, -10.0)
    paused_from = MONDAY_LONDON + 2 * HOUR
    assert rm.paused_until == paused_from + 24 * HOUR
    assert "paused after 3 losses in a row" in check(rm, paused_from + HOUR).reasons[0]
    assert check(rm, paused_from + 24 * HOUR).allowed


def test_a_win_resets_the_losing_streak() -> None:
    rm = manager(max_trades_per_day=10, max_trades_per_session=10, daily_loss_limit_pct=20.0)
    for pnl in (-10.0, -10.0, 5.0, -10.0, -10.0):
        trade(rm, MONDAY_LONDON, pnl)
    assert rm.paused_until is None


def test_spread_filter() -> None:
    rm = manager()
    assert check(rm, spread_points=40.0, median_spread_points=20.0).allowed  # exactly 2x
    blocked = check(rm, spread_points=41.0, median_spread_points=20.0)
    assert "spread 41 points is above 40 (2x the usual 20)" in blocked.reasons
    assert check(rm, spread_points=99.0, median_spread_points=None).allowed  # nothing to compare


def test_news_blackout() -> None:
    cpi = NewsEvent(utc(2026, 11, 16, 13, 30), "US CPI")
    speech = NewsEvent(utc(2026, 11, 16, 13, 30), "Minor speech", impact="low")
    rm = manager()
    assert (
        "news blackout: US CPI at 13:30 UTC"
        in check(rm, utc(2026, 11, 16, 13, 0), events=[cpi]).reasons
    )
    assert not check(rm, utc(2026, 11, 16, 13, 45), events=[cpi]).allowed  # 15 min after
    assert check(rm, utc(2026, 11, 16, 12, 59), events=[cpi]).allowed  # 31 min before
    assert check(rm, utc(2026, 11, 16, 13, 46), events=[cpi]).allowed
    assert check(rm, utc(2026, 11, 16, 13, 30), events=[speech]).allowed  # low impact
    assert blackout_event(utc(2026, 11, 16, 13, 30), [], before_min=30, after_min=15) is None


def test_sizing_follows_realised_equity() -> None:
    rm = manager()
    rm.book(MONDAY_LONDON, 10_000.0)  # equity doubles
    assert rm.size(2030.0, 2027.19, GOLD).lots == 0.35


def test_equity_must_start_positive() -> None:
    with pytest.raises(ValueError, match="equity"):
        RiskManager(RiskConfig(), NewsConfig(), DayBoundaryConfig(), equity=0.0)

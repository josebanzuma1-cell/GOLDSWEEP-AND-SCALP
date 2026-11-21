"""The offline mock broker: repeatable prices, closed bars only, symbol resolution."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import numpy as np
import pytest

from goldsweep.core.clock import ManualClock
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import (
    AccountType,
    BrokerConnectionError,
    BrokerError,
    SymbolNotFoundError,
    TradeMode,
    rank_gold_symbols,
)
from goldsweep.data.mock_broker import (
    MockBroker,
    default_gold_spec,
    generate_m1_candles,
    market_open_mask,
)
from tests.helpers import assert_candles_equal, make_candles, utc

CANDIDATES = ("XAUUSD", "XAUUSD.m", "XAUUSDm", "GOLD")
NOW = utc(2026, 11, 18, 10, 2, 30)  # a Wednesday, mid-morning in London
M1, M5, H1 = Timeframe.M1, Timeframe.M5, Timeframe.H1


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def broker(clock: ManualClock) -> MockBroker:
    mock = MockBroker(clock=clock, history_days=10, future_days=3)
    mock.connect()
    return mock


def connected(clock: ManualClock, **kwargs: Any) -> MockBroker:
    mock = MockBroker(clock=clock, history_days=3, future_days=1, **kwargs)
    mock.connect()
    return mock


# --------------------------------------------------------------------------- #
# Connection, account, terminal
# --------------------------------------------------------------------------- #


def test_must_connect_first(clock: ManualClock) -> None:
    mock = MockBroker(clock=clock, history_days=1, future_days=1)
    assert not mock.is_connected()
    with pytest.raises(BrokerConnectionError):
        mock.account_info()
    with pytest.raises(BrokerConnectionError):
        mock.get_rates("XAUUSD", M5, 5)


def test_context_manager_connects_and_shuts_down(clock: ManualClock) -> None:
    mock = MockBroker(clock=clock, history_days=1, future_days=1)
    with mock as opened:
        assert opened is mock and mock.is_connected()
    assert not mock.is_connected()


def test_account_is_a_demo_account(broker: MockBroker) -> None:
    account = broker.account_info()
    assert account.account_type is AccountType.DEMO and account.is_demo
    assert account.currency == "USD"
    assert account.balance == account.equity == account.margin_free == 10_000.0
    assert account.margin_level is None
    assert broker.terminal_info().connected


def test_server_time_is_plain_confirmed_utc(broker: MockBroker) -> None:
    state = broker.server_time
    assert state.confirmed and state.source == "mock" and state.rule.spec == "UTC"


# --------------------------------------------------------------------------- #
# Symbol resolution (shared with the MT5 client)
# --------------------------------------------------------------------------- #


def test_default_symbol_is_resolved(broker: MockBroker) -> None:
    spec = broker.resolve_symbol(CANDIDATES)
    assert spec.name == "XAUUSD"
    assert (spec.digits, spec.point, spec.contract_size) == (2, 0.01, 100.0)
    assert spec.value_per_price_unit == pytest.approx(100.0)
    assert spec.tradable


@pytest.mark.parametrize("name", ["XAUUSD.m", "XAUUSDm", "GOLD"])
def test_broker_specific_names_are_resolved(clock: ManualClock, name: str) -> None:
    mock = connected(clock, symbols=[default_gold_spec(name)])
    assert mock.resolve_symbol(CANDIDATES).name == name


def test_candidate_order_decides_when_several_exist(clock: ManualClock) -> None:
    mock = connected(clock, symbols=[default_gold_spec("GOLD"), default_gold_spec("XAUUSD.m")])
    assert mock.resolve_symbol(CANDIDATES).name == "XAUUSD.m"
    assert mock.resolve_symbol(("GOLD", "XAUUSD.m")).name == "GOLD"


def test_unlisted_name_is_found_by_search(clock: ManualClock) -> None:
    mock = connected(
        clock,
        symbols=[default_gold_spec("XAUUSD-ECN-raw"), default_gold_spec("XAUUSD.pro")],
    )
    assert mock.resolve_symbol(CANDIDATES).name == "XAUUSD.pro"  # the plainer name


def test_disabled_symbol_is_skipped(clock: ManualClock) -> None:
    mock = connected(
        clock,
        symbols=[
            default_gold_spec("XAUUSD", trade_mode=TradeMode.DISABLED),
            default_gold_spec("XAUUSDm"),
        ],
    )
    assert mock.resolve_symbol(CANDIDATES).name == "XAUUSDm"


def test_no_usable_symbol_is_an_error(clock: ManualClock) -> None:
    disabled = connected(clock, symbols=[default_gold_spec(trade_mode=TradeMode.DISABLED)])
    with pytest.raises(SymbolNotFoundError, match="disabled for trading: XAUUSD"):
        disabled.resolve_symbol(CANDIDATES)

    eur_gold = default_gold_spec("XAUUSD.eur", currency_profit="EUR")
    wrong_currency = connected(clock, symbols=[eur_gold])
    with pytest.raises(SymbolNotFoundError, match="no tradable gold symbol"):
        wrong_currency.resolve_symbol(CANDIDATES)

    with pytest.raises(SymbolNotFoundError, match="NOPE"):
        disabled.symbol_spec("NOPE")


def test_ranking_of_gold_symbol_names() -> None:
    names = ["EURUSD", "XAUEUR", "GOLDm", "XAUUSD.pro", "mXAUUSD", "XAUUSDc", "XAGUSD", "GOLD"]
    assert rank_gold_symbols(names) == ["XAUUSDc", "XAUUSD.pro", "mXAUUSD", "GOLD", "GOLDm"]
    without = rank_gold_symbols(names, exclude=["GOLD", "XAUUSDc"])
    assert without == ["XAUUSD.pro", "mXAUUSD", "GOLDm"]


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def test_only_closed_bars_are_returned(broker: MockBroker) -> None:
    bars = broker.get_rates("XAUUSD", M5, 50)
    assert len(bars) == 50
    assert bars.index[-1] == utc(2026, 11, 18, 9, 55)  # the 10:00 bar is still forming
    assert bars.index.is_monotonic_increasing and bars.index.is_unique
    assert (bars.index + M5.duration <= NOW).all()


def test_forming_bar_only_on_request(broker: MockBroker) -> None:
    bars = broker.get_rates("XAUUSD", M5, 3, include_forming=True)
    assert list(bars.index) == [
        utc(2026, 11, 18, 9, 50),
        utc(2026, 11, 18, 9, 55),
        utc(2026, 11, 18, 10, 0),
    ]
    # Built only from the one-minute bars that have closed: 10:00 and 10:01.
    minutes = broker.get_rates_range("XAUUSD", M1, utc(2026, 11, 18, 10, 0), NOW)
    assert list(minutes.index) == [utc(2026, 11, 18, 10, 0), utc(2026, 11, 18, 10, 1)]
    forming = bars.iloc[-1]
    assert forming["open"] == minutes["open"].iloc[0]
    assert forming["close"] == minutes["close"].iloc[-1]
    assert forming["high"] == minutes["high"].max()


def test_advancing_the_clock_reveals_bars_without_rewriting_history(
    broker: MockBroker, clock: ManualClock
) -> None:
    before = broker.get_rates("XAUUSD", M5, 20)
    clock.advance(minutes=5)  # 10:07:30
    after = broker.get_rates("XAUUSD", M5, 21)
    assert after.index[-1] == utc(2026, 11, 18, 10, 0)
    assert_candles_equal(after.iloc[:-1], before)


def test_higher_timeframes_are_built_from_the_one_minute_bars(broker: MockBroker) -> None:
    bar = broker.get_rates("XAUUSD", M5, 1)
    opened = bar.index[0].to_pydatetime()
    minutes = broker.get_rates_range("XAUUSD", M1, opened, opened + M5.duration)
    assert len(minutes) == 5
    assert bar["open"].iloc[0] == minutes["open"].iloc[0]
    assert bar["high"].iloc[0] == minutes["high"].max()
    assert bar["low"].iloc[0] == minutes["low"].min()
    assert bar["close"].iloc[0] == minutes["close"].iloc[-1]
    assert bar["tick_volume"].iloc[0] == minutes["tick_volume"].sum()

    hourly = broker.get_rates("XAUUSD", H1, 5)
    assert hourly.index[-1] == utc(2026, 11, 18, 9, 0)
    assert all(stamp.minute == 0 for stamp in hourly.index)


def test_range_is_half_open_and_never_looks_ahead(broker: MockBroker) -> None:
    bars = broker.get_rates_range("XAUUSD", M5, utc(2026, 11, 18, 9, 30), utc(2026, 11, 18, 9, 45))
    assert list(bars.index) == [
        utc(2026, 11, 18, 9, 30),
        utc(2026, 11, 18, 9, 35),
        utc(2026, 11, 18, 9, 40),
    ]
    future = broker.get_rates_range("XAUUSD", M5, utc(2026, 11, 18, 9, 50), utc(2026, 11, 19))
    assert list(future.index) == [utc(2026, 11, 18, 9, 50), utc(2026, 11, 18, 9, 55)]


def test_prices_are_repeatable_for_a_seed() -> None:
    first = MockBroker(clock=ManualClock(NOW), seed=11, history_days=3, future_days=1)
    second = MockBroker(clock=ManualClock(NOW), seed=11, history_days=3, future_days=1)
    other = MockBroker(clock=ManualClock(NOW), seed=12, history_days=3, future_days=1)
    for mock in (first, second, other):
        mock.connect()
    a = first.get_rates("XAUUSD", M5, 100)
    assert_candles_equal(second.get_rates("XAUUSD", M5, 100), a)
    assert not np.allclose(other.get_rates("XAUUSD", M5, 100)["close"], a["close"])


def test_generated_bars_are_well_formed() -> None:
    bars = generate_m1_candles(utc(2026, 11, 9), utc(2026, 11, 19), seed=3)
    assert (bars["high"] >= bars[["open", "close"]].max(axis=1)).all()
    assert (bars["low"] <= bars[["open", "close"]].min(axis=1)).all()
    assert (bars["tick_volume"] > 0).all()
    assert (bars["spread"] == 20).all()
    assert bars["open"].iloc[1] == bars["close"].iloc[0]
    with pytest.raises(ValueError, match="end must be after start"):
        generate_m1_candles(utc(2026, 11, 19), utc(2026, 11, 9))


def test_market_hours_follow_new_york_time() -> None:
    bars = generate_m1_candles(utc(2026, 11, 12), utc(2026, 11, 17), seed=3)
    index = bars.index
    # Friday closes at 17:00 New York (22:00 UTC in winter); Sunday reopens at 18:00 (23:00 UTC).
    weekend = index[(index >= utc(2026, 11, 13, 22, 0)) & (index < utc(2026, 11, 15, 23, 0))]
    assert len(weekend) == 0
    assert utc(2026, 11, 13, 21, 59) in index
    assert utc(2026, 11, 15, 23, 0) in index
    # Daily break 17:00-18:00 New York on a weekday.
    assert utc(2026, 11, 12, 21, 59) in index
    assert utc(2026, 11, 12, 22, 30) not in index
    assert utc(2026, 11, 12, 23, 0) in index
    # In summer the same New York hours are one hour earlier in UTC.
    summer = generate_m1_candles(utc(2026, 7, 9), utc(2026, 7, 10), seed=3).index
    assert utc(2026, 7, 9, 21, 30) not in summer
    assert utc(2026, 7, 9, 22, 0) in summer
    assert market_open_mask(index).all()


def test_weekend_shows_fridays_last_bar(clock: ManualClock) -> None:
    clock.set(utc(2026, 11, 14, 12, 0))  # Saturday
    mock = connected(clock)
    bars = mock.get_rates("XAUUSD", M5, 3)
    assert bars.index[-1] == utc(2026, 11, 13, 21, 55)
    tick = mock.get_tick("XAUUSD")
    assert tick.time == utc(2026, 11, 13, 22, 0)  # the moment the market closed


def test_tick_and_spread(broker: MockBroker) -> None:
    tick = broker.get_tick("XAUUSD")
    assert tick.ask > tick.bid
    assert tick.spread == pytest.approx(0.20)
    assert tick.time == utc(2026, 11, 18, 10, 2)  # close of the last finished one-minute bar
    last_minute = broker.get_rates("XAUUSD", M1, 1)
    assert tick.last == last_minute["close"].iloc[-1]
    assert broker.spread_points("XAUUSD") == 20.0


def test_spread_setting_and_point_size_are_respected(clock: ManualClock) -> None:
    mock = connected(
        clock,
        spread_points=350,
        symbols=[default_gold_spec(digits=3, point=0.001, tick_size=0.001, tick_value=0.1)],
    )
    assert mock.spread_points("XAUUSD") == 350.0
    assert mock.get_tick("XAUUSD").spread == pytest.approx(0.35)


def test_supplied_candles_replace_the_generated_ones(clock: ManualClock) -> None:
    supplied = make_candles(utc(2026, 11, 18, 9, 0), 60, minutes=1)  # 09:00 .. 09:59
    mock = MockBroker(clock=clock, candles_m1=supplied)
    mock.connect()
    bars = mock.get_rates("XAUUSD", M5, 100)
    assert len(bars) == 12
    assert bars["open"].iloc[0] == supplied["open"].iloc[0]
    assert mock.get_tick("XAUUSD").last == supplied["close"].iloc[-1]


def test_bad_requests(broker: MockBroker, clock: ManualClock) -> None:
    with pytest.raises(ValueError, match="count"):
        broker.get_rates("XAUUSD", M5, 0)
    with pytest.raises(SymbolNotFoundError):
        broker.get_rates("EURUSD", M5, 5)
    with pytest.raises(ValueError, match="end must be after start"):
        broker.get_rates_range("XAUUSD", M5, NOW, NOW - dt.timedelta(hours=1))
    with pytest.raises(ValueError, match="naive"):
        broker.get_rates_range("XAUUSD", M5, dt.datetime(2026, 11, 18, 9), NOW)
    later = make_candles(utc(2026, 11, 19), 5, minutes=1)
    empty_history = MockBroker(clock=clock, candles_m1=later)
    empty_history.connect()
    with pytest.raises(BrokerError, match="no price data"):
        empty_history.get_tick("XAUUSD")
    assert empty_history.get_rates("XAUUSD", M5, 5).empty


def test_configured_symbol_not_quoted_in_usd_is_used_with_a_warning(
    clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    mock = connected(clock, symbols=[default_gold_spec("XAUUSD", currency_profit="EUR")])
    with caplog.at_level(logging.WARNING):
        assert mock.resolve_symbol(CANDIDATES).name == "XAUUSD"
    assert "not quoted in USD" in caplog.text


def test_name_search_skips_disabled_matches(clock: ManualClock) -> None:
    mock = connected(
        clock,
        symbols=[
            default_gold_spec("XAUUSD.x", trade_mode=TradeMode.DISABLED),
            default_gold_spec("XAUUSD.pro"),
        ],
    )
    assert mock.resolve_symbol(CANDIDATES).name == "XAUUSD.pro"


def test_broken_contract_data_is_reported_not_divided_by(clock: ManualClock) -> None:
    mock = connected(clock, symbols=[default_gold_spec(tick_size=0.0, point=0.0)])
    with pytest.raises(BrokerError, match="tick size"):
        _ = mock.symbol_spec("XAUUSD").value_per_price_unit
    with pytest.raises(BrokerError, match="point size"):
        mock.spread_points("XAUUSD")


def test_no_bars_are_generated_while_the_market_is_shut() -> None:
    assert generate_m1_candles(utc(2026, 11, 14, 0), utc(2026, 11, 15, 12)).empty

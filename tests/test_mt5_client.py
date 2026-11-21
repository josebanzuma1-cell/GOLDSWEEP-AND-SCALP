"""The MetaTrader 5 client, driven by an in-memory fake of the MetaTrader5 module.

The fake's server clock runs two hours ahead of UTC (a "New York close" server
in November), so every test here also proves the server-time conversion.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pytest

from goldsweep.core.clock import ManualClock
from goldsweep.core.server_time import DEFAULT_ASSUMED_RULE
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import (
    AccountType,
    BrokerConnectionError,
    BrokerError,
    BrokerUnavailableError,
    SymbolNotFoundError,
    TradeMode,
)
from goldsweep.data.candles import CANDLE_COLUMNS
from goldsweep.data.factory import create_broker
from goldsweep.data.mock_broker import MockBroker
from goldsweep.data.mt5_client import MT5Client, _import_mt5
from goldsweep.storage.db import Database
from tests.fake_mt5 import Bar, FakeMT5, gold_symbol
from tests.helpers import utc

CANDIDATES = ("XAUUSD", "XAUUSD.m", "XAUUSDm", "GOLD")
NOW = utc(2026, 11, 16, 10, 2, 30)  # Monday; London and New York both on winter time
M5 = Timeframe.M5
TWO_HOURS = dt.timedelta(hours=2)


def m5_bars() -> list[Bar]:
    """Seven M5 bars opening 09:30 .. 10:00 UTC; at NOW the last one is still forming."""
    first = utc(2026, 11, 16, 9, 30)
    return [
        (first + dt.timedelta(minutes=5 * n), 2650.0 + n, 2651.5 + n, 2649.0 + n, 2650.5 + n)
        for n in range(7)
    ]


def at(hour: int, minute: int) -> dt.datetime:
    return utc(2026, 11, 16, hour, minute)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def fake(clock: ManualClock) -> FakeMT5:
    mt5 = FakeMT5(clock)
    mt5.add_bars(FakeMT5.TIMEFRAME_M5, m5_bars())
    return mt5


def make_client(fake: FakeMT5, clock: ManualClock, **kwargs: Any) -> tuple[MT5Client, list[float]]:
    """A client wired to the fake, plus the list of delays it asked to sleep for."""
    sleeps: list[float] = []
    client = MT5Client(mt5=fake, clock=clock, sleep=sleeps.append, **kwargs)
    return client, sleeps


def connected(fake: FakeMT5, clock: ManualClock, **kwargs: Any) -> MT5Client:
    client, _sleeps = make_client(fake, clock, **kwargs)
    client.connect()
    return client


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #


def test_attaches_to_the_running_terminal_when_no_credentials_are_given(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client, sleeps = make_client(fake, clock)
    assert not client.is_connected()
    client.connect()
    assert client.is_connected()
    assert fake.count("initialize") == 1
    assert fake.count("login") == 0
    assert sleeps == []
    account = client.account_info()
    assert (account.login, account.server, account.currency) == (5012345, "Demo-Server", "USD")
    assert account.account_type is AccountType.DEMO and account.is_demo
    assert (account.balance, account.equity, account.leverage) == (10_000.0, 10_000.0, 100)
    assert account.margin_level is None  # no margin in use
    assert account.trade_allowed


def test_logs_in_with_credentials_and_passes_the_path_positionally(
    fake: FakeMT5, clock: ManualClock, tmp_path: Path
) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"")
    client = connected(fake, clock, login=777, password="pw", server="Demo-Server", path=terminal)
    _name, args, kwargs = fake.calls_named("initialize")[0]
    assert args == (str(terminal),)
    assert kwargs == {"timeout": 60_000}
    _name, login_args, login_kwargs = fake.calls_named("login")[0]
    assert login_args == (777,)
    assert login_kwargs == {"password": "pw", "server": "Demo-Server"}
    assert client.account_info().login == 777


def test_connection_is_retried_with_exponential_backoff(fake: FakeMT5, clock: ManualClock) -> None:
    fake.initialize_results = [False, False, True]
    client, sleeps = make_client(fake, clock)
    client.connect()
    assert sleeps == [1.0, 2.0]
    assert fake.count("initialize") == 3
    assert client.is_connected()


def test_connection_gives_up_after_the_last_attempt(fake: FakeMT5, clock: ManualClock) -> None:
    fake.initialize_results = [False] * 5
    client, sleeps = make_client(fake, clock)
    with pytest.raises(BrokerConnectionError, match="after 5 attempts"):
        client.connect()
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert not client.is_connected()


def test_backoff_delay_is_capped(fake: FakeMT5, clock: ManualClock) -> None:
    fake.initialize_results = [False, False, False, False, True]
    client, sleeps = make_client(fake, clock, backoff_base=10.0, backoff_max=25.0)
    client.connect()
    assert sleeps == [10.0, 20.0, 25.0, 25.0]


def test_rejected_login_is_not_retried(fake: FakeMT5, clock: ManualClock) -> None:
    fake.login_results = [False]
    client, sleeps = make_client(fake, clock, login=777, password="wrong", server="Demo-Server")
    with pytest.raises(BrokerConnectionError, match="rejected the login"):
        client.connect()
    assert sleeps == []
    assert fake.count("initialize") == 1


def test_wrong_account_is_refused(fake: FakeMT5, clock: ManualClock) -> None:
    fake.login_switches_account = False  # the terminal stays on account 5012345
    client, _sleeps = make_client(fake, clock, login=777, password="pw", server="Demo-Server")
    with pytest.raises(BrokerConnectionError, match="not the configured MT5_LOGIN 777"):
        client.connect()
    assert not client.is_connected()


def test_missing_terminal_path_is_reported_before_anything_starts(
    fake: FakeMT5, clock: ManualClock, tmp_path: Path
) -> None:
    client, _sleeps = make_client(fake, clock, path=tmp_path / "nowhere" / "terminal64.exe")
    with pytest.raises(BrokerUnavailableError, match="MT5_PATH"):
        client.connect()
    assert fake.count("initialize") == 0


def test_missing_package_gives_a_clear_error(
    clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # makes "import MetaTrader5" fail
    client = MT5Client(clock=clock)
    with pytest.raises(BrokerUnavailableError, match="--mock"):
        client.connect()


def test_calls_before_connect_are_refused(fake: FakeMT5, clock: ManualClock) -> None:
    client, _sleeps = make_client(fake, clock)
    with pytest.raises(BrokerConnectionError, match="not connected"):
        client.account_info()
    with pytest.raises(BrokerConnectionError, match="not connected"):
        client.get_rates("XAUUSD", M5, 3)


def test_shutdown_is_idempotent_and_context_manager_works(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client, _sleeps = make_client(fake, clock)
    with client as opened:
        assert opened is client and client.is_connected()
    assert not client.is_connected()
    assert fake.count("shutdown") == 1
    client.shutdown()
    assert fake.count("shutdown") == 1


def test_bad_arguments_are_rejected_at_construction(fake: FakeMT5, clock: ManualClock) -> None:
    with pytest.raises(ValueError, match="server time zone"):
        MT5Client(mt5=fake, clock=clock, server_timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="max_attempts"):
        MT5Client(mt5=fake, clock=clock, max_attempts=0)


def test_client_from_settings(settings: Settings, fake: FakeMT5, clock: ManualClock) -> None:
    client = MT5Client.from_settings(settings, clock=clock, mt5=fake)
    client.connect()
    assert fake.count("login") == 0  # no credentials in the test environment
    assert client.server_time.source == "assumed"  # config.yaml says "auto"


# --------------------------------------------------------------------------- #
# Account and terminal details
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("trade_mode", "expected"),
    [(0, AccountType.DEMO), (1, AccountType.CONTEST), (2, AccountType.REAL), (9, AccountType.REAL)],
)
def test_account_type_and_unknown_types_count_as_real(
    clock: ManualClock, trade_mode: int, expected: AccountType
) -> None:
    client = connected(FakeMT5(clock, account_trade_mode=trade_mode), clock)
    assert client.account_info().account_type is expected


def test_margin_level_is_reported_once_margin_is_in_use(fake: FakeMT5, clock: ManualClock) -> None:
    fake.account.margin = 500.0
    fake.account.margin_level = 2000.0
    assert connected(fake, clock).account_info().margin_level == 2000.0


def test_terminal_details(fake: FakeMT5, clock: ManualClock) -> None:
    terminal = connected(fake, clock).terminal_info()
    assert (terminal.name, terminal.build) == ("MetaTrader 5", 5200)
    assert terminal.connected and terminal.trade_allowed
    assert terminal.common_data_path == r"C:\MT5\common"


# --------------------------------------------------------------------------- #
# Symbol resolution
# --------------------------------------------------------------------------- #


def test_first_candidate_wins_and_is_added_to_market_watch(
    fake: FakeMT5, clock: ManualClock
) -> None:
    spec = connected(fake, clock).resolve_symbol(CANDIDATES)
    assert spec.name == "XAUUSD"
    assert ("symbol_select", ("XAUUSD", True), {}) in fake.calls
    assert fake.symbols["XAUUSD"].visible
    # Instrument properties come from the broker, never from assumptions.
    assert (spec.digits, spec.point, spec.contract_size) == (2, 0.01, 100.0)
    assert (spec.tick_size, spec.tick_value) == (0.01, 1.0)
    assert (spec.volume_min, spec.volume_step, spec.volume_max) == (0.01, 0.01, 100.0)
    assert spec.value_per_price_unit == pytest.approx(100.0)
    assert spec.trade_mode is TradeMode.FULL and spec.tradable
    assert spec.currency_profit == "USD"


def test_broker_specific_contract_details_are_read(clock: ManualClock) -> None:
    fake = FakeMT5(
        clock,
        symbols=[
            gold_symbol(
                "XAUUSD.m",
                digits=3,
                point=0.001,
                trade_contract_size=10.0,
                trade_tick_size=0.001,
                trade_tick_value=0.01,
                volume_min=0.1,
                trade_stops_level=50,
            )
        ],
    )
    spec = connected(fake, clock).resolve_symbol(CANDIDATES)
    assert spec.name == "XAUUSD.m"
    assert (spec.digits, spec.point, spec.contract_size) == (3, 0.001, 10.0)
    assert (spec.volume_min, spec.stops_level) == (0.1, 50)
    assert spec.value_per_price_unit == pytest.approx(10.0)


def test_symbol_named_gold(clock: ManualClock) -> None:
    fake = FakeMT5(clock, symbols=[gold_symbol("GOLD")])
    assert connected(fake, clock).resolve_symbol(CANDIDATES).name == "GOLD"


def test_unlisted_name_is_found_by_search(clock: ManualClock) -> None:
    fake = FakeMT5(
        clock,
        symbols=[
            gold_symbol("EURUSD", currency_base="EUR"),
            gold_symbol("XAUEUR", currency_profit="EUR"),
            gold_symbol("XAUUSD-ECN-raw"),
            gold_symbol("XAUUSD.pro"),
        ],
    )
    assert connected(fake, clock).resolve_symbol(CANDIDATES).name == "XAUUSD.pro"
    assert fake.calls_named("symbols_get")[0][2] == {"group": "*XAU*,*GOLD*"}


def test_disabled_symbol_is_skipped(clock: ManualClock) -> None:
    fake = FakeMT5(clock, symbols=[gold_symbol("XAUUSD", trade_mode=0), gold_symbol("XAUUSDm")])
    assert connected(fake, clock).resolve_symbol(CANDIDATES).name == "XAUUSDm"


def test_unknown_trade_mode_counts_as_disabled(clock: ManualClock) -> None:
    fake = FakeMT5(clock, symbols=[gold_symbol("XAUUSD", trade_mode=9)])
    client = connected(fake, clock)
    assert client.symbol_spec("XAUUSD").trade_mode is TradeMode.DISABLED
    with pytest.raises(SymbolNotFoundError, match="disabled for trading: XAUUSD"):
        client.resolve_symbol(CANDIDATES)


def test_gold_not_quoted_in_usd_is_not_picked_by_the_search(clock: ManualClock) -> None:
    fake = FakeMT5(clock, symbols=[gold_symbol("XAUUSD.eur", currency_profit="EUR")])
    with pytest.raises(SymbolNotFoundError, match="no tradable gold symbol"):
        connected(fake, clock).resolve_symbol(CANDIDATES)


def test_account_without_gold(clock: ManualClock) -> None:
    client = connected(FakeMT5(clock, symbols=[gold_symbol("EURUSD")]), clock)
    with pytest.raises(SymbolNotFoundError, match="no tradable gold symbol"):
        client.resolve_symbol(CANDIDATES)
    with pytest.raises(SymbolNotFoundError, match="XAUUSD"):
        client.symbol_spec("XAUUSD")


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #


def test_bar_times_are_converted_from_server_time_to_utc(fake: FakeMT5, clock: ManualClock) -> None:
    client = connected(fake, clock)
    bars = client.get_rates("XAUUSD", M5, 3)
    assert list(bars.index) == [at(9, 45), at(9, 50), at(9, 55)]
    assert bars.index.tz is not None and bars.index.name == "time"
    # What MT5 itself reported for the newest bar: 12:00, two hours ahead of UTC.
    raw = fake.copy_rates_from_pos("XAUUSD", FakeMT5.TIMEFRAME_M5, 0, 1)
    assert raw is not None and int(raw["time"][0]) == int(at(12, 0).timestamp())
    state = client.server_time
    assert state.confirmed and state.source == "live_tick"
    assert state.rule == DEFAULT_ASSUMED_RULE
    assert state.observed_offset == TWO_HOURS


def test_bar_values_and_columns(fake: FakeMT5, clock: ManualClock) -> None:
    bars = connected(fake, clock).get_rates("XAUUSD", M5, 2)
    assert list(bars.columns) == list(CANDLE_COLUMNS)
    assert bars["open"].tolist() == [2654.0, 2655.0]  # the 09:50 and 09:55 bars
    assert bars["high"].tolist() == [2655.5, 2656.5]
    assert bars["low"].tolist() == [2653.0, 2654.0]
    assert bars["close"].tolist() == [2654.5, 2655.5]
    assert bars["tick_volume"].tolist() == [100, 100]
    assert bars["spread"].tolist() == [21, 21]
    assert str(bars["tick_volume"].dtype) == "int64"


def test_forming_bar_only_on_request(fake: FakeMT5, clock: ManualClock) -> None:
    client = connected(fake, clock)
    with_forming = client.get_rates("XAUUSD", M5, 3, include_forming=True)
    assert list(with_forming.index) == [at(9, 50), at(9, 55), at(10, 0)]
    assert client.get_rates("XAUUSD", M5, 100).index[-1] == at(9, 55)


def test_newest_bar_counts_as_closed_once_the_clock_passes_its_end(
    fake: FakeMT5, clock: ManualClock
) -> None:
    """Quiet market: no tick has opened the 10:05 bar yet, but the 10:00 bar has ended."""
    client = connected(fake, clock)
    clock.set(utc(2026, 11, 16, 10, 5, 0))
    assert client.get_rates("XAUUSD", M5, 3).index[-1] == at(9, 55)  # inside the 1 s grace
    clock.set(utc(2026, 11, 16, 10, 5, 2))
    assert client.get_rates("XAUUSD", M5, 3).index[-1] == at(10, 0)


def test_weekend_with_a_pinned_server_time_returns_fridays_last_bar(clock: ManualClock) -> None:
    clock.set(utc(2026, 11, 14, 12, 0))  # Saturday
    fake = FakeMT5(clock, live=False)
    fake.last_tick_time = utc(2026, 11, 13, 21, 59, 58)
    fake.add_bars(
        FakeMT5.TIMEFRAME_M5,
        [
            (utc(2026, 11, 13, 21, 50), 2650.0, 2651.0, 2649.0, 2650.5),
            (utc(2026, 11, 13, 21, 55), 2650.5, 2651.5, 2649.5, 2651.0),
        ],
    )
    client = connected(fake, clock, server_timezone="NY+7")
    bars = client.get_rates("XAUUSD", M5, 2)
    assert list(bars.index) == [utc(2026, 11, 13, 21, 50), utc(2026, 11, 13, 21, 55)]
    assert client.server_time.source == "config" and client.server_time.confirmed


def test_unconfirmed_server_time_withholds_the_newest_bar(clock: ManualClock) -> None:
    """A guessed offset could hide that the newest bar is still forming, so it is held back."""
    clock.set(utc(2026, 11, 14, 5, 0))  # Saturday, seven hours after Friday's close
    fake = FakeMT5(clock, live=False)
    fake.add_bars(
        FakeMT5.TIMEFRAME_M5,
        [
            (utc(2026, 11, 13, 21, 50), 2650.0, 2651.0, 2649.0, 2650.5),
            (utc(2026, 11, 13, 21, 55), 2650.5, 2651.5, 2649.5, 2651.0),
        ],
    )
    client = connected(fake, clock)
    bars = client.get_rates("XAUUSD", M5, 2)
    assert list(bars.index) == [utc(2026, 11, 13, 21, 50)]
    assert not client.server_time.confirmed and client.server_time.source == "assumed"
    clock.set(utc(2026, 11, 14, 13, 0))  # more than 14 hours after that bar ended
    assert len(client.get_rates("XAUUSD", M5, 2)) == 2


def test_range_request_is_sent_in_server_time_and_is_half_open(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client = connected(fake, clock)
    bars = client.get_rates_range("XAUUSD", M5, at(9, 35), at(9, 50))
    assert list(bars.index) == [at(9, 35), at(9, 40), at(9, 45)]
    _name, args, _kwargs = fake.calls_named("copy_rates_range")[0]
    assert args[2] == at(11, 35) and args[3] == at(11, 50)  # the server clock is UTC+2
    assert args[2].utcoffset() == dt.timedelta(0)  # labelled UTC, as the MT5 API expects


def test_range_never_returns_the_forming_bar(fake: FakeMT5, clock: ManualClock) -> None:
    bars = connected(fake, clock).get_rates_range("XAUUSD", M5, at(9, 50), at(10, 30))
    assert list(bars.index) == [at(9, 50), at(9, 55)]


def test_no_bars_available_gives_an_empty_frame(fake: FakeMT5, clock: ManualClock) -> None:
    bars = connected(fake, clock).get_rates("XAUUSD", Timeframe.H1, 5)
    assert bars.empty
    assert list(bars.columns) == list(CANDLE_COLUMNS)
    assert fake.calls_named("copy_rates_from_pos")[-1][1] == (
        "XAUUSD",
        FakeMT5.TIMEFRAME_H1,
        0,
        6,
    )


def test_failed_bar_request_raises(fake: FakeMT5, clock: ManualClock) -> None:
    client = connected(fake, clock)
    fake.errors["copy_rates_from_pos"] = [(-2, "Invalid params")]
    with pytest.raises(BrokerError, match="Invalid params"):
        client.get_rates("XAUUSD", M5, 3)
    fake.errors["copy_rates_range"] = [(-2, "Invalid params")]
    with pytest.raises(BrokerError, match="Invalid params"):
        client.get_rates_range("XAUUSD", M5, at(9, 0), at(10, 0))


def test_bad_bar_requests(fake: FakeMT5, clock: ManualClock) -> None:
    client = connected(fake, clock)
    with pytest.raises(ValueError, match="count"):
        client.get_rates("XAUUSD", M5, 0)
    with pytest.raises(ValueError, match="end must be after start"):
        client.get_rates_range("XAUUSD", M5, at(10, 0), at(9, 0))
    with pytest.raises(ValueError, match="naive"):
        client.get_rates_range("XAUUSD", M5, dt.datetime(2026, 11, 16, 9, 0), at(10, 0))


# --------------------------------------------------------------------------- #
# Reconnecting
# --------------------------------------------------------------------------- #


def test_dropped_link_is_reconnected_and_the_call_retried(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client, sleeps = make_client(fake, clock)
    client.connect()
    client.get_tick("XAUUSD")  # settles the server-time check first
    sleeps.clear()
    fake.errors["copy_rates_from_pos"] = [(-10004, "No IPC connection")]
    bars = client.get_rates("XAUUSD", M5, 3)
    assert len(bars) == 3
    assert sleeps == [1.0]
    assert fake.count("initialize") == 2


def test_link_that_stays_down_raises(fake: FakeMT5, clock: ManualClock) -> None:
    client, sleeps = make_client(fake, clock)
    client.connect()
    client.get_tick("XAUUSD")
    sleeps.clear()
    fake.errors["copy_rates_from_pos"] = [(-10004, "No IPC connection")] * 5
    with pytest.raises(BrokerConnectionError, match="still failing after 5 reconnect attempts"):
        client.get_rates("XAUUSD", M5, 3)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


# --------------------------------------------------------------------------- #
# Ticks and spread
# --------------------------------------------------------------------------- #


def test_tick_time_is_true_utc(fake: FakeMT5, clock: ManualClock) -> None:
    tick = connected(fake, clock).get_tick("XAUUSD")
    assert abs((tick.time - NOW).total_seconds()) < 1.0
    assert tick.time.utcoffset() == dt.timedelta(0)
    assert (tick.bid, tick.ask) == (2654.31, 2654.52)
    assert tick.spread == pytest.approx(0.21)
    assert tick.mid == pytest.approx(2654.415)


def test_spread_in_points_uses_the_brokers_point_size(fake: FakeMT5, clock: ManualClock) -> None:
    assert connected(fake, clock).spread_points("XAUUSD") == 21.0
    three_digit = FakeMT5(clock, symbols=[gold_symbol(digits=3, point=0.001)])
    assert connected(three_digit, clock).spread_points("XAUUSD") == 210.0


def test_tick_for_an_unknown_symbol(fake: FakeMT5, clock: ManualClock) -> None:
    with pytest.raises(BrokerError, match="no tick for NOPE"):
        connected(fake, clock).get_tick("NOPE")


# --------------------------------------------------------------------------- #
# Server time
# --------------------------------------------------------------------------- #


def test_server_time_starts_as_an_unconfirmed_assumption(fake: FakeMT5, clock: ManualClock) -> None:
    state = connected(fake, clock).server_time
    assert state.source == "assumed" and not state.confirmed
    assert state.rule == DEFAULT_ASSUMED_RULE


@pytest.mark.parametrize(
    ("now", "offset_hours", "expected_spec"),
    [
        (utc(2026, 1, 15, 10, 2, 30), 2, "America/New_York+07:00"),  # winter
        (utc(2026, 7, 15, 10, 2, 30), 3, "America/New_York+07:00"),  # summer
        (utc(2026, 7, 15, 10, 2, 30), 0, "UTC"),
        (utc(2026, 7, 15, 10, 2, 30), 2, "UTC+02:00"),  # +2 in summer is not New York close
        (utc(2026, 1, 15, 10, 2, 30), 8, "UTC+08:00"),
    ],
)
def test_live_tick_measures_the_server_offset(
    now: dt.datetime, offset_hours: int, expected_spec: str
) -> None:
    clock = ManualClock(now)
    fake = FakeMT5(clock, server_offset=dt.timedelta(hours=offset_hours))
    client = connected(fake, clock)
    tick = client.get_tick("XAUUSD")
    state = client.server_time
    assert state.confirmed and state.source == "live_tick"
    assert state.rule.spec == expected_spec
    assert state.observed_offset == dt.timedelta(hours=offset_hours)
    assert abs((tick.time - now).total_seconds()) < 1.0


def test_stale_tick_is_never_used_to_measure_the_offset(clock: ManualClock) -> None:
    """Market shut: a tick exactly three hours old would otherwise look like "UTC-1"."""
    fake = FakeMT5(clock, live=False)
    fake.last_tick_time = NOW - dt.timedelta(hours=3)
    client, sleeps = make_client(fake, clock)
    client.connect()
    tick = client.get_tick("XAUUSD")
    assert client.server_time.source == "assumed" and not client.server_time.confirmed
    assert tick.time == NOW - dt.timedelta(hours=3)
    assert sleeps == [0.25] * 6  # watched for a fresh tick, saw none


def test_measurement_is_remembered_for_when_the_market_is_shut(
    clock: ManualClock, db: Database
) -> None:
    live = FakeMT5(clock, server_offset=dt.timedelta(hours=8))
    first = connected(live, clock, state=db)
    first.get_tick("XAUUSD")
    first.shutdown()
    assert db.get_state("mt5.server_time.Demo-Server")["spec"] == "UTC+08:00"

    shut = FakeMT5(clock, server_offset=dt.timedelta(hours=8), live=False)
    second = connected(shut, clock, state=db)
    assert second.server_time.source == "cache" and not second.server_time.confirmed
    assert second.server_time.rule.spec == "UTC+08:00"

    other_server = FakeMT5(clock, server="Other-Server", live=False)
    assert connected(other_server, clock, state=db).server_time.source == "assumed"


def test_pinned_rule_is_confirmed_by_a_matching_live_tick(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client = connected(fake, clock, server_timezone="NY+7")
    assert client.server_time.source == "config" and client.server_time.confirmed
    client.get_tick("XAUUSD")
    state = client.server_time
    assert state.source == "config" and state.confirmed and state.warning is None
    assert state.observed_offset == TWO_HOURS


def test_pinned_rule_that_contradicts_the_live_clock_is_flagged(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client = connected(fake, clock, server_timezone="UTC")
    client.get_tick("XAUUSD")
    state = client.server_time
    assert not state.confirmed
    assert state.warning is not None and "UTC+02:00" in state.warning
    assert state.rule.spec == "UTC"  # the user's setting is reported, never silently replaced


def test_pc_clock_error_is_measured(fake: FakeMT5, clock: ManualClock) -> None:
    fake.pc_clock_behind_seconds = 12.0
    client = connected(fake, clock)
    client.get_tick("XAUUSD")
    state = client.server_time
    assert state.observed_offset == TWO_HOURS
    assert state.clock_skew_seconds == pytest.approx(12.0, abs=0.1)


def test_server_time_is_rechecked_periodically_not_on_every_call(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client = connected(fake, clock)
    client.get_tick("XAUUSD")
    calls = fake.count("symbol_info_tick")
    client.get_tick("XAUUSD")
    assert fake.count("symbol_info_tick") == calls + 1  # no extra measurement

    fake.server_offset = dt.timedelta(hours=3)  # the server clock changed
    clock.advance(hours=7)
    client.get_tick("XAUUSD")
    state = client.server_time
    assert state.observed_offset == dt.timedelta(hours=3)
    assert state.rule.spec == "UTC+03:00"


# --------------------------------------------------------------------------- #
# Failures that must be reported clearly or survived quietly
# --------------------------------------------------------------------------- #


def test_unresponsive_terminal_gets_a_specific_hint(fake: FakeMT5, clock: ManualClock) -> None:
    """Seen on a fresh install: the terminal runs, but no account is logged in yet."""
    fake.initialize_results = [False, False]
    fake.initialize_error = (-10005, "IPC timeout")
    client, sleeps = make_client(fake, clock, max_attempts=2)
    with pytest.raises(BrokerConnectionError, match="no trading account logged in"):
        client.connect()
    assert sleeps == [1.0]
    assert fake.count("initialize") == 2


def test_factory_picks_the_broker_and_can_limit_attempts(settings: Settings) -> None:
    assert isinstance(create_broker(settings, mock=True, clock=ManualClock(NOW)), MockBroker)
    client = create_broker(settings, connect_attempts=2, connect_timeout_ms=30_000)
    assert isinstance(client, MT5Client)
    assert (client._max_attempts, client._init_timeout_ms) == (2, 30_000)
    assert not client.is_connected()  # building a client contacts nothing
    assert create_broker(settings)._max_attempts == 5  # type: ignore[attr-defined]


def test_account_and_terminal_failures_are_reported(fake: FakeMT5, clock: ManualClock) -> None:
    client = connected(fake, clock)
    fake.errors["account_info"] = [(-1, "generic failure")]
    with pytest.raises(BrokerError, match="account_info failed: generic failure"):
        client.account_info()
    fake.errors["terminal_info"] = [(-1, "generic failure")]
    with pytest.raises(BrokerError, match="terminal_info failed: generic failure"):
        client.terminal_info()


def test_symbol_that_cannot_be_added_to_market_watch(
    fake: FakeMT5, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = connected(fake, clock)
    monkeypatch.setattr(fake, "symbol_select", lambda name, enable=True: False)
    with pytest.raises(BrokerError, match="could not add XAUUSD to Market Watch"):
        client.resolve_symbol(CANDIDATES)


def test_broken_state_store_does_not_break_the_client(fake: FakeMT5, clock: ManualClock) -> None:
    class BrokenStore:
        def get_state(self, key: str, default: object = None) -> object:
            raise OSError("disk unavailable")

        def set_state(self, key: str, value: object) -> None:
            raise OSError("disk unavailable")

    client = connected(fake, clock, state=BrokenStore())
    assert client.server_time.source == "assumed"
    client.get_tick("XAUUSD")
    assert client.server_time.confirmed  # measured live even though it could not be saved


def test_unusable_remembered_rule_is_ignored(clock: ManualClock, db: Database) -> None:
    db.set_state("mt5.server_time.Demo-Server", {"spec": "Mars/Olympus"})
    assert connected(FakeMT5(clock, live=False), clock, state=db).server_time.source == "assumed"
    db.set_state("mt5.server_time.Demo-Server", "not a mapping")
    assert connected(FakeMT5(clock, live=False), clock, state=db).server_time.source == "assumed"


def test_measurement_stops_quietly_when_ticks_stop_arriving(
    fake: FakeMT5, clock: ManualClock
) -> None:
    client = connected(fake, clock)
    answers = iter([fake.symbol_info_tick("XAUUSD")])  # one tick, then nothing
    fake.symbol_info_tick = lambda name: next(answers, None)  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="no tick for XAUUSD"):
        client.get_tick("XAUUSD")
    assert not client.server_time.confirmed


def test_installed_metatrader5_package_has_the_api_this_client_uses() -> None:
    """Runs only where the real package is installed (Windows); needs no terminal."""
    mt5 = pytest.importorskip("MetaTrader5")
    for name in (
        "initialize",
        "login",
        "shutdown",
        "last_error",
        "account_info",
        "terminal_info",
        "symbol_info",
        "symbol_select",
        "symbols_get",
        "symbol_info_tick",
        "copy_rates_from_pos",
        "copy_rates_range",
    ):
        assert callable(getattr(mt5, name)), name
    for timeframe in Timeframe:
        constant = f"TIMEFRAME_{timeframe.value}"
        assert getattr(mt5, constant) == getattr(FakeMT5, constant), constant
    assert _import_mt5() is mt5


def test_no_second_login_to_the_account_already_open(fake: FakeMT5, clock: ManualClock) -> None:
    """A login to the same account counts as an account change in MT5, which can turn
    its Algo Trading button off; the client only logs in when it has to."""
    client = connected(fake, clock, login=5012345, password="pw", server="Demo-Server")
    assert fake.count("login") == 0 and client.account_info().login == 5012345
    other = connected(fake, clock, login=777, password="pw", server="Demo-Server")
    assert fake.count("login") == 1 and other.account_info().login == 777

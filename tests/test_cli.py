"""The ``python main.py --check`` report."""

from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path
from typing import Any

import pytest

from goldsweep import cli
from goldsweep.cli import EXIT_BROKER, EXIT_CONFIG, EXIT_OK, main, run_check, session_lines
from goldsweep.core.clock import ManualClock, SessionCalculator
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import (
    AccountType,
    BrokerClient,
    BrokerConnectionError,
    BrokerUnavailableError,
)
from goldsweep.data.mock_broker import MockBroker, default_gold_spec
from goldsweep.data.mt5_client import MT5Client
from goldsweep.storage.db import SCHEMA_VERSION, Database
from tests.fake_mt5 import FakeMT5
from tests.helpers import utc

MONDAY_IN_LONDON_KILL_ZONE = utc(2026, 11, 16, 8, 30)
SATURDAY_NIGHT = utc(2026, 11, 14, 21, 10, 13)  # already Sunday 00:10 in Kampala


def check(settings: Settings, moment: dt.datetime, **kwargs: Any) -> tuple[int, str]:
    """Run the report with the mock broker at a fixed time and capture its text."""
    out = io.StringIO()
    kwargs.setdefault("use_mock", True)
    code = run_check(settings, clock=ManualClock(moment), out=out, **kwargs)
    return code, out.getvalue()


def cells(text: str, label: str) -> list[str]:
    """The columns of the table row that starts with ``label``."""
    line = next(line for line in text.splitlines() if line.startswith(label))
    return re.split(r"\s{2,}", line.strip())


def value(text: str, label: str) -> str:
    """The value of a ``Label       : value`` line."""
    line = next(line for line in text.splitlines() if line.startswith(label))
    return line.split(":", 1)[1].strip()


# --------------------------------------------------------------------------- #
# Full report with the mock broker
# --------------------------------------------------------------------------- #


def test_report_shows_mode_account_symbol_and_sessions(settings: Settings) -> None:
    code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert code == EXIT_OK
    assert text.splitlines()[0] == "GoldSweep 0.1.0 - environment check"
    assert value(text, "Mode").startswith("PAPER")
    assert "MODE=PAPER" in text and "mode=PAPER" in text
    assert value(text, "Time now") == (
        "2026-11-16 08:30:00 UTC  =  2026-11-16 11:30:00 Africa/Kampala"
    )
    assert f"schema v{SCHEMA_VERSION}" in value(text, "Database")
    assert "goldsweep-mock.sqlite3" in value(text, "Database")

    assert "Broker: MOCK (synthetic prices, not market data)" in text
    assert value(text, "Account").endswith("[DEMO]")
    assert "10,000.00 USD" in value(text, "Funds")
    assert value(text, "Server time").startswith("UTC+00:00 now")
    assert value(text, "Resolved").startswith("XAUUSD")
    assert value(text, "Candidates") == "XAUUSD, XAUUSD.m, XAUUSDm, GOLD"
    assert value(text, "Trade mode") == "FULL"
    assert "100 units per lot" in value(text, "Contract")
    assert "a 1.00 price move on 1.00 lot = 100.00 USD" in text
    assert value(text, "Spread").startswith("20 points")
    assert value(text, "Last M5 bar").startswith("opened 2026-11-16 08:25 UTC")
    assert value(text, "Stored") == "200 M5 candles and 1 spread sample in SQLite"
    assert text.rstrip().endswith("Result: OK")


def test_session_table_in_utc_and_kampala_time(settings: Settings) -> None:
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert "Sessions for Monday 2026-11-16" in text
    assert cells(text, "Session  ") == [
        "Session",
        "Defined as (local time)",
        "UTC",
        "Africa/Kampala",
        "New trades",
    ]
    assert cells(text, "Asia / Tokyo") == [
        "Asia / Tokyo",
        "09:00-15:00 Asia/Tokyo",
        "00:00-06:00",
        "03:00-09:00",
        "on",  # the owner switched Asia on (D87); its playbook decides what trades
    ]
    assert cells(text, "London kill zone") == [
        "London kill zone",
        "07:00-10:00 Europe/London",
        "07:00-10:00",
        "10:00-13:00",
        "on",
    ]
    assert cells(text, "New York kill zone") == [
        "New York kill zone",
        "08:00-11:00 America/New_York",
        "13:00-16:00",
        "16:00-19:00",
        "on",
    ]
    assert cells(text, "London-NY overlap") == [
        "London-NY overlap",
        "08:00-16:30 Europe/London + 08:00-17:00 America/New_York",
        "13:00-16:30",
        "16:00-19:30",
        "on",
    ]
    assert cells(text, "Rollover block") == [
        "Rollover block",
        "16:45-18:15 America/New_York",
        "21:45-23:15",
        "00:45(+1d)-02:15(+1d)",
        "blocked",
    ]
    assert cells(text, "Friday cutoff") == [
        "Friday cutoff",
        "from 12:00 America/New_York",
        "17:00",
        "20:00",
        "Fridays only",
    ]
    assert "(+1d) / (-1d)" in text


def test_live_status_inside_a_kill_zone(settings: Settings) -> None:
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert value(text, "Market") == "open; inside London kill zone"
    assert value(text, "New trades").startswith("allowed by the clock")
    assert value(text, "Next window") == (
        "New York kill zone opens Mon 2026-11-16 13:00 UTC = Mon 2026-11-16 16:00 Africa/Kampala"
    )


def test_weekend_report_uses_the_local_date_and_says_the_market_is_closed(
    settings: Settings,
) -> None:
    code, text = check(settings, SATURDAY_NIGHT)
    assert code == EXIT_OK
    # Saturday 21:10 UTC is already Sunday in Kampala: "today" follows the display zone.
    assert "Sessions for Sunday 2026-11-15" in text
    assert cells(text, "London kill zone")[-1] == "closed (weekend)"
    assert cells(text, "New York kill zone")[-1] == "closed (weekend)"
    assert value(text, "Market") == (
        "CLOSED (weekend); reopens Sun 2026-11-15 23:00 UTC = Mon 2026-11-16 02:00 Africa/Kampala"
    )
    assert value(text, "New trades").startswith("NOT allowed now: weekend: market closed")
    # Asia is on (D87): Monday's first window is Tokyo's, 09:00 local = 00:00 UTC.
    assert value(text, "Next window").startswith("Asia / Tokyo opens Mon 2026-11-16 00:00 UTC")
    # The mock still serves Friday's last closed bar.
    assert value(text, "Last M5 bar").startswith("opened 2026-11-13 21:55 UTC")


def test_report_for_another_date_follows_daylight_saving(settings: Settings) -> None:
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE, day=dt.date(2026, 7, 15))
    assert "Sessions for Wednesday 2026-07-15" in text
    assert cells(text, "London kill zone")[2:4] == ["06:00-09:00", "09:00-12:00"]
    assert cells(text, "New York kill zone")[2:4] == ["12:00-15:00", "15:00-18:00"]
    assert cells(text, "London-NY overlap")[2:4] == ["12:00-15:30", "15:00-18:30"]
    assert cells(text, "Asia / Tokyo")[2:4] == ["00:00-06:00", "03:00-09:00"]
    assert "Next window" not in text  # live status is only shown for today


def test_friday_cutoff_row_on_a_friday(settings: Settings) -> None:
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE, day=dt.date(2026, 11, 20))
    assert cells(text, "Friday cutoff")[-1] == "applies today"
    assert cells(text, "New York kill zone")[-1] == "on"  # 08:00-11:00 ends before noon


def test_sessions_and_broker_sections_are_plain_ascii(settings: Settings) -> None:
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    text.split("Sessions for", 1)[1].encode("ascii")  # raises if anything is not ASCII


def test_session_lines_without_live_status(settings: Settings) -> None:
    calc = SessionCalculator(settings.config.sessions)
    lines = session_lines(calc, dt.date(2026, 11, 16), settings.display_zone, now=None)
    assert lines[1] == "Sessions for Monday 2026-11-16"
    assert not any(line.startswith("Market") for line in lines)


# --------------------------------------------------------------------------- #
# What the check stores
# --------------------------------------------------------------------------- #


def test_check_stores_candles_a_spread_sample_and_a_log_row(settings: Settings) -> None:
    check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert not settings.db_path.exists()  # synthetic data never reaches the real file
    with Database(settings.mock_db_path) as db:
        assert db.count_candles("XAUUSD", Timeframe.M5) == 200
        assert db.spread_sample_count("XAUUSD") == 1
        assert db.median_spread("XAUUSD", since=utc(2026, 11, 1)) == 20.0
        messages = [row["message"] for row in db.recent_logs()]
    assert "environment check finished" in messages
    log_file = settings.log_dir / "goldsweep.jsonl"
    assert log_file.is_file() and "environment check finished" in log_file.read_text("utf-8")


def test_running_twice_does_not_duplicate_candles(settings: Settings) -> None:
    check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    with Database(settings.mock_db_path) as db:
        assert db.count_candles("XAUUSD", Timeframe.M5) == 200


# --------------------------------------------------------------------------- #
# Mode display
# --------------------------------------------------------------------------- #


def test_disagreeing_mode_switches_are_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODE", "LIVE")
    settings = load_settings(
        DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path / "d", log_dir=tmp_path / "l"
    )
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert value(text, "Mode").startswith("PAPER")
    assert "the two switches disagree, so PAPER applies" in text


def test_live_mode_is_shown_with_the_confirmation_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODE", "LIVE")
    live_config = tmp_path / "config.yaml"
    live_config.write_text("mode: LIVE\n", encoding="utf-8")
    settings = load_settings(
        live_config, read_env_file=False, data_dir=tmp_path / "d", log_dir=tmp_path / "l"
    )
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE)
    assert value(text, "Mode").startswith("LIVE")
    assert "typed confirmation" in value(text, "Mode")


# --------------------------------------------------------------------------- #
# Broker problems
# --------------------------------------------------------------------------- #


def test_missing_broker_fails_the_check_but_still_prints_sessions(settings: Settings) -> None:
    def no_broker() -> BrokerClient:
        raise BrokerUnavailableError("the MetaTrader5 Python package is not installed")

    code, text = check(
        settings, MONDAY_IN_LONDON_KILL_ZONE, use_mock=False, broker_factory=no_broker
    )
    assert code == EXIT_BROKER
    assert "Broker: MetaTrader 5" in text
    assert value(text, "FAILED") == "the MetaTrader5 Python package is not installed"
    assert "python main.py --check --mock" in text
    assert cells(text, "London kill zone")[2] == "07:00-10:00"
    assert "Result: FAILED: the broker step did not complete" in text
    assert "Result: OK" not in text


def test_account_without_gold_fails_after_showing_the_account(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    broker = MockBroker(
        clock=clock, history_days=2, future_days=1, symbols=[default_gold_spec("EURUSD")]
    )
    out = io.StringIO()
    code = run_check(settings, clock=clock, broker_factory=lambda: broker, out=out)
    text = out.getvalue()
    assert code == EXIT_BROKER
    assert value(text, "Account").endswith("[DEMO]")
    assert value(text, "FAILED").startswith("no tradable gold symbol found")
    assert not broker.is_connected()  # always shut down, even after a failure


def test_real_account_gets_a_warning(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    broker = MockBroker(clock=clock, history_days=2, future_days=1, account_type=AccountType.REAL)
    out = io.StringIO()
    code = run_check(settings, clock=clock, broker_factory=lambda: broker, out=out)
    text = out.getvalue()
    assert code == EXIT_OK
    assert value(text, "Account").endswith("[REAL]")
    assert "REAL-money account" in value(text, "WARNING")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    assert "--check" in out and "--mock" in out


def test_check_with_mock_from_the_command_line(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda config_path=None: settings)
    assert main(["--check", "--mock"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Broker: MOCK" in out
    assert "Africa/Kampala" in out
    assert out.rstrip().endswith("Result: OK")


def test_date_option_reaches_the_report(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda config_path=None: settings)
    assert main(["--check", "--mock", "--date", "2026-03-09"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Sessions for Monday 2026-03-09" in out
    # US daylight time has started, UK summer time has not.
    assert cells(out, "New York kill zone")[2] == "12:00-15:00"
    assert cells(out, "London kill zone")[2] == "07:00-10:00"


def test_invalid_config_exits_with_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("risk:\n  risk_per_trade_pct: 5\n", encoding="utf-8")
    assert main(["--check", "--config", str(bad)]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Configuration error" in err
    assert "risk.risk_per_trade_pct" in err


def test_missing_config_exits_with_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--check", "--config", str(tmp_path / "missing.yaml")]) == EXIT_CONFIG
    assert "config file not found" in capsys.readouterr().err


def test_bad_date_is_rejected_by_the_parser(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--check", "--date", "16/11/2026"])
    assert raised.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert "GoldSweep 0.1.0" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The same report through the MetaTrader 5 client (against the fake module)
# --------------------------------------------------------------------------- #


def mt5_check(
    settings: Settings, fake: FakeMT5, clock: ManualClock, **client_options: Any
) -> tuple[int, str]:
    out = io.StringIO()
    code = run_check(
        settings,
        clock=clock,
        out=out,
        broker_factory=lambda: MT5Client(
            mt5=fake, clock=clock, sleep=lambda seconds: None, **client_options
        ),
    )
    return code, out.getvalue()


def fake_terminal(clock: ManualClock, **options: Any) -> FakeMT5:
    """A fake terminal holding seven M5 bars, 08:00 .. 08:30 UTC on Monday 16 November."""
    fake = FakeMT5(clock, **options)
    first = utc(2026, 11, 16, 8, 0)
    fake.add_bars(
        FakeMT5.TIMEFRAME_M5,
        [
            (first + dt.timedelta(minutes=5 * n), 2650.0 + n, 2651.5 + n, 2649.0 + n, 2650.5 + n)
            for n in range(7)
        ],
    )
    return fake


def test_report_through_the_mt5_client(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    code, text = mt5_check(settings, fake_terminal(clock), clock)
    assert code == EXIT_OK
    assert "Broker: MetaTrader 5" in text
    assert value(text, "Terminal") == (
        "MetaTrader 5 build 5200 (MetaQuotes Ltd.); server link up; algo trading enabled; "
        "ping 68.7 ms"  # S2: the terminal's last ping
    )
    assert value(text, "Account") == "5012345 @ Demo-Server  [DEMO]"
    assert value(text, "Server time") == (
        "UTC+02:00 now; rule NY+7 (New York close) (measured from a live tick; confirmed)"
    )
    assert value(text, "Resolved") == "XAUUSD  (Gold vs US Dollar)"
    assert value(text, "Quote") == "bid 2654.31 / ask 2654.52 at 2026-11-16 08:30:00 UTC"
    assert value(text, "Spread") == "21 points (0.21 in price)"
    # The 08:30 bar is still forming, so the newest closed bar is 08:25, shown in true UTC.
    assert value(text, "Last M5 bar").startswith("opened 2026-11-16 08:25 UTC | O 2655.00")
    assert value(text, "Stored") == "6 M5 candles and 1 spread sample in SQLite"
    assert text.rstrip().endswith("Result: OK")
    assert not settings.mock_db_path.exists()  # broker data goes to the real file
    with Database(settings.db_path) as db:
        assert db.count_candles("XAUUSD", Timeframe.M5) == 6


def test_wrong_server_time_setting_fails_the_check(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    fake = fake_terminal(clock)
    fake.pc_clock_behind_seconds = 12.0
    code, text = mt5_check(settings, fake, clock, server_timezone="UTC")  # the server is UTC+2
    assert code == EXIT_BROKER
    assert "NOT confirmed" in value(text, "Server time")
    assert "PC clock differs from the server by +12.0s" in value(text, "Server time")
    after_warning = " ".join(text.split("WARNING", 1)[1].split())  # undo the line folding
    assert "the live server clock is UTC+02:00" in after_warning
    assert "Result: FAILED: the broker server-time setting needs fixing" in text


def test_closed_market_passes_with_a_note_about_unconfirmed_time(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    code, text = mt5_check(settings, fake_terminal(clock, live=False), clock)
    assert code == EXIT_OK
    assert "ASSUMED, not measured yet; NOT confirmed" in value(text, "Server time")
    assert "Result: OK" in text
    assert text.rstrip().endswith("Run the check again while the market is open.")


def test_no_bars_yet_is_reported_plainly(settings: Settings) -> None:
    clock = ManualClock(MONDAY_IN_LONDON_KILL_ZONE)
    code, text = mt5_check(settings, FakeMT5(clock), clock)
    assert code == EXIT_OK
    assert value(text, "Last M5 bar") == "none available yet"
    assert value(text, "Stored") == "0 M5 candles and 1 spread sample in SQLite"


def test_window_after_the_friday_cutoff_is_marked(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        'sessions:\n  newyork: {tz: America/New_York, start: "12:30", end: "14:00", trade: true}\n',
        encoding="utf-8",
    )
    settings = load_settings(
        config, read_env_file=False, data_dir=tmp_path / "d", log_dir=tmp_path / "l"
    )
    _code, text = check(settings, MONDAY_IN_LONDON_KILL_ZONE, day=dt.date(2026, 11, 20))
    assert cells(text, "New York kill zone")[-1] == "no (Friday cutoff)"
    _code, monday = check(settings, MONDAY_IN_LONDON_KILL_ZONE, day=dt.date(2026, 11, 16))
    assert cells(monday, "New York kill zone")[-1] == "on"


def test_connection_failure_gets_advice_that_fits_it(settings: Settings) -> None:
    def cannot_connect() -> BrokerClient:
        raise BrokerConnectionError("could not connect to MetaTrader 5 after 2 attempts")

    code, text = check(
        settings, MONDAY_IN_LONDON_KILL_ZONE, use_mock=False, broker_factory=cannot_connect
    )
    assert code == EXIT_BROKER
    assert value(text, "FAILED") == "could not connect to MetaTrader 5 after 2 attempts"
    assert "Leave all" in text and "python main.py --check --mock" in text
    assert "pip install MetaTrader5" not in text  # the package is clearly there already


def test_long_failure_text_is_folded_onto_aligned_lines(settings: Settings) -> None:
    def cannot_connect() -> BrokerClient:
        raise BrokerConnectionError("the terminal did not answer " * 8)

    _code, text = check(
        settings, MONDAY_IN_LONDON_KILL_ZONE, use_mock=False, broker_factory=cannot_connect
    )
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("FAILED"))
    continuation = lines[first + 1]
    assert continuation.startswith(" " * 14) and continuation.strip()
    assert all(len(line) <= 100 for line in lines[first : first + 3])

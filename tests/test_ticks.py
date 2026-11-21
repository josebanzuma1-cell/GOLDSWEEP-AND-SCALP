"""The tick recorder (S2, D106): every gold tick saved, one file per hour."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from goldsweep.core.clock import ManualClock
from goldsweep.data.broker import BrokerError
from goldsweep.data.mock_broker import MockBroker
from goldsweep.data.mt5_client import MT5Client
from goldsweep.scalping.ticks import (
    BACKFILL,
    HOURS_PER_CALL,
    TickRecorder,
    hour_path,
    load_ticks,
    tick_folder,
)
from goldsweep.storage.db import Database
from tests.fake_mt5 import FakeMT5
from tests.helpers import utc

NOW = utc(2026, 11, 17, 10, 4)  # a Tuesday


@pytest.fixture
def broker() -> MockBroker:
    mock = MockBroker(clock=ManualClock(NOW), history_days=20)
    mock.connect()
    return mock


def recorder(broker: MockBroker, db: Database, folder: Path, **kwargs: int) -> TickRecorder:
    return TickRecorder(broker, "XAUUSD", folder, db, broker._clock, **kwargs)  # noqa: SLF001


def test_the_mock_broker_serves_four_ticks_a_minute(broker: MockBroker) -> None:
    ticks = broker.get_ticks("XAUUSD", utc(2026, 11, 17, 9, 0), utc(2026, 11, 17, 9, 2))
    assert len(ticks) == 8 and ticks.index.tz is not None
    assert list(ticks.index[:4]) == [
        pd.Timestamp("2026-11-17 09:00:00", tz="UTC") + pd.Timedelta(seconds=s)
        for s in (0, 15, 30, 45)
    ]
    assert ((ticks["ask"] - ticks["bid"]).round(2) == 0.20).all()  # 20 points
    with pytest.raises(ValueError, match="after start"):
        broker.get_ticks("XAUUSD", NOW, NOW)


def test_finished_hours_are_saved_once_and_read_back(
    broker: MockBroker, db: Database, tmp_path: Path
) -> None:
    folder = tick_folder(tmp_path, "XAUUSD")
    ticks = recorder(broker, db, folder)
    rounds = 0
    while ticks.saved_until != utc(2026, 11, 17, 10):
        ticks.record()  # the first start fetches the last two weeks, 12 hours a call
        rounds += 1
        assert rounds < 100
    assert rounds == (BACKFILL // dt.timedelta(hours=1)) // HOURS_PER_CALL  # 336 hours: 28
    nine = hour_path(folder, utc(2026, 11, 17, 9))
    assert nine == folder / "2026-11-17" / "09.npz" and nine.exists()
    assert not hour_path(folder, utc(2026, 11, 15, 12)).exists()  # a Sunday: no ticks, no file
    saved = load_ticks(folder, utc(2026, 11, 17, 9), utc(2026, 11, 17, 10))
    served = broker.get_ticks("XAUUSD", utc(2026, 11, 17, 9), utc(2026, 11, 17, 10))
    assert len(saved) == 240
    assert (saved.index == served.index).all() and saved["bid"].tolist() == served["bid"].tolist()
    assert ticks.record() == 0  # nothing new until the next hour has ended (and settled)
    broker._clock.set(utc(2026, 11, 17, 11, 0, 30))  # noqa: SLF001
    assert ticks.record() == 0  # 10:00-11:00 is not settled yet
    broker._clock.set(utc(2026, 11, 17, 11, 1))  # noqa: SLF001
    assert ticks.record() == 1 and hour_path(folder, utc(2026, 11, 17, 10)).exists()


def test_a_restart_carries_on(broker: MockBroker, db: Database, tmp_path: Path) -> None:
    folder = tick_folder(tmp_path, "XAUUSD")
    db.set_state("ticks.XAUUSD.saved_until", utc(2026, 11, 17, 7).isoformat())
    assert recorder(broker, db, folder).record() == 3  # 07, 08 and 09
    assert sorted(p.name for p in (folder / "2026-11-17").iterdir()) == [
        "07.npz",
        "08.npz",
        "09.npz",
    ]


def test_a_broker_error_retries_the_same_hour(db: Database, tmp_path: Path) -> None:
    class Broken(MockBroker):
        def get_ticks(self, symbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
            raise BrokerError("no ticks now")

    broken = Broken(clock=ManualClock(NOW))
    broken.connect()
    db.set_state("ticks.XAUUSD.saved_until", utc(2026, 11, 17, 9).isoformat())
    ticks = TickRecorder(broken, "XAUUSD", tmp_path, db, ManualClock(NOW))
    assert ticks.record() == 0
    assert ticks.saved_until == utc(2026, 11, 17, 9)  # not skipped


def test_old_days_are_removed_when_asked(broker: MockBroker, db: Database, tmp_path: Path) -> None:
    old = tmp_path / "2026-10-01"
    old.mkdir()
    (old / "08.npz").write_bytes(b"")
    (tmp_path / "notes").mkdir()  # not a day: left alone
    db.set_state("ticks.XAUUSD.saved_until", utc(2026, 11, 17, 9).isoformat())
    recorder(broker, db, tmp_path, keep_days=30).record()
    assert not old.exists() and (tmp_path / "notes").exists()
    assert (tmp_path / "2026-11-17" / "09.npz").exists()


def test_nothing_recorded_reads_as_empty(tmp_path: Path) -> None:
    empty = load_ticks(tmp_path, utc(2026, 11, 17, 9), utc(2026, 11, 17, 12))
    assert empty.empty and list(empty.columns) == ["bid", "ask"]


def test_mt5_ticks_come_back_in_utc() -> None:
    clock = ManualClock(NOW)
    fake = FakeMT5(clock, server_offset=dt.timedelta(hours=2))
    fake.ticks = [
        (utc(2026, 11, 17, 9, 0, 1), 2650.10, 2650.34),
        (utc(2026, 11, 17, 9, 0, 2), 2650.20, 2650.44),
        (utc(2026, 11, 17, 10, 0, 0), 2651.00, 2651.24),  # outside the hour
    ]
    client = MT5Client(mt5=fake, clock=clock, sleep=lambda _s: None, server_timezone="+02:00")
    client.connect()
    ticks = client.get_ticks("XAUUSD", utc(2026, 11, 17, 9), utc(2026, 11, 17, 10))
    assert list(ticks.index) == [
        pd.Timestamp("2026-11-17 09:00:01", tz="UTC"),
        pd.Timestamp("2026-11-17 09:00:02", tz="UTC"),
    ]
    assert ticks["ask"].tolist() == [2650.34, 2650.44]
    (call,) = fake.calls_named("copy_ticks_range")
    asked = call[1][1].replace(tzinfo=None)
    assert asked == dt.datetime(2026, 11, 17, 11, 0)  # asked in server time (UTC+2)
    assert client.terminal_info().ping_ms == 68.7
    fake.errors["copy_ticks_range"] = [(-1, "terminal busy")]
    with pytest.raises(BrokerError, match="no ticks"):
        client.get_ticks("XAUUSD", utc(2026, 11, 17, 9), utc(2026, 11, 17, 10))

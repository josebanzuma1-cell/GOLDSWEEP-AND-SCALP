"""SQLite storage: migrations, candles, spread samples, log rows and saved state."""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

import pytest

from goldsweep.core.types import Timeframe
from goldsweep.data.candles import empty_candles
from goldsweep.storage import db as db_module
from goldsweep.storage.db import SCHEMA_VERSION, Database
from tests.helpers import assert_candles_equal, make_candles, utc

START = utc(2026, 11, 16, 8, 0)
M5 = Timeframe.M5


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_new_database_is_migrated_to_the_current_schema(db: Database) -> None:
    assert db.schema_version == SCHEMA_VERSION
    assert set(db.table_names()) >= {"candles", "spread_samples", "app_log", "kv_state"}


def test_parent_folder_is_created(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "er" / "goldsweep.sqlite3"
    with Database(path) as database:
        assert database.schema_version == SCHEMA_VERSION
    assert path.is_file()


def test_reopening_keeps_data_and_does_not_migrate_again(tmp_path: Path) -> None:
    path = tmp_path / "goldsweep.sqlite3"
    with Database(path) as first:
        first.upsert_candles("XAUUSD", M5, make_candles(START, 3))
        first.set_state("paused", True)
    with Database(path) as second:
        assert second.schema_version == SCHEMA_VERSION
        assert second.count_candles("XAUUSD", M5) == 3
        assert second.get_state("paused") is True


def test_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "goldsweep.sqlite3"
    Database(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Database(path)


def test_in_memory_database_works() -> None:
    with Database(":memory:") as database:
        database.set_state("key", {"a": 1})
        assert database.get_state("key") == {"a": 1}


def test_close_is_idempotent(db: Database) -> None:
    db.close()
    db.close()


# --------------------------------------------------------------------------- #
# Candles
# --------------------------------------------------------------------------- #


def test_candles_round_trip(db: Database) -> None:
    frame = make_candles(START, 12)
    assert db.upsert_candles("XAUUSD", M5, frame) == 12
    loaded = db.load_candles("XAUUSD", M5)
    assert_candles_equal(loaded, frame)
    assert loaded.index.tz is not None
    assert loaded.index.name == "time"
    assert str(loaded["tick_volume"].dtype) == "int64"


def test_upsert_is_idempotent_and_updates_changed_bars(db: Database) -> None:
    frame = make_candles(START, 5)
    db.upsert_candles("XAUUSD", M5, frame)
    db.upsert_candles("XAUUSD", M5, frame)
    assert db.count_candles("XAUUSD", M5) == 5

    revised = frame.copy()
    revised.loc[revised.index[-1], "close"] = 2699.0
    db.upsert_candles("XAUUSD", M5, revised.iloc[-2:])
    loaded = db.load_candles("XAUUSD", M5)
    assert len(loaded) == 5
    assert loaded["close"].iloc[-1] == 2699.0
    assert loaded["close"].iloc[0] == frame["close"].iloc[0]


def test_symbols_and_timeframes_are_kept_apart(db: Database) -> None:
    db.upsert_candles("XAUUSD", M5, make_candles(START, 4))
    db.upsert_candles("XAUUSD", Timeframe.M15, make_candles(START, 2, minutes=15))
    db.upsert_candles("XAUUSD.m", M5, make_candles(START, 1))
    assert db.count_candles("XAUUSD", M5) == 4
    assert db.count_candles("XAUUSD", Timeframe.M15) == 2
    assert db.count_candles("XAUUSD.m", M5) == 1
    assert db.count_candles("GOLD", M5) == 0


def test_range_is_start_inclusive_end_exclusive(db: Database) -> None:
    db.upsert_candles("XAUUSD", M5, make_candles(START, 12))  # 08:00 .. 08:55
    loaded = db.load_candles(
        "XAUUSD", M5, start=utc(2026, 11, 16, 8, 10), end=utc(2026, 11, 16, 8, 25)
    )
    assert list(loaded.index) == [
        utc(2026, 11, 16, 8, 10),
        utc(2026, 11, 16, 8, 15),
        utc(2026, 11, 16, 8, 20),
    ]
    # A start between two bars must not pull in the earlier bar.
    after = db.load_candles("XAUUSD", M5, start=utc(2026, 11, 16, 8, 50, 1))
    assert list(after.index) == [utc(2026, 11, 16, 8, 55)]


def test_limit_returns_the_most_recent_bars_oldest_first(db: Database) -> None:
    db.upsert_candles("XAUUSD", M5, make_candles(START, 12))
    loaded = db.load_candles("XAUUSD", M5, limit=3)
    assert list(loaded.index) == [
        utc(2026, 11, 16, 8, 45),
        utc(2026, 11, 16, 8, 50),
        utc(2026, 11, 16, 8, 55),
    ]
    with pytest.raises(ValueError, match="limit"):
        db.load_candles("XAUUSD", M5, limit=0)


def test_nothing_stored_gives_an_empty_frame(db: Database) -> None:
    loaded = db.load_candles("XAUUSD", M5)
    assert loaded.empty
    assert list(loaded.columns) == list(empty_candles().columns)
    assert db.upsert_candles("XAUUSD", M5, empty_candles()) == 0


def test_naive_candles_and_naive_bounds_are_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        db.upsert_candles("XAUUSD", M5, make_candles(START, 3).tz_localize(None))
    with pytest.raises(ValueError, match="naive"):
        db.load_candles("XAUUSD", M5, start=dt.datetime(2026, 11, 16, 8, 0))


def test_candles_given_in_another_zone_are_stored_as_utc(db: Database) -> None:
    db.upsert_candles("XAUUSD", M5, make_candles(START, 2).tz_convert("Africa/Kampala"))
    assert list(db.load_candles("XAUUSD", M5).index) == [START, START + dt.timedelta(minutes=5)]


# --------------------------------------------------------------------------- #
# Spread samples
# --------------------------------------------------------------------------- #


def test_median_spread_is_measured_from_stored_samples(db: Database) -> None:
    for minute, spread in enumerate([18, 20, 22, 60, 19]):
        db.record_spread("XAUUSD", START + dt.timedelta(minutes=minute), spread)
    assert db.spread_sample_count("XAUUSD") == 5
    assert db.median_spread("XAUUSD", since=START) == 20.0
    # Only samples at or after "since" count.
    assert db.median_spread("XAUUSD", since=START + dt.timedelta(minutes=3)) == 39.5
    assert db.spread_sample_count("XAUUSD", since=START + dt.timedelta(minutes=3)) == 2


def test_median_spread_is_unknown_until_enough_samples_exist(db: Database) -> None:
    assert db.median_spread("XAUUSD", since=START) is None
    db.record_spread("XAUUSD", START, 21)
    assert db.median_spread("XAUUSD", since=START, min_samples=2) is None
    assert db.median_spread("XAUUSD", since=START) == 21.0
    assert db.median_spread("GOLD", since=START) is None


def test_two_samples_in_the_same_second_keep_the_latest(db: Database) -> None:
    db.record_spread("XAUUSD", START, 21)
    db.record_spread("XAUUSD", START, 25)
    assert db.spread_sample_count("XAUUSD") == 1
    assert db.median_spread("XAUUSD", since=START) == 25.0


# --------------------------------------------------------------------------- #
# Log rows and saved state
# --------------------------------------------------------------------------- #


def test_log_rows(db: Database) -> None:
    db.write_log(
        time="2026-11-16T08:00:00.000Z", level="INFO", logger="goldsweep.x", message="first"
    )
    db.write_log(
        time="2026-11-16T08:00:01.000Z",
        level="ERROR",
        logger="goldsweep.x",
        message="second",
        data={"code": 10004, "when": utc(2026, 11, 16, 8)},
    )
    newest, oldest = db.recent_logs()
    assert newest["message"] == "second" and newest["level"] == "ERROR"
    assert newest["data"]["code"] == 10004
    assert oldest["message"] == "first" and oldest["data"] == {}
    assert len(db.recent_logs(limit=1)) == 1


def test_state_round_trip_and_overwrite(db: Database) -> None:
    assert db.get_state("missing") is None
    assert db.get_state("missing", default="fallback") == "fallback"
    db.set_state("server_time", {"spec": "NY+7", "offset_minutes": 120})
    assert db.get_state("server_time") == {"spec": "NY+7", "offset_minutes": 120}
    db.set_state("server_time", {"spec": "UTC"})
    assert db.get_state("server_time") == {"spec": "UTC"}
    db.set_state("flag", False)
    assert db.get_state("flag", default=True) is False


def test_concurrent_writers_do_not_corrupt_or_fail(db: Database) -> None:
    errors: list[BaseException] = []

    def worker(number: int) -> None:
        try:
            for step in range(40):
                moment = START + dt.timedelta(seconds=number * 1000 + step)
                db.record_spread("XAUUSD", moment, 20 + number)
                db.set_state(f"worker-{number}", step)
                db.upsert_candles(f"SYM{number}", M5, make_candles(START, 3))
        except BaseException as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert db.spread_sample_count("XAUUSD") == 160
    assert [db.get_state(f"worker-{number}") for number in range(4)] == [39, 39, 39, 39]
    assert all(db.count_candles(f"SYM{number}", M5) == 3 for number in range(4))


# --------------------------------------------------------------------------- #
# All-or-nothing writes
# --------------------------------------------------------------------------- #


def test_failed_batch_leaves_nothing_behind(db: Database) -> None:
    """A batch is atomic: an error half-way through rolls the whole batch back."""
    with pytest.raises(RuntimeError, match="boom"):
        with db._transaction() as conn:
            conn.execute(
                "INSERT INTO spread_samples (symbol, time, spread_points) VALUES ('XAUUSD', 1, 20)"
            )
            raise RuntimeError("boom")
    assert db.spread_sample_count("XAUUSD") == 0
    db.record_spread("XAUUSD", START, 21)  # the connection is still usable afterwards
    assert db.spread_sample_count("XAUUSD") == 1


def test_failed_migration_is_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration that fails half-way must not leave half of its tables behind."""
    broken_script = "CREATE TABLE half_done (id INTEGER); CREATE TABLE half_done (id INTEGER);"
    path = tmp_path / "goldsweep.sqlite3"
    with monkeypatch.context() as patched:
        patched.setattr(
            db_module, "MIGRATIONS", (*db_module.MIGRATIONS, (SCHEMA_VERSION + 1, broken_script))
        )
        patched.setattr(db_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
        with pytest.raises(sqlite3.Error):
            Database(path)
    with Database(path) as database:  # the earlier, good migrations were kept
        assert database.schema_version == SCHEMA_VERSION
        assert "half_done" not in database.table_names()
        assert database.path == str(path)

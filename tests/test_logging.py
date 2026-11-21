"""Structured logging: JSON lines on disk, a copy in SQLite, secrets masked."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import SecretStr

from goldsweep.core.logging import (
    MASK,
    ConsoleFormatter,
    JsonFormatter,
    get_logger,
    record_to_dict,
    redact,
    setup_logging,
    shutdown_logging,
)
from goldsweep.core.types import Timeframe
from goldsweep.storage.db import Database
from tests.helpers import utc


def make_record(msg: str = "hello", level: int = logging.INFO, **fields: Any) -> logging.LogRecord:
    record = logging.LogRecord("goldsweep.test", level, __file__, 10, msg, (), None)
    record.fields = fields
    return record


def read_json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_json_formatter_writes_one_object_per_record() -> None:
    record = logging.LogRecord(
        "goldsweep.test", logging.WARNING, __file__, 10, "order %s", ("sent",), None
    )
    record.fields = {"symbol": "XAUUSD", "lots": 0.12}
    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["msg"] == "order sent"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "goldsweep.test"
    assert payload["symbol"] == "XAUUSD"
    assert payload["lots"] == 0.12


def test_timestamps_are_utc() -> None:
    record = make_record()
    record.created = utc(2026, 11, 16, 8, 30, 15).timestamp() + 0.25
    assert record_to_dict(record)["ts"] == "2026-11-16T08:30:15.250Z"


def test_awkward_values_are_serialised() -> None:
    record = make_record(
        when=utc(2026, 11, 16, 8),
        wait=dt.timedelta(seconds=90),
        price=np.float64(2650.5),
        bars=np.int64(200),
        timeframe=Timeframe.M5,
        folder=Path("data"),
        tags={"b", "a"},
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["when"] == "2026-11-16T08:00:00+00:00"
    assert payload["wait"] == 90.0
    assert payload["price"] == 2650.5
    assert payload["bars"] == 200
    assert payload["timeframe"] == "M5"
    assert payload["folder"] == "data"
    assert payload["tags"] == ["a", "b"]


def test_secrets_are_masked_at_any_depth() -> None:
    record = make_record(
        password="hunter2",
        api_key="sk-live",
        telegram_bot_token="123:abc",
        token="t",
        input_tokens=512,  # an LLM usage count, not a secret
        nested={"mt5_password": "x", "ok": 1, "items": [{"Authorization": "Bearer y"}]},
        secret_object=SecretStr("never-shown"),
    )
    text = JsonFormatter().format(record)
    payload = json.loads(text)
    assert payload["password"] == MASK
    assert payload["api_key"] == MASK
    assert payload["telegram_bot_token"] == MASK
    assert payload["token"] == MASK
    assert payload["input_tokens"] == 512
    assert payload["nested"] == {"mt5_password": MASK, "ok": 1, "items": [{"Authorization": MASK}]}
    for secret in ("hunter2", "sk-live", "123:abc", "Bearer y", "never-shown"):
        assert secret not in text


def test_redact_leaves_plain_values_alone() -> None:
    assert redact({"symbol": "XAUUSD", "lots": 0.1}) == {"symbol": "XAUUSD", "lots": 0.1}
    assert redact("text") == "text"


def test_fields_cannot_overwrite_the_core_keys() -> None:
    record = make_record("real message")
    record.fields = {"ts": "spoofed", "level": "spoofed", "logger": "spoofed", "msg": "spoofed"}
    payload = record_to_dict(record)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "goldsweep.test"
    assert payload["msg"] == "real message"


def test_standard_library_extra_is_kept() -> None:
    record = logging.LogRecord("apscheduler", logging.INFO, __file__, 1, "job ran", (), None)
    record.job_id = "m5-close"
    assert record_to_dict(record)["job_id"] == "m5-close"


def test_console_formatter_is_one_readable_line() -> None:
    line = ConsoleFormatter().format(make_record("connected", symbol="XAUUSD", password="pw"))
    assert "goldsweep.test: connected" in line
    assert 'symbol="XAUUSD"' in line
    assert "pw" not in line.replace("password", "")


def test_file_and_sqlite_receive_the_same_records(tmp_path: Path, db: Database) -> None:
    setup_logging(tmp_path / "logs", db=db, console=False)
    log = get_logger("goldsweep.test")
    log.info("order sent", symbol="XAUUSD", lots=0.12, api_key="sk-secret")
    log.debug("too detailed")  # below the INFO threshold
    log.warning("spread wide", spread_points=55)
    shutdown_logging()

    lines = read_json_lines(tmp_path / "logs" / "goldsweep.jsonl")
    assert [line["msg"] for line in lines] == ["order sent", "spread wide"]
    assert lines[0]["symbol"] == "XAUUSD" and lines[0]["api_key"] == MASK
    assert lines[1]["level"] == "WARNING" and lines[1]["spread_points"] == 55

    rows = db.recent_logs()
    assert [row["message"] for row in rows] == ["spread wide", "order sent"]  # newest first
    assert rows[1]["data"] == {"symbol": "XAUUSD", "lots": 0.12, "api_key": MASK}
    assert rows[1]["logger"] == "goldsweep.test"
    assert rows[1]["time"].endswith("Z")


def test_setup_twice_does_not_duplicate_records(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    setup_logging(tmp_path, console=False)
    get_logger("goldsweep.test").info("once")
    shutdown_logging()
    assert len(read_json_lines(tmp_path / "goldsweep.jsonl")) == 1


def test_nothing_is_written_after_shutdown(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    log = get_logger("goldsweep.test")
    log.info("before")
    shutdown_logging()
    log.info("after")
    assert [line["msg"] for line in read_json_lines(tmp_path / "goldsweep.jsonl")] == ["before"]


def test_exceptions_keep_their_traceback(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    log = get_logger("goldsweep.test")
    try:
        1 / 0  # noqa: B018
    except ZeroDivisionError:
        log.exception("calculation failed", step="sizing")
    shutdown_logging()
    (line,) = read_json_lines(tmp_path / "goldsweep.jsonl")
    assert line["level"] == "ERROR"
    assert line["step"] == "sizing"
    assert "ZeroDivisionError" in line["exc"]


def test_bound_fields_are_added_to_every_record(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    log = get_logger("goldsweep.test").bind(signal_id="2026-11-16-LDN-01")
    assert log.name == "goldsweep.test"
    log.info("alert sent", score=81)
    log.error("order rejected", signal_id="override")
    log.critical("kill switch")
    shutdown_logging()
    first, second, third = read_json_lines(tmp_path / "goldsweep.jsonl")
    assert first["signal_id"] == "2026-11-16-LDN-01" and first["score"] == 81
    assert second["signal_id"] == "override"
    assert third["level"] == "CRITICAL" and third["signal_id"] == "2026-11-16-LDN-01"


def test_sqlite_level_can_differ_from_the_file_level(tmp_path: Path, db: Database) -> None:
    setup_logging(tmp_path, level="DEBUG", db=db, db_level="WARNING", console=False)
    log = get_logger("goldsweep.test")
    log.debug("detail")
    log.warning("important")
    shutdown_logging()
    assert len(read_json_lines(tmp_path / "goldsweep.jsonl")) == 2
    assert [row["message"] for row in db.recent_logs()] == ["important"]


def test_console_output_goes_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(None, console=True, console_level="WARNING")
    log = get_logger("goldsweep.test")
    log.info("quiet")
    log.warning("loud", symbol="XAUUSD")
    shutdown_logging()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "loud" in captured.err and "quiet" not in captured.err


def test_exception_and_stack_text_reach_file_and_console() -> None:
    try:
        raise ValueError("bad value")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("goldsweep.test", logging.ERROR, __file__, 1, "failed", (), exc_info)
    record.stack_info = "Stack (most recent call last):\n  somewhere"
    payload = record_to_dict(record)
    assert "ValueError: bad value" in payload["exc"]
    assert payload["stack"].startswith("Stack")
    console = ConsoleFormatter().format(record)
    assert console.splitlines()[0].endswith("goldsweep.test: failed")
    assert "ValueError: bad value" in console


def test_enums_and_unusual_objects_are_serialised() -> None:
    class Side(Enum):
        LONG = 1

    class Odd:
        def item(self) -> float:
            raise RuntimeError("no scalar value")

        def __str__(self) -> str:
            return "odd-object"

    payload = json.loads(JsonFormatter().format(make_record(side=Side.LONG, odd=Odd())))
    assert payload["side"] == 1
    assert payload["odd"] == "odd-object"


def test_a_failing_database_never_breaks_logging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class BrokenDatabase:
        def write_log(self, **row: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

    setup_logging(tmp_path, db=BrokenDatabase(), console=False)  # type: ignore[arg-type]
    get_logger("goldsweep.test").info("still written to the file")
    shutdown_logging()
    lines = read_json_lines(tmp_path / "goldsweep.jsonl")
    assert [line["msg"] for line in lines] == ["still written to the file"]
    assert "database is locked" in capsys.readouterr().err  # reported, not raised

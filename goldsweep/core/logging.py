"""Structured logging: JSON lines in a daily file, plus a copy in SQLite.

Usage::

    from goldsweep.core.logging import get_logger

    log = get_logger(__name__)
    log.info("order sent", symbol="XAUUSD", lots=0.12)

Each record becomes one JSON object (``ts`` in UTC, ``level``, ``logger``,
``msg`` and the keyword fields). Values under keys that look like secrets
(password, token, api_key, ...) are masked before anything is written.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import sys
import traceback
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from goldsweep.storage.db import Database

_HANDLER_FLAG = "_goldsweep_handler"
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "fields",
    "taskName",
}
_SENSITIVE_PARTS = ("password", "passwd", "secret", "api_key", "apikey", "authorization")
_NOISY_LIBRARIES = ("urllib3", "httpx", "httpcore", "apscheduler", "asyncio")
MASK = "***"


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_PARTS):
        return True
    # "token" and "bot_token" are secrets; "input_tokens" (an LLM usage count) is not.
    return lowered == "token" or lowered.endswith("_token")


def redact(value: Any) -> Any:
    """Mask values stored under secret-looking keys, at any nesting depth."""
    if isinstance(value, Mapping):
        return {
            str(key): (MASK if _is_sensitive(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def _json_default(obj: Any) -> Any:
    """Serialise the non-JSON types that show up in log fields."""
    if isinstance(obj, dt.datetime | dt.date | dt.time):
        return obj.isoformat()
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, set | frozenset):
        return sorted(str(item) for item in obj)
    item = getattr(obj, "item", None)  # numpy scalars
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(obj)  # Path, SecretStr (prints as asterisks), anything else


def _iso_utc(epoch_seconds: float) -> str:
    moment = dt.datetime.fromtimestamp(epoch_seconds, tz=dt.UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    """Flatten a log record into a JSON-ready dict with secrets masked."""
    payload: dict[str, Any] = {
        "ts": _iso_utc(record.created),
        "level": record.levelname,
        "logger": record.name,
        "msg": record.getMessage(),
    }
    extras: dict[str, Any] = {}
    fields = getattr(record, "fields", None)
    if isinstance(fields, Mapping):
        extras.update(fields)
    for key, value in record.__dict__.items():
        if key not in _STANDARD_ATTRS and not key.startswith("_"):
            extras.setdefault(key, value)
    for key, value in redact(extras).items():
        payload.setdefault(key, value)  # never overwrite ts / level / logger / msg
    if record.exc_info and record.exc_info[0] is not None:
        payload["exc"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
    if record.stack_info:
        payload["stack"] = record.stack_info
    return payload


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(record_to_dict(record), default=_json_default, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single line: time, level, logger, message, then ``key=value`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        data = record_to_dict(record)
        line = f"{data.pop('ts')} {data.pop('level'):<8} {data.pop('logger')}: {data.pop('msg')}"
        exc = data.pop("exc", None)
        data.pop("stack", None)
        if data:
            line += "  " + " ".join(
                f"{key}={json.dumps(value, default=_json_default, ensure_ascii=False)}"
                for key, value in data.items()
            )
        if exc:
            line += "\n" + exc
        return line


class SQLiteLogHandler(logging.Handler):
    """Copies log records into the ``app_log`` table."""

    def __init__(self, db: Database, level: int | str = logging.INFO) -> None:
        super().__init__(level)
        self._db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = record_to_dict(record)
            self._db.write_log(
                time=payload.pop("ts"),
                level=payload.pop("level"),
                logger=payload.pop("logger"),
                message=payload.pop("msg"),
                data=payload or None,
            )
        except Exception:
            self.handleError(record)


def shutdown_logging() -> None:
    """Remove and close every handler installed by :func:`setup_logging`."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def setup_logging(
    log_dir: Path | None = None,
    *,
    level: int | str = logging.INFO,
    db: Database | None = None,
    db_level: int | str = logging.INFO,
    console: bool = True,
    console_level: int | str | None = None,
    file_name: str = "goldsweep.jsonl",
    backup_days: int = 30,
) -> None:
    """Configure logging for the process. Safe to call more than once.

    Args:
        log_dir: folder for the JSON-lines file, rotated daily at midnight UTC.
            ``None`` disables file logging.
        level: minimum level for the file (and the console unless overridden).
        db: when given, records at ``db_level`` and above are also stored in SQLite.
        console: also print human-readable lines to stderr.
        console_level: minimum level for the console; defaults to ``level``.
        file_name: log file name. Each process should use its own.
        backup_days: how many rotated daily files to keep.
    """
    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(level)
    handlers: list[logging.Handler] = []

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / file_name,
            when="midnight",
            utc=True,
            backupCount=backup_days,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(JsonFormatter())
        handlers.append(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(ConsoleFormatter())
        if console_level is not None:
            console_handler.setLevel(console_level)
        handlers.append(console_handler)

    if db is not None:
        handlers.append(SQLiteLogHandler(db, level=db_level))

    for handler in handlers:
        setattr(handler, _HANDLER_FLAG, True)
        root.addHandler(handler)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


class StructuredLogger:
    """Thin wrapper that turns keyword arguments into structured log fields."""

    __slots__ = ("_bound", "_logger")

    def __init__(self, logger: logging.Logger, bound: Mapping[str, Any] | None = None) -> None:
        self._logger = logger
        self._bound: dict[str, Any] = dict(bound or {})

    @property
    def name(self) -> str:
        return self._logger.name

    def bind(self, **fields: Any) -> StructuredLogger:
        """Return a logger that adds ``fields`` to every record."""
        return StructuredLogger(self._logger, {**self._bound, **fields})

    def _log(self, level: int, msg: str, fields: dict[str, Any], *, exc_info: bool = False) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(
                level,
                msg,
                extra={"fields": {**self._bound, **fields}},
                exc_info=exc_info,
                stacklevel=3,
            )

    def debug(self, msg: str, /, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, fields)

    def info(self, msg: str, /, **fields: Any) -> None:
        self._log(logging.INFO, msg, fields)

    def warning(self, msg: str, /, **fields: Any) -> None:
        self._log(logging.WARNING, msg, fields)

    def error(self, msg: str, /, **fields: Any) -> None:
        self._log(logging.ERROR, msg, fields)

    def critical(self, msg: str, /, **fields: Any) -> None:
        self._log(logging.CRITICAL, msg, fields)

    def exception(self, msg: str, /, **fields: Any) -> None:
        """Log at ERROR level with the current exception's traceback attached."""
        self._log(logging.ERROR, msg, fields, exc_info=True)


def get_logger(name: str) -> StructuredLogger:
    """Structured logger for module ``name``."""
    return StructuredLogger(logging.getLogger(name))

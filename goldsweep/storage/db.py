"""SQLite storage.

One database file and one connection, guarded by a lock so that scheduler
threads (Phase 5) can share it safely.

Schema changes go through numbered migrations recorded in ``PRAGMA
user_version``. Phase 1 creates what Phase 1 uses: candles, spread samples,
the application log and a small key/value state store. Tables for news,
calendar events, signals and trades are added by later migrations when those
phases are built, so their columns are designed with the code that uses them.

All times are stored in UTC: integer epoch seconds for candles and spread
samples, ISO-8601 text for log rows.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import statistics
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from goldsweep.core.clock import ensure_utc, utc_now
from goldsweep.core.types import Timeframe
from goldsweep.data.candles import (
    empty_candles,
    from_epoch_seconds,
    to_epoch_seconds,
    validate_candles,
)

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE candles (
            symbol      TEXT    NOT NULL,
            timeframe   TEXT    NOT NULL,
            time        INTEGER NOT NULL,  -- bar open, epoch seconds UTC
            open        REAL    NOT NULL,
            high        REAL    NOT NULL,
            low         REAL    NOT NULL,
            close       REAL    NOT NULL,
            tick_volume INTEGER NOT NULL DEFAULT 0,
            spread      INTEGER NOT NULL DEFAULT 0,  -- broker points
            PRIMARY KEY (symbol, timeframe, time)
        ) WITHOUT ROWID;

        CREATE TABLE spread_samples (
            symbol        TEXT    NOT NULL,
            time          INTEGER NOT NULL,  -- epoch seconds UTC
            spread_points REAL    NOT NULL,
            PRIMARY KEY (symbol, time)
        ) WITHOUT ROWID;

        CREATE TABLE app_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            time    TEXT NOT NULL,  -- ISO-8601 UTC
            level   TEXT NOT NULL,
            logger  TEXT NOT NULL,
            message TEXT NOT NULL,
            data    TEXT            -- JSON object with the structured fields
        );
        CREATE INDEX idx_app_log_time ON app_log (time);

        CREATE TABLE kv_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,  -- JSON
            updated_at TEXT NOT NULL   -- ISO-8601 UTC
        ) WITHOUT ROWID;
        """,
    ),
    (
        2,
        """
        CREATE TABLE calendar_events (
            source     TEXT    NOT NULL,
            event_id   TEXT    NOT NULL,
            time       INTEGER NOT NULL,  -- release, epoch seconds UTC
            currency   TEXT    NOT NULL,
            title      TEXT    NOT NULL,
            impact     TEXT    NOT NULL,  -- high / medium / low / holiday / none
            forecast   TEXT,
            previous   TEXT,
            actual     TEXT,
            all_day    INTEGER NOT NULL DEFAULT 0,
            url        TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (source, event_id)
        ) WITHOUT ROWID;
        CREATE INDEX idx_calendar_events_time ON calendar_events (time);

        CREATE TABLE headlines (
            key           TEXT PRIMARY KEY,  -- hash of the normalised title
            source        TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            summary       TEXT,
            url           TEXT,
            published     INTEGER NOT NULL,  -- epoch seconds UTC
            fetched       INTEGER NOT NULL,
            relevant      INTEGER,           -- classification, NULL until classified
            direction     TEXT,
            impact        TEXT,
            drivers       TEXT,              -- JSON list
            confidence    REAL,
            reason        TEXT,
            method        TEXT,              -- llm / rules / skipped
            model         TEXT,
            classified_at INTEGER
        ) WITHOUT ROWID;
        CREATE INDEX idx_headlines_published ON headlines (published);

        CREATE TABLE llm_usage (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            time               INTEGER NOT NULL,  -- epoch seconds UTC
            day                TEXT    NOT NULL,  -- UTC date, YYYY-MM-DD
            purpose            TEXT    NOT NULL,  -- headline / scenario
            model              TEXT    NOT NULL,
            input_tokens       INTEGER NOT NULL DEFAULT 0,
            output_tokens      INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
            cost_usd           REAL    NOT NULL DEFAULT 0,
            outcome            TEXT    NOT NULL   -- ok / invalid / refused / error
        );
        CREATE INDEX idx_llm_usage_day ON llm_usage (day);

        CREATE TABLE scenarios (
            key     TEXT PRIMARY KEY,  -- one brief per trigger
            time    INTEGER NOT NULL,  -- when it was built, epoch seconds UTC
            trigger TEXT    NOT NULL,
            method  TEXT    NOT NULL,  -- llm / template
            body    TEXT    NOT NULL   -- JSON
        ) WITHOUT ROWID;
        """,
    ),
    (
        3,
        """
        CREATE TABLE signals (
            id          TEXT PRIMARY KEY,  -- e.g. 2026-11-17-LDN-01
            time        INTEGER NOT NULL,  -- bar close, epoch seconds UTC
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            entry       REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            take_profit_1 REAL  NOT NULL,
            take_profit_2 REAL,
            risk_reward REAL    NOT NULL,
            action      TEXT    NOT NULL,  -- traded / blocked / alert / below_alert / ...
            detail      TEXT               -- JSON: swept pool, notes, pressure, reasons
        ) WITHOUT ROWID;
        CREATE INDEX idx_signals_time ON signals (time);

        CREATE TABLE paper_trades (
            id          TEXT PRIMARY KEY,  -- the signal id
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            signal_time INTEGER NOT NULL,
            fill_time   INTEGER NOT NULL,
            exit_time   INTEGER NOT NULL,
            fill_price  REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            take_profit_1 REAL  NOT NULL,
            take_profit_2 REAL,
            lots        REAL    NOT NULL,
            risk_amount REAL    NOT NULL,
            exit_reason TEXT    NOT NULL,
            net_pnl     REAL    NOT NULL,
            r_multiple  REAL    NOT NULL,
            legs        TEXT    NOT NULL   -- JSON
        ) WITHOUT ROWID;
        CREATE INDEX idx_paper_trades_exit ON paper_trades (exit_time);
        """,
    ),
    (
        4,
        """
        -- Phase 6: trades on the broker, and every order request with its answer.
        CREATE TABLE live_trades (
            id          TEXT PRIMARY KEY,  -- the signal id
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            signal_time INTEGER NOT NULL,
            fill_time   INTEGER NOT NULL,
            exit_time   INTEGER NOT NULL,
            fill_price  REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            take_profit_1 REAL  NOT NULL,
            take_profit_2 REAL,
            lots        REAL    NOT NULL,
            risk_amount REAL    NOT NULL,
            exit_reason TEXT    NOT NULL,
            net_pnl     REAL    NOT NULL,
            r_multiple  REAL    NOT NULL,
            legs        TEXT    NOT NULL   -- JSON
        ) WITHOUT ROWID;
        CREATE INDEX idx_live_trades_exit ON live_trades (exit_time);

        CREATE TABLE order_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            time        INTEGER NOT NULL,
            signal_id   TEXT,
            action      TEXT    NOT NULL,  -- market / limit / modify / close / cancel / *_check
            ok          INTEGER NOT NULL,
            retcode     INTEGER NOT NULL,
            ticket      INTEGER,
            message     TEXT,
            request     TEXT    NOT NULL   -- JSON
        );
        CREATE INDEX idx_order_log_time ON order_log (time);
        """,
    ),
    (
        5,
        """
        -- Phase 7: commands from the dashboard, carried out by the running bot.
        CREATE TABLE commands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created     INTEGER NOT NULL,
            source      TEXT    NOT NULL,  -- dashboard
            name        TEXT    NOT NULL,  -- pause / resume / stop / status
            status      TEXT    NOT NULL DEFAULT 'pending',  -- pending / done / expired
            finished    INTEGER,
            reply       TEXT
        );
        CREATE INDEX idx_commands_status ON commands (status);
        """,
    ),
    (
        6,
        """
        -- Phase 10: monitored A+ setups, traded on the shadow book (never sent).
        CREATE TABLE shadow_trades (
            id          TEXT PRIMARY KEY,  -- the signal id
            playbook    TEXT    NOT NULL,  -- asia / london / newyork
            fingerprint TEXT    NOT NULL,  -- the candidate rule it was monitored for
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            signal_time INTEGER NOT NULL,
            fill_time   INTEGER NOT NULL,
            exit_time   INTEGER NOT NULL,
            fill_price  REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            exit_reason TEXT    NOT NULL,
            net_pnl     REAL    NOT NULL,
            r_multiple  REAL    NOT NULL
        ) WITHOUT ROWID;
        CREATE INDEX idx_shadow_trades_playbook ON shadow_trades (playbook, fingerprint);
        """,
    ),
    (
        7,
        """
        -- Scalper S2: how long each order_send took, request to answer (milliseconds).
        ALTER TABLE order_log ADD COLUMN elapsed_ms REAL;
        """,
    ),
    (
        8,
        """
        -- Scalper S5 (D111): its closed trades, on paper and on the demo account,
        -- in the layout of paper_trades.
        CREATE TABLE scalp_trades (
            id          TEXT PRIMARY KEY,
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            signal_time INTEGER NOT NULL,
            fill_time   INTEGER NOT NULL,
            exit_time   INTEGER NOT NULL,
            fill_price  REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            take_profit_1 REAL  NOT NULL,
            take_profit_2 REAL,
            lots        REAL    NOT NULL,
            risk_amount REAL    NOT NULL,
            exit_reason TEXT    NOT NULL,
            net_pnl     REAL    NOT NULL,
            r_multiple  REAL    NOT NULL,
            legs        TEXT    NOT NULL
        ) WITHOUT ROWID;
        CREATE INDEX idx_scalp_trades_exit ON scalp_trades (exit_time);
        CREATE TABLE scalp_live_trades (
            id          TEXT PRIMARY KEY,
            direction   TEXT    NOT NULL,
            session     TEXT,
            score       REAL    NOT NULL,
            signal_time INTEGER NOT NULL,
            fill_time   INTEGER NOT NULL,
            exit_time   INTEGER NOT NULL,
            fill_price  REAL    NOT NULL,
            stop_loss   REAL    NOT NULL,
            take_profit_1 REAL  NOT NULL,
            take_profit_2 REAL,
            lots        REAL    NOT NULL,
            risk_amount REAL    NOT NULL,
            exit_reason TEXT    NOT NULL,
            net_pnl     REAL    NOT NULL,
            r_multiple  REAL    NOT NULL,
            legs        TEXT    NOT NULL
        ) WITHOUT ROWID;
        CREATE INDEX idx_scalp_live_trades_exit ON scalp_live_trades (exit_time);
        """,
    ),
)

SCHEMA_VERSION: int = MIGRATIONS[-1][0]
_MEMORY = ":memory:"


class Database:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._closed = False
        if self._path != _MEMORY:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, with explicit BEGIN/COMMIT for batches.
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self._path != _MEMORY:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        try:
            self._migrate()
        except BaseException:
            self._conn.close()  # do not leave the file locked when startup is refused
            raise

    # -- lifecycle --------------------------------------------------------- #

    @property
    def path(self) -> str:
        return self._path

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self._lock:
            current = self.schema_version
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema v{current} is newer than this code (v{SCHEMA_VERSION}); "
                    "update GoldSweep before opening it"
                )
            for version, script in MIGRATIONS:
                if version <= current:
                    continue
                try:
                    self._conn.executescript(
                        f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except sqlite3.Error:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements atomically. Do not nest."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # -- plain SQL, for the services that own the newer tables ------------ #

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one statement; returns the number of rows changed."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        """Run one statement for many rows, atomically; returns the rows changed."""
        with self._transaction() as conn:
            return conn.executemany(sql, [tuple(row) for row in rows]).rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return [tuple(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    def table_names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return [str(row[0]) for row in rows]

    # -- candles ----------------------------------------------------------- #

    def upsert_candles(self, symbol: str, timeframe: Timeframe, candles: pd.DataFrame) -> int:
        """Insert bars, replacing any already stored for the same open time.

        Returns the number of rows written.
        """
        frame = validate_candles(candles)
        if frame.empty:
            return 0
        count = len(frame)
        rows = list(
            zip(
                [symbol] * count,
                [timeframe.value] * count,
                to_epoch_seconds(frame.index).tolist(),
                frame["open"].tolist(),
                frame["high"].tolist(),
                frame["low"].tolist(),
                frame["close"].tolist(),
                frame["tick_volume"].tolist(),
                frame["spread"].tolist(),
                strict=True,
            )
        )
        with self._transaction() as conn:
            conn.executemany(
                """
                INSERT INTO candles
                    (symbol, timeframe, time, open, high, low, close, tick_volume, spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, timeframe, time) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    tick_volume = excluded.tick_volume,
                    spread = excluded.spread
                """,
                rows,
            )
        return count

    def load_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Stored bars with open time in ``[start, end)``, oldest first.

        ``limit`` keeps only the most recent ``limit`` bars of that range.
        """
        clauses = ["symbol = ?", "timeframe = ?"]
        params: list[Any] = [symbol, timeframe.value]
        if start is not None:
            clauses.append("time >= ?")
            params.append(math.ceil(ensure_utc(start).timestamp()))
        if end is not None:
            clauses.append("time < ?")
            params.append(math.ceil(ensure_utc(end).timestamp()))
        sql = (
            "SELECT time, open, high, low, close, tick_volume, spread FROM candles "
            f"WHERE {' AND '.join(clauses)}"
        )
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            sql += " ORDER BY time DESC LIMIT ?"
            params.append(int(limit))
        else:
            sql += " ORDER BY time ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return empty_candles()
        if limit is not None:
            rows.reverse()
        times, opens, highs, lows, closes, volumes, spreads = zip(*rows, strict=True)
        return pd.DataFrame(
            {
                "open": np.asarray(opens, dtype="float64"),
                "high": np.asarray(highs, dtype="float64"),
                "low": np.asarray(lows, dtype="float64"),
                "close": np.asarray(closes, dtype="float64"),
                "tick_volume": np.asarray(volumes, dtype="int64"),
                "spread": np.asarray(spreads, dtype="int64"),
            },
            index=from_epoch_seconds(times),
        )

    def count_candles(self, symbol: str, timeframe: Timeframe) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM candles WHERE symbol = ? AND timeframe = ?",
                (symbol, timeframe.value),
            ).fetchone()
        return int(row[0])

    # -- spread samples ---------------------------------------------------- #

    def record_spread(self, symbol: str, moment: dt.datetime, spread_points: float) -> None:
        """Store one spread observation (the app measures its own typical spread)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO spread_samples (symbol, time, spread_points) "
                "VALUES (?, ?, ?)",
                (symbol, int(ensure_utc(moment).timestamp()), float(spread_points)),
            )

    def spread_sample_count(self, symbol: str, *, since: dt.datetime | None = None) -> int:
        sql = "SELECT COUNT(*) FROM spread_samples WHERE symbol = ?"
        params: list[Any] = [symbol]
        if since is not None:
            sql += " AND time >= ?"
            params.append(int(ensure_utc(since).timestamp()))
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0])

    def median_spread(
        self, symbol: str, *, since: dt.datetime, min_samples: int = 1
    ) -> float | None:
        """Median spread in points over samples taken at or after ``since``.

        Returns ``None`` when fewer than ``min_samples`` observations exist, so a
        caller can tell "not measured yet" from a real value.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT spread_points FROM spread_samples WHERE symbol = ? AND time >= ?",
                (symbol, int(ensure_utc(since).timestamp())),
            ).fetchall()
        values = [float(row[0]) for row in rows]
        if len(values) < max(1, min_samples):
            return None
        return float(statistics.median(values))

    # -- application log --------------------------------------------------- #

    def write_log(
        self,
        *,
        time: str,
        level: str,
        logger: str,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        payload = json.dumps(data, default=str, ensure_ascii=False) if data else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_log (time, level, logger, message, data) VALUES (?, ?, ?, ?, ?)",
                (time, level, logger, message, payload),
            )

    def recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest ``limit`` log rows, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT time, level, logger, message, data FROM app_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "time": row[0],
                "level": row[1],
                "logger": row[2],
                "message": row[3],
                "data": json.loads(row[4]) if row[4] else {},
            }
            for row in rows
        ]

    # -- key/value state --------------------------------------------------- #

    def backup(self, target: Path | str) -> Path:
        """A consistent copy of the whole database (safe while the bot is writing)."""
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            copy = sqlite3.connect(str(path))
            try:
                self._conn.backup(copy)
            finally:
                copy.close()
        return path

    def prune_logs(self, before: dt.datetime) -> int:
        """Remove log rows older than ``before``; returns how many went."""
        # The same text form the log writer uses: 2026-11-18T15:45:54.055Z
        cutoff = ensure_utc(before).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._lock:
            cursor = self._conn.execute("DELETE FROM app_log WHERE time < ?", (cutoff,))
            return int(cursor.rowcount or 0)

    def set_state(self, key: str, value: Any) -> None:
        """Remember a JSON-serialisable value across restarts."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, default=str), utc_now().isoformat()),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

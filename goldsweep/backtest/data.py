"""Price history for the backtester: download from MetaTrader 5, store, load, describe.

History is downloaded one calendar month at a time and stored in the SQLite
candles table, so a later run only needs the database. How far back MT5 can go
depends on the broker and on the terminal's *Max bars in chart* setting
(Tools > Options > Charts); set it to "Unlimited" before downloading years of
M1 or M5 bars. Months that come back empty are listed in the download report.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from goldsweep.core.clock import ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, SymbolSpec, TradeMode
from goldsweep.storage.db import Database

log = get_logger(__name__)

SPEC_STATE_KEY = "backtest.symbol_spec"


@dataclass(frozen=True, slots=True)
class DownloadReport:
    symbol: str
    timeframe: Timeframe
    start: dt.datetime
    end: dt.datetime
    rows: int
    empty_months: tuple[str, ...]  # "YYYY-MM" months the broker returned nothing for

    def describe(self) -> str:
        span = f"{self.start:%Y-%m-%d} .. {self.end:%Y-%m-%d}"
        text = f"{self.timeframe.value}: {self.rows:,} bars for {span}"
        if self.empty_months:
            text += f"; nothing returned for {', '.join(self.empty_months)}"
        return text


def month_chunks(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """``[start, end)`` cut at calendar-month boundaries (UTC)."""
    start, end = ensure_utc(start), ensure_utc(end)
    chunks: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start
    while cursor < end:
        year, month = (
            (cursor.year + 1, 1) if cursor.month == 12 else (cursor.year, cursor.month + 1)
        )
        boundary = dt.datetime(year, month, 1, tzinfo=dt.UTC)
        chunks.append((cursor, min(boundary, end)))
        cursor = boundary
    return chunks


def download_history(
    broker: BrokerClient,
    db: Database,
    symbol: str,
    timeframe: Timeframe,
    start: dt.datetime,
    end: dt.datetime,
    *,
    progress: Callable[[str], None] | None = None,
) -> DownloadReport:
    """Fetch closed bars of ``[start, end)`` month by month and store them (idempotent)."""
    rows = 0
    empty: list[str] = []
    for first, last in month_chunks(start, end):
        frame = broker.get_rates_range(symbol, timeframe, first, last)
        if frame.empty:
            empty.append(f"{first:%Y-%m}")
        else:
            rows += db.upsert_candles(symbol, timeframe, frame)
        if progress is not None:
            progress(f"{timeframe.value} {first:%Y-%m}: {len(frame):,} bars")
    report = DownloadReport(
        symbol, timeframe, ensure_utc(start), ensure_utc(end), rows, tuple(empty)
    )
    log.info("history downloaded", summary=report.describe())
    return report


def save_symbol_spec(db: Database, spec: SymbolSpec) -> None:
    """Remember the broker's contract details, so a later offline run sizes trades the same way."""
    db.set_state(SPEC_STATE_KEY, dataclasses.asdict(spec))


def load_symbol_spec(db: Database) -> SymbolSpec | None:
    data = db.get_state(SPEC_STATE_KEY)
    if not data:
        return None
    data = dict(data)
    data["trade_mode"] = TradeMode(data["trade_mode"])
    return SymbolSpec(**data)


def fingerprint(*frames: pd.DataFrame | None) -> str:
    """A short hash of the bars, so a report names exactly the data it came from."""
    digest = hashlib.sha256()
    for frame in frames:
        if frame is None:
            digest.update(b"none")
            continue
        digest.update(frame.index.as_unit("s").asi8.astype("int64").tobytes())
        for column in ("open", "high", "low", "close", "tick_volume", "spread"):
            digest.update(np.ascontiguousarray(frame[column].to_numpy()).tobytes())
    return digest.hexdigest()[:16]


def days_without_bars(candles: pd.DataFrame, start: dt.datetime, end: dt.datetime) -> list[dt.date]:
    """Weekdays (Monday to Friday, UTC) in ``[start, end)`` with no bar at all.

    Market holidays show up here too; long runs of missing days mean missing data.
    """
    first, last = ensure_utc(start).date(), ensure_utc(end).date()
    present = set(candles.index.date) if not candles.empty else set()
    missing: list[dt.date] = []
    day = first
    while day < last:
        if day.weekday() < 5 and day not in present:
            missing.append(day)
        day += dt.timedelta(days=1)
    return missing


def summarize_gaps(days: Sequence[dt.date], limit: int = 8) -> str:
    """ "2024-12-25, 2025-01-01 and 3 more" style text."""
    if not days:
        return "none"
    shown = ", ".join(f"{day:%Y-%m-%d}" for day in days[:limit])
    extra = len(days) - limit
    return shown + (f" and {extra} more" if extra > 0 else "")

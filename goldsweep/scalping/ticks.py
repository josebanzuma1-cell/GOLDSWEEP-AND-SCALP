"""The tick recorder (D106): every gold tick, saved for testing the tick mode.

The broker serves only about two weeks of tick history, too little to judge a mode
that trades in seconds. So the bot saves every bid/ask change of the gold symbol,
one compressed file per hour::

    data/ticks/<symbol>/<YYYY-MM-DD>/<HH>.npz   (UTC; arrays time_ms, bid, ask)

An hour is saved once it has ended (plus a minute for late ticks). Progress is kept
in the database, so a restart carries on where it stopped, and anything missed while
the bot was down is fetched again as long as the broker still has it (on the first
start, the broker's whole history: about two weeks). An hour without ticks (the
weekend) leaves no file. ``keep_days`` removes old days; 0 keeps them all.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from goldsweep.core.clock import Clock, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.data.broker import BrokerClient, BrokerError, empty_ticks
from goldsweep.storage.db import Database

log = get_logger(__name__)

HOUR = dt.timedelta(hours=1)
#: Ticks of the last seconds of an hour can reach the terminal a little late.
SETTLE = dt.timedelta(minutes=1)
#: How far back the first start looks (the broker keeps about two weeks).
BACKFILL = dt.timedelta(days=14)
#: Hours fetched per call, so one call never holds the trading lock for long.
HOURS_PER_CALL = 12


def tick_folder(data_dir: Path, symbol: str) -> Path:
    return data_dir / "ticks" / symbol


def hour_path(folder: Path, hour: dt.datetime) -> Path:
    moment = ensure_utc(hour)
    return folder / f"{moment:%Y-%m-%d}" / f"{moment:%H}.npz"


def save_hour(folder: Path, hour: dt.datetime, ticks: pd.DataFrame) -> Path:
    """Write one hour of ticks (UTC epoch milliseconds, bid, ask)."""
    path = hour_path(folder, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamps = np.asarray(ticks.index.as_unit("ms").asi8, dtype="int64")
    np.savez_compressed(
        path,
        time_ms=stamps,
        bid=ticks["bid"].to_numpy(dtype="float64"),
        ask=ticks["ask"].to_numpy(dtype="float64"),
    )
    return path


def load_ticks(folder: Path, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """The recorded ticks in ``[start, end)``, oldest first (hours with no file: none)."""
    first, last = ensure_utc(start), ensure_utc(end)
    hour = first.replace(minute=0, second=0, microsecond=0)
    parts: list[pd.DataFrame] = []
    while hour < last:
        path = hour_path(folder, hour)
        if path.exists():
            with np.load(path) as data:
                index = pd.to_datetime(data["time_ms"], unit="ms", utc=True)
                parts.append(
                    pd.DataFrame(
                        {"bid": data["bid"], "ask": data["ask"]},
                        index=pd.DatetimeIndex(index, name="time"),
                    )
                )
        hour += HOUR
    if not parts:
        return empty_ticks()
    frame = pd.concat(parts)
    return frame.loc[(frame.index >= first) & (frame.index < last)]


class TickRecorder:
    """Saves each finished hour of ticks; call :meth:`record` every minute or so."""

    def __init__(
        self,
        broker: BrokerClient,
        symbol: str,
        folder: Path,
        db: Database,
        clock: Clock,
        *,
        keep_days: int = 0,
    ) -> None:
        self._broker = broker
        self._symbol = symbol
        self.folder = folder
        self._db = db
        self._clock = clock
        self._keep_days = keep_days
        self._state = f"ticks.{symbol}.saved_until"

    @property
    def saved_until(self) -> dt.datetime | None:
        """Every hour before this one has been handled."""
        value = self._db.get_state(self._state)
        return dt.datetime.fromisoformat(value) if value else None

    def record(self) -> int:
        """Save the finished hours not saved yet (at most ``HOURS_PER_CALL``); returns
        how many files were written. A broker error stops this call; the next call
        retries the same hour."""
        now = ensure_utc(self._clock.now())
        latest = (now - SETTLE).replace(minute=0, second=0, microsecond=0)  # first unfinished
        hour = self.saved_until or (latest - BACKFILL)
        written = 0
        for _ in range(HOURS_PER_CALL):
            if hour >= latest:
                break
            try:
                ticks = self._broker.get_ticks(self._symbol, hour, hour + HOUR)
            except BrokerError as exc:
                log.warning("ticks not recorded: will retry", hour=hour.isoformat(), error=str(exc))
                break
            if not ticks.empty:
                save_hour(self.folder, hour, ticks)
                written += 1
            hour += HOUR
            self._db.set_state(self._state, hour.isoformat())
        if written:
            log.info("ticks recorded", hours=written, until=hour.isoformat())
        self._prune(now)
        return written

    def _prune(self, now: dt.datetime) -> None:
        if self._keep_days <= 0 or not self.folder.exists():
            return
        oldest = (now - dt.timedelta(days=self._keep_days)).date()
        for day in self.folder.iterdir():
            try:
                date = dt.date.fromisoformat(day.name)
            except ValueError:
                continue
            if date < oldest:
                shutil.rmtree(day, ignore_errors=True)

"""``python main.py --watchdog``: a Telegram alert when the bot has gone quiet (spec 9.8).

Task Scheduler runs this every 15 minutes, as a program separate from the bot.
It reads the heartbeat the bot writes to SQLite every 10 seconds. While the gold
market is open, a heartbeat older than ``alerts.watchdog_minutes`` sends one
alert; the first fresh heartbeat afterwards sends an all-clear. It cannot help
when the whole PC is off or asleep, because then it does not run either: that
is what an always-on Windows VPS is for (docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import datetime as dt
import sys
from typing import TextIO
from zoneinfo import ZoneInfo

from goldsweep.alerts.telegram import Notifier, make_notifier
from goldsweep.core.clock import Clock, SystemClock, ensure_utc
from goldsweep.core.settings import Settings
from goldsweep.live import HEARTBEAT_KEY
from goldsweep.storage.db import Database

ALERTED_KEY = "watchdog.alerted"
NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE = dt.time(18, 0), dt.time(17, 0)  # New York time


def gold_market_open(now: dt.datetime) -> bool:
    """Spot gold trades Sunday 18:00 to Friday 17:00 New York time, paused 17:00-18:00 daily."""
    local = ensure_utc(now).astimezone(NEW_YORK)
    day, moment = local.weekday(), local.time()  # Monday is 0
    if day == 5:
        return False
    if day == 6:
        return moment >= OPEN
    if day == 4 and moment >= CLOSE:
        return False
    return not CLOSE <= moment < OPEN


def check(db: Database, now: dt.datetime, minutes: int) -> str | None:
    """The message to send now, if any; remembers that an alert went out."""
    instant = ensure_utc(now)
    beat = db.get_state(HEARTBEAT_KEY)
    seen = dt.datetime.fromisoformat(beat["time"]) if isinstance(beat, dict) else None
    stale = seen is None or instant - seen > dt.timedelta(minutes=minutes)
    alerted = db.get_state(ALERTED_KEY)
    if stale:
        if alerted or not gold_market_open(instant):
            return None
        db.set_state(ALERTED_KEY, instant.isoformat())
        if seen is None:
            return "⚠️ GoldSweep has never reported in. Is it started?"
        silent = int((instant - seen).total_seconds() // 60)
        return (
            f"⚠️ GoldSweep has not been seen for {silent} min (last {seen:%a %H:%M} UTC): it is "
            "not trading or managing trades. Open positions keep their stop-loss and take-profit "
            "at the broker. Check the PC, MetaTrader 5 and the GoldSweep window."
        )
    if alerted:
        db.set_state(ALERTED_KEY, None)
        age = int((instant - seen).total_seconds()) if seen else 0
        return f"✅ GoldSweep is running again (seen {age} s ago)."
    return None


def run_watchdog(
    settings: Settings,
    *,
    clock: Clock | None = None,
    notifier: Notifier | None = None,
    out: TextIO | None = None,
) -> int:
    """One check; sends at most one message. Returns 0."""
    stream = out or sys.stdout
    now = ensure_utc((clock or SystemClock()).now())
    with Database(settings.db_path) as db:
        text = check(db, now, settings.config.alerts.watchdog_minutes)
        if text:
            sender = notifier or make_notifier(settings, db, stream=stream)[0]
            sender.send(text)
        else:
            print(f"{now:%Y-%m-%d %H:%M} UTC: nothing to report", file=stream)
    return 0

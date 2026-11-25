"""Following a main-strategy signal the owner trades by hand (S11, D118).

With ``execution.signals_only`` the bot places nothing: each main setup is sent as a
manual trade with its retest entry (the gap middle, D82), and this watcher follows it on
the closed M1 bars, so Telegram can say when to act:

* **entry**: price came back to the entry zone (the retest) before the setup expired;
* **tp1**: TP1 was reached after the entry: take part profit and move the stop to
  breakeven;
* **stopped**: the stop was hit after the entry;
* **expired**: no retest before the entry's deadline: do not enter;
* **missed**: price reached TP1 without the retest: do not chase;
* **failed**: price crossed the stop before the retest.

The rules are the ones the simulator applies to a waiting limit entry: when one bar
touches both the entry and the stop, both happen (the worse case); the stop is checked
before a target inside one bar. Pure: a watch and the bars give the next watch.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ensure_utc
from goldsweep.scalping.grade_filter import PathBar

WAITING, ENTERED, DONE = "waiting", "entered", "done"
#: An entered setup is followed for at most this long (then it is left to the owner).
FOLLOW_FOR = dt.timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class SignalWatch:
    """One manual main-strategy setup being followed."""

    id: str
    direction: Direction
    grade: str
    entry: float
    stop: float
    take_profit_1: float
    take_profit_2: float | None
    take_profit_3: float | None
    created: dt.datetime  # the signal's bar close: bars from here on count
    expires_at: dt.datetime  # the entry's deadline
    stage: str = WAITING
    entered_at: dt.datetime | None = None
    checked: dt.datetime | None = None  # the open time of the last bar read

    @property
    def long(self) -> bool:
        return self.direction is Direction.LONG

    def to_state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "direction": self.direction.value,
            "grade": self.grade,
            "entry": self.entry,
            "stop": self.stop,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "take_profit_3": self.take_profit_3,
            "created": self.created.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "stage": self.stage,
            "entered_at": self.entered_at.isoformat() if self.entered_at else None,
            "checked": self.checked.isoformat() if self.checked else None,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> SignalWatch:
        def moment(key: str) -> dt.datetime | None:
            value = data.get(key)
            return dt.datetime.fromisoformat(str(value)) if value else None

        created, expires = moment("created"), moment("expires_at")
        assert created is not None and expires is not None
        return cls(
            id=str(data["id"]),
            direction=Direction(data["direction"]),
            grade=str(data.get("grade", "")),
            entry=float(data["entry"]),
            stop=float(data["stop"]),
            take_profit_1=float(data["take_profit_1"]),
            take_profit_2=None
            if data.get("take_profit_2") is None
            else float(data["take_profit_2"]),
            take_profit_3=None
            if data.get("take_profit_3") is None
            else float(data["take_profit_3"]),
            created=created,
            expires_at=expires,
            stage=str(data.get("stage", WAITING)),
            entered_at=moment("entered_at"),
            checked=moment("checked"),
        )


def advance(watch: SignalWatch, bars: Iterable[PathBar]) -> tuple[SignalWatch, list[str]]:
    """Read the bars after ``watch.checked``; return the new watch and what happened, in
    order (``entry``, ``tp1``, ``stopped``, ``expired``, ``missed``, ``failed``)."""
    events: list[str] = []
    long = watch.long
    for bar in bars:
        start = ensure_utc(bar.time)
        if watch.stage == DONE:
            break
        if start < watch.created or (watch.checked is not None and start <= watch.checked):
            continue
        watch = replace(watch, checked=start)
        if watch.stage == WAITING:
            if start >= watch.expires_at:
                watch = replace(watch, stage=DONE)
                events.append("expired")
                break
            touched = bar.low <= watch.entry if long else bar.high >= watch.entry
            through_stop = bar.low <= watch.stop if long else bar.high >= watch.stop
            if not touched:
                if through_stop:
                    watch = replace(watch, stage=DONE)
                    events.append("failed")
                    break
                ran = bar.high >= watch.take_profit_1 if long else bar.low <= watch.take_profit_1
                if ran:
                    watch = replace(watch, stage=DONE)
                    events.append("missed")
                    break
                continue
            watch = replace(watch, stage=ENTERED, entered_at=start)
            events.append("entry")
            if through_stop:  # the same bar went on through the stop (the worse case)
                watch = replace(watch, stage=DONE)
                events.append("stopped")
                break
            continue
        # entered: the stop first inside one bar, then TP1
        if watch.entered_at is not None and start - watch.entered_at > FOLLOW_FOR:
            watch = replace(watch, stage=DONE)
            break
        if bar.low <= watch.stop if long else bar.high >= watch.stop:
            watch = replace(watch, stage=DONE)
            events.append("stopped")
            break
        if bar.high >= watch.take_profit_1 if long else bar.low <= watch.take_profit_1:
            watch = replace(watch, stage=DONE)
            events.append("tp1")
            break
    return watch, events

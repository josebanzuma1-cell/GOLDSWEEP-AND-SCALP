"""Scalps only with the main strategy's graded setups, and only while they last
(S9, D115; every mode since S10, D116: HFT A or A+, intraday and swing A+, A or B).

An A or A+ signal of the main strategy allows HFT scalps in its direction while the
setup is still **live**:

* it is younger than ``scalper.hft.a_window_min`` minutes;
* price has not reached its last target since it formed (the move is done);
* price has not run back through the sweep's extreme (or its stop): the setup failed;
* its trade, if it traded, has not closed.

A setup forming now that can still become its session's A+ ("A+ possible", S8) allows
its direction while it is forming; when it disappears without becoming a signal, the
permission goes with it. Intraday and swing scalping follow setups graded A+, A or B
over longer windows (S10). A failed setup closes nothing; since S10 the runner closes a
scalp whose setup reaches its last target (:func:`target_reached`, the move is done).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from goldsweep.analysis.structure import Direction

#: ``scalper.hft.grade_filter`` and the grades each one accepts (as stored: "A+ (...)").
GRADE_PREFIXES = {"a": ("A+", "A"), "a_plus": ("A+",), "b": ("A+", "A", "B")}
GRADE_NAMES = {"a": "A or A+", "a_plus": "A+", "b": "A+, A or B"}


@dataclass(frozen=True, slots=True)
class GradedSetup:
    """An A or A+ setup of the main strategy that HFT may follow."""

    id: str
    time: dt.datetime  # the signal's bar close; for a forming setup, its first bar
    direction: Direction
    grade: str  # "A+", "A" or "A+ possible"
    target: float | None = None  # its last target: reached means the move is done
    invalid: float | None = None  # the sweep's extreme (or its stop): crossed, it failed
    closed: bool = False  # it traded and the trade has closed
    forming: bool = False

    @property
    def label(self) -> str:
        side = "BUY" if self.direction is Direction.LONG else "SELL"
        return f"{self.grade} {side} {self.time:%H:%M} UTC"

    def to_state(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time.isoformat(),
            "direction": self.direction.value,
            "grade": self.grade,
            "target": self.target,
            "invalid": self.invalid,
            "closed": self.closed,
            "forming": self.forming,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> GradedSetup:
        return cls(
            id=str(data["id"]),
            time=dt.datetime.fromisoformat(str(data["time"])),
            direction=Direction(data["direction"]),
            grade=str(data["grade"]),
            target=None if data.get("target") is None else float(data["target"]),
            invalid=None if data.get("invalid") is None else float(data["invalid"]),
            closed=bool(data.get("closed", False)),
            forming=bool(data.get("forming", False)),
        )


@dataclass(frozen=True, slots=True)
class PathBar:
    """One closed M1 bar of the price path: its open time, high and low."""

    time: dt.datetime
    high: float
    low: float


def accepts(grade: str | None, grade_filter: str) -> bool:
    """Does a stored grade ("A+ (London playbook)", "A", "B") pass the filter?"""
    if not grade or grade_filter not in GRADE_PREFIXES:
        return False
    return grade.startswith(GRADE_PREFIXES[grade_filter])


def ended(
    setup: GradedSetup, path: Iterable[PathBar], now: dt.datetime, window: dt.timedelta
) -> str | None:
    """Why ``setup`` no longer allows HFT, or None while it is live.

    Bars are read from the signal's close on; when one bar touches both the failure
    level and the target, the failure is assumed (the worse case).
    """
    if setup.forming:
        return None
    if now - setup.time > window:
        return f"is over {window.total_seconds() / 60:.0f} minutes old"
    if setup.closed:
        return "has closed its trade"
    long = setup.direction is Direction.LONG
    for bar in path:
        if bar.time < setup.time:
            continue
        if setup.invalid is not None and (
            bar.low <= setup.invalid if long else bar.high >= setup.invalid
        ):
            return "ran back through its sweep"
        if setup.target is not None and (
            bar.high >= setup.target if long else bar.low <= setup.target
        ):
            return "reached its target"
    return None


def hft_permission(
    direction: Direction,
    setups: Sequence[GradedSetup],
    path: Sequence[PathBar],
    now: dt.datetime,
    window: dt.timedelta,
    grades: str = "A or A+",
    who: str = "HFT",
) -> str | None:
    """None when a live setup allows ``direction``; otherwise why ``who`` (a mode's
    label) may not go that way."""
    live: list[GradedSetup] = []
    done: list[tuple[GradedSetup, str]] = []
    for setup in setups:
        why = ended(setup, path, now, window)
        if why is None:
            live.append(setup)
        else:
            done.append((setup, why))
    if any(s.direction is direction for s in live):
        return None
    side = "buy" if direction is Direction.LONG else "sell"
    mine = [(s, why) for s, why in done if s.direction is direction]
    if mine:
        setup, why = max(mine, key=lambda item: item[0].time)
        return f"{who} follows {grades} setups: the {setup.label} setup {why}"
    if live:
        latest = max(live, key=lambda s: s.time)
        return f"{who} follows {grades} setups: the live one is {latest.label}, not a {side}"
    minutes = window.total_seconds() / 60
    return f"{who} follows {grades} setups: none to the {side} side in the last {minutes:.0f} min"


def followed(
    direction: Direction,
    setups: Sequence[GradedSetup],
    path: Sequence[PathBar],
    now: dt.datetime,
    window: dt.timedelta,
) -> GradedSetup | None:
    """The newest live setup in ``direction``: the one a new scalp follows (S10)."""
    live = [s for s in setups if s.direction is direction and ended(s, path, now, window) is None]
    return max(live, key=lambda s: s.time) if live else None


def backing_grade(
    direction: Direction,
    setups: Sequence[GradedSetup],
    path: Sequence[PathBar],
    now: dt.datetime,
    window: dt.timedelta,
) -> tuple[str, GradedSetup | None]:
    """The best live main setup in ``direction`` (S11): its grade ("A+", "A", "B";
    "A+ possible" counts as A+) and the setup, or ("none", None)."""
    rank = {"A+": 0, "A+ possible": 0, "A": 1, "B": 2}
    live = [
        s
        for s in setups
        if s.direction is direction and s.grade in rank and ended(s, path, now, window) is None
    ]
    if not live:
        return "none", None
    best = min(live, key=lambda s: (rank[s.grade], -s.time.timestamp()))
    return ("A+" if rank[best.grade] == 0 else best.grade), best


def target_reached(setup: GradedSetup, path: Iterable[PathBar]) -> bool:
    """Has price reached the setup's last target since it formed, before failing (S10)?"""
    if setup.target is None or setup.forming:
        return False
    long = setup.direction is Direction.LONG
    for bar in path:
        if bar.time < setup.time:
            continue
        if setup.invalid is not None and (
            bar.low <= setup.invalid if long else bar.high >= setup.invalid
        ):
            return False
        if bar.high >= setup.target if long else bar.low <= setup.target:
            return True
    return False


def allowed_directions(
    setups: Sequence[GradedSetup],
    path: Sequence[PathBar],
    now: dt.datetime,
    window: dt.timedelta,
) -> set[Direction]:
    """The directions HFT may take now (for status lines)."""
    return {s.direction for s in setups if ended(s, path, now, window) is None}

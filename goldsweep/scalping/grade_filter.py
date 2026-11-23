"""HFT only with the main strategy's A or A+ setups, and only while they last (S9, D115).

An A or A+ signal of the main strategy allows HFT scalps in its direction while the
setup is still **live**:

* it is younger than ``scalper.hft.a_window_min`` minutes;
* price has not reached its last target since it formed (the move is done);
* price has not run back through the sweep's extreme (or its stop): the setup failed;
* its trade, if it traded, has not closed.

A setup forming now that can still become its session's A+ ("A+ possible", S8) allows
its direction while it is forming; when it disappears without becoming a signal, the
permission goes with it. Nothing here closes a scalp: an open HFT scalp finishes under
its own stop-loss, targets and time stop.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from goldsweep.analysis.structure import Direction

#: ``scalper.hft.grade_filter`` and the grades each one accepts (as stored: "A+ (...)").
GRADE_PREFIXES = {"a": ("A+", "A"), "a_plus": ("A+",)}
GRADE_NAMES = {"a": "A or A+", "a_plus": "A+"}


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
) -> str | None:
    """None when a live setup allows ``direction``; otherwise why HFT may not go that way."""
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
        return f"HFT follows {grades} setups: the {setup.label} setup {why}"
    if live:
        latest = max(live, key=lambda s: s.time)
        return f"HFT follows {grades} setups: the live one is {latest.label}, not a {side}"
    minutes = window.total_seconds() / 60
    return f"HFT follows {grades} setups: none to the {side} side in the last {minutes:.0f} min"


def allowed_directions(
    setups: Sequence[GradedSetup],
    path: Sequence[PathBar],
    now: dt.datetime,
    window: dt.timedelta,
) -> set[Direction]:
    """The directions HFT may take now (for status lines)."""
    return {s.direction for s in setups if ended(s, path, now, window) is None}

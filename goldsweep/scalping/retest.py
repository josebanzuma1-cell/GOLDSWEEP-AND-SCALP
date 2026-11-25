"""Scalps enter on a retest (S11, D118): price comes back to the level it broke first.

The owner's observation: after a setup the market usually comes back to retest the
price it just left (the swept level, the range edge, the VWAP, the EMA) before it runs.
A scalp therefore no longer enters on its signal bar. It names its retest level, and the
closed M1 bars after it are watched:

* **confirmed**: a bar comes back to the level (within ``tolerance``) and closes back
  the trade's way; the entry is that close;
* **missed**: price reaches the setup's TP1 without the retest (it ran without us), or
  the window ends;
* **failed**: price crosses the setup's stop anchor first.

A setup without a level of its own (or one on the wrong side) retests the point
``pullback_r`` of the way back from its signal close towards its stop anchor. Pure: used
by the runner and :mod:`~goldsweep.scalping.mode_backtest` alike.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, is_dataclass, replace

from goldsweep.analysis.structure import Direction
from goldsweep.scalping.logic import Candidate

CONFIRMED, MISSED, FAILED = "confirmed", "missed", "failed"


def retest_level(candidate: Candidate, pullback_r: float) -> float:
    """The price ``candidate`` waits to see again: its own level when it lies between its
    stop anchor and its signal close, else ``pullback_r`` of the way back."""
    sign = candidate.direction.sign
    level = candidate.retest_level
    span = (candidate.reference - candidate.anchor) * sign
    if level is not None and math.isfinite(level):
        inside = 0 <= (candidate.reference - level) * sign < span
        if inside:
            return level
    return candidate.reference - sign * pullback_r * span


@dataclass(frozen=True, slots=True)
class RetestWatch:
    """A setup waiting for its retest."""

    candidate: Candidate
    level: float
    target: float  # the setup's TP1 from its signal close: reached first, it ran away
    tolerance: float  # in price
    deadline: dt.datetime

    @property
    def long(self) -> bool:
        return self.candidate.direction is Direction.LONG


def check(
    watch: RetestWatch, opened: dt.datetime, high: float, low: float, close: float
) -> str | None:
    """What one closed M1 bar (opened at ``opened``) did to the watch; None: still waiting."""
    if opened + dt.timedelta(minutes=1) > watch.deadline:
        return MISSED
    anchor = watch.candidate.anchor
    if watch.long:
        if low <= anchor:
            return FAILED
        if low <= watch.level + watch.tolerance:
            return CONFIRMED if close > watch.level else None
        return MISSED if high >= watch.target else None
    if high >= anchor:
        return FAILED
    if high >= watch.level - watch.tolerance:
        return CONFIRMED if close < watch.level else None
    return MISSED if low <= watch.target else None


def confirmed(candidate: Candidate, close: float, when: dt.datetime) -> Candidate:
    """The candidate as entered on its retest: the confirming close at that bar's close.

    The mode's own engine setup moves with it, so the plan (stop rules, ladder, cost gate)
    is made from the retest price."""
    source = candidate.source
    if is_dataclass(source) and hasattr(source, "reference") and hasattr(source, "time"):
        source = replace(source, reference=close, time=when)  # type: ignore[type-var]
    return replace(candidate, reference=close, time=when, source=source, retested=True)

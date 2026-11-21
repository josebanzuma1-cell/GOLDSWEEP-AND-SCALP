"""The scalper's modes and their state (D103).

A mode is **off** (not selected), **paper** (selected, traded on a shadow book only,
never sent to the broker) or **trading** (its evidence test passed and the owner
approved it). A mode that is not built yet does nothing whichever way it is switched.

Each later phase flips its mode's ``built`` and, if its backtest passes, ``passed``.
"""

from __future__ import annotations

from dataclasses import dataclass

OFF, PAPER, TRADING, NOT_BUILT = "off", "paper", "trading", "not built"


@dataclass(frozen=True, slots=True)
class ModeInfo:
    name: str
    label: str
    phase: str  # the phase that builds and tests it
    built: bool = False
    passed: bool = False  # its evidence test passed (D103)
    #: Why a mode does not run, once its phase has judged it (e.g. a failed backtest).
    note: str = ""


REGISTRY: dict[str, ModeInfo] = {
    "tick": ModeInfo("tick", "tick (seconds to two minutes)", "S5"),
    "intraday": ModeInfo(
        "intraday",
        "intraday (flat by the New York close)",
        "S3",
        note="its three rules failed their evidence test in S3 (D109)",
    ),
    "swing": ModeInfo(
        "swing",
        "swing (days to weeks)",
        "S4",
        note=(
            "its three rules failed their evidence test in S4 (D110), and its minimum lot "
            "needs a balance of about 76,000 at 0.25% a trade"
        ),
    ),
}
MODE_NAMES = tuple(REGISTRY)


def mode_state(name: str, *, selected: str, approved: bool) -> str:
    """off / paper / trading / not built, for the mode ``name``."""
    info = REGISTRY[name]
    if selected != name:
        return OFF
    if not info.built:
        return NOT_BUILT
    return TRADING if (info.passed and approved) else PAPER


def describe(name: str, *, selected: str, approved: bool) -> str:
    """One line for /status."""
    info = REGISTRY[name]
    state = mode_state(name, selected=selected, approved=approved)
    if state == NOT_BUILT:
        if info.note:
            return f"{info.label}: selected, but {info.note}: nothing runs"
        return f"{info.label}: selected, not built yet (phase {info.phase}): nothing runs"
    return f"{info.label}: {state}"

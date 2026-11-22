"""The scalper's modes and their state (D103, D111).

A mode is **off** (not selected), **paper** (selected: traded on the scalper's paper
book on the live feed, never sent to the broker) or **demo** (the owner approved it
with ``/approve scalper <mode>``: its orders go to the demo account). Real accounts stay
locked (D93). A mode that is not built yet does nothing whichever way it is switched.

The owner tests every built mode on paper and on the demo account (D111): its
backtest is reported, not a gate. Each phase flips its mode's ``built``.
"""

from __future__ import annotations

from dataclasses import dataclass

OFF, PAPER, DEMO, NOT_BUILT = "off", "paper", "demo", "not built"


@dataclass(frozen=True, slots=True)
class ModeInfo:
    name: str
    label: str
    phase: str  # the phase that builds it
    built: bool = False


REGISTRY: dict[str, ModeInfo] = {
    "hft": ModeInfo("hft", "HFT (M1, seconds to 15 minutes)", "S5", built=True),
    "intraday": ModeInfo("intraday", "intraday (M5, flat by the New York close)", "S6", built=True),
    "swing": ModeInfo("swing", "swing scalping (M15, hours to two days)", "S6", built=True),
}
MODE_NAMES = tuple(REGISTRY)


def mode_state(name: str, *, selected: str, approved: bool) -> str:
    """off / paper / demo / not built, for the mode ``name``."""
    info = REGISTRY[name]
    if selected != name:
        return OFF
    if not info.built:
        return NOT_BUILT
    return DEMO if approved else PAPER


def describe(name: str, *, selected: str, approved: bool) -> str:
    """One line for /status."""
    info = REGISTRY[name]
    state = mode_state(name, selected=selected, approved=approved)
    if state == NOT_BUILT:
        return f"{info.label}: selected, not built yet (phase {info.phase}): nothing runs"
    if state == DEMO:
        return f"{info.label}: on the demo account"
    if state == PAPER:
        return f"{info.label}: on paper"
    return f"{info.label}: off"

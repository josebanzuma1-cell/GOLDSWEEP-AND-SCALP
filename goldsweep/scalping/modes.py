"""The scalper's modes and their state (D103, D111, D115).

A mode is **off** (not selected), **paper** (selected: traded on the scalper's paper
book on the live feed, never sent to the broker) or **demo** (the owner approved it
with ``/approve scalper <mode>``: its orders go to the demo account). Real accounts stay
locked (D93). A mode that is not built yet does nothing whichever way it is switched.
One, two or all three modes may be selected at once (S9, D115): ``/scalper hft,swing``.

The owner tests every built mode on paper and on the demo account (D111): its
backtest is reported, not a gate. Each phase flips its mode's ``built``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from itertools import combinations

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


def ordered(modes: Iterable[str]) -> tuple[str, ...]:
    """Known modes in their fixed order (hft, intraday, swing), each once."""
    chosen = set(modes)
    return tuple(name for name in MODE_NAMES if name in chosen)


def parse_modes(text: str) -> tuple[str, ...] | None:
    """``off`` -> (); ``all`` -> every mode; ``hft,swing`` / ``hft swing`` / ``hft+swing``
    -> those modes. None when a word is not a mode."""
    words = text.strip().lower().replace(",", " ").replace("+", " ").split()
    if words == ["off"]:
        return ()
    if words == ["all"]:
        return MODE_NAMES
    if not words or any(word not in MODE_NAMES for word in words):
        return None
    return ordered(words)


def modes_key(modes: Iterable[str]) -> str:
    """The canonical text of a selection: ``off`` or ``hft,swing``."""
    names = ordered(modes)
    return ",".join(names) if names else "off"


def all_selections() -> tuple[str, ...]:
    """``off`` and every combination of modes, as :func:`modes_key` writes them."""
    keys = ["off"]
    for size in range(1, len(MODE_NAMES) + 1):
        keys += [",".join(combo) for combo in combinations(MODE_NAMES, size)]
    return tuple(keys)


def _is_selected(name: str, selected: str | Collection[str]) -> bool:
    if isinstance(selected, str):
        return selected == name or name in selected.split(",")
    return name in selected


def mode_state(name: str, *, selected: str | Collection[str], approved: bool) -> str:
    """off / paper / demo / not built, for the mode ``name`` (``selected``: one mode or
    several)."""
    info = REGISTRY[name]
    if not _is_selected(name, selected):
        return OFF
    if not info.built:
        return NOT_BUILT
    return DEMO if approved else PAPER


def describe(name: str, *, selected: str | Collection[str], approved: bool) -> str:
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

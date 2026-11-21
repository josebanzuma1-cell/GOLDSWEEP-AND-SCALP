"""Small shared enums used across the package."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum


class Mode(StrEnum):
    """Execution mode. ``PAPER`` never sends an order to the broker."""

    PAPER = "PAPER"
    LIVE = "LIVE"


class Timeframe(StrEnum):
    """Bar timeframes supported by the engine (names match MetaTrader 5)."""

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        """Length of one bar in minutes."""
        return _MINUTES[self.value]

    @property
    def duration(self) -> dt.timedelta:
        """Length of one bar."""
        return dt.timedelta(minutes=self.minutes)

    @property
    def pandas_freq(self) -> str:
        """Pandas offset alias for this timeframe, for example ``"5min"``."""
        return f"{self.minutes}min"


_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

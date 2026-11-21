"""Broker server time <-> UTC.

MetaTrader 5 timestamps (bar open times, tick times) are NOT true UTC. The
terminal reports the trade server's wall-clock time encoded as if it were UTC.
Most FX/CFD servers run on "New York close" time (New York + 7 hours, which is
UTC+2 in winter and UTC+3 in summer); others use plain UTC or a fixed offset.

Reading those stamps as UTC would shift every session window, the Asian range
and the news blackout by two or three hours. The MT5 client therefore passes
every timestamp through a :class:`ServerTimeRule` before it leaves the data
layer, and everything above the data layer sees true UTC only.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldsweep.core.clock import UTC, ensure_utc

#: Config value meaning "measure the server offset from a live tick".
AUTO = "auto"

_NEW_YORK = "America/New_York"
_ZERO = dt.timedelta(0)
_MAX_SHIFT = dt.timedelta(hours=14)

# Short names for the common "New York close" convention.
_ALIASES: dict[str, tuple[str, dt.timedelta]] = {
    "NY+7": (_NEW_YORK, dt.timedelta(hours=7)),
    "NYC+7": (_NEW_YORK, dt.timedelta(hours=7)),
    "NEWYORK+7": (_NEW_YORK, dt.timedelta(hours=7)),
    "NY_CLOSE": (_NEW_YORK, dt.timedelta(hours=7)),
}

_FIXED_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_ZONE_SHIFT_RE = re.compile(
    r"^(?P<zone>[A-Za-z][A-Za-z0-9_/-]*?)\s*"
    r"(?P<sign>[+-])\s*(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?$"
)

ServerTimeSource = Literal["config", "live_tick", "cache", "assumed", "mock"]


def _zone_exists(key: str) -> bool:
    try:
        ZoneInfo(key)
    except Exception:  # ZoneInfoNotFoundError, ValueError or OSError for odd keys
        return False
    return True


def _signed(sign: str, hours: str, minutes: str | None) -> dt.timedelta:
    mins = int(minutes) if minutes else 0
    if mins >= 60:
        raise ValueError("offset minutes must be below 60")
    delta = dt.timedelta(hours=int(hours), minutes=mins)
    return -delta if sign == "-" else delta


def format_offset(offset: dt.timedelta) -> str:
    """Render a UTC offset as ``+HH:MM`` / ``-HH:MM``."""
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, remainder = divmod(abs(total), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


@dataclass(frozen=True, slots=True)
class ServerTimeRule:
    """How a broker's server wall clock relates to UTC.

    ``server wall clock = wall clock in `zone` + shift``

    Examples: ``ServerTimeRule()`` is plain UTC, ``ServerTimeRule("UTC",
    timedelta(hours=2))`` is a fixed UTC+2 server, ``ServerTimeRule(
    "Europe/Athens")`` follows EU daylight saving, and ``ServerTimeRule(
    "America/New_York", timedelta(hours=7))`` is the common "New York close"
    server.
    """

    zone: str = "UTC"
    shift: dt.timedelta = _ZERO

    def __post_init__(self) -> None:
        if not _zone_exists(self.zone):
            raise ValueError(f"unknown IANA time zone {self.zone!r}")
        if abs(self.shift) > _MAX_SHIFT:
            raise ValueError("server time shift must be within 14 hours")

    # -- construction ------------------------------------------------------ #

    @classmethod
    def parse(cls, spec: str) -> ServerTimeRule:
        """Build a rule from config text.

        Accepted forms: ``"UTC"``, a fixed offset (``"+02:00"``, ``"UTC+2"``),
        an IANA zone (``"Europe/Athens"``), a zone plus a shift
        (``"America/New_York+7"``) or the alias ``"NY+7"``.

        Raises:
            ValueError: the text is not understood. ``"auto"`` is not a rule;
                callers handle it before parsing.
        """
        text = spec.strip()
        if not text:
            raise ValueError("server time zone is empty")
        alias = _ALIASES.get(text.upper().replace(" ", ""))
        if alias is not None:
            return cls(alias[0], alias[1])
        fixed = _FIXED_RE.match(text)
        if fixed is not None:
            return cls("UTC", _signed(fixed.group(1), fixed.group(2), fixed.group(3)))
        if _zone_exists(text):
            return cls(text)
        shifted = _ZONE_SHIFT_RE.match(text)
        if shifted is not None and _zone_exists(shifted["zone"]):
            return cls(
                shifted["zone"],
                _signed(shifted["sign"], shifted["hours"], shifted["minutes"]),
            )
        raise ValueError(
            f"cannot understand server time zone {spec!r}: use 'auto', 'UTC', a fixed offset "
            "such as '+02:00', an IANA zone such as 'Europe/Athens', or 'NY+7'"
        )

    @property
    def spec(self) -> str:
        """Canonical text form, accepted back by :meth:`parse`."""
        if self.shift == _ZERO:
            return self.zone
        if self.zone == "UTC":
            return f"UTC{format_offset(self.shift)}"
        return f"{self.zone}{format_offset(self.shift)}"

    def describe(self) -> str:
        """Short human-readable name."""
        if self.zone == _NEW_YORK and self.shift == dt.timedelta(hours=7):
            return "NY+7 (New York close)"
        return self.spec

    # -- conversion -------------------------------------------------------- #

    def utc_offset(self, moment: dt.datetime) -> dt.timedelta:
        """Server time minus UTC at the true-UTC instant ``moment``."""
        local = ensure_utc(moment).astimezone(ZoneInfo(self.zone))
        return (local.utcoffset() or _ZERO) + self.shift

    def from_utc(self, moment: dt.datetime) -> dt.datetime:
        """True UTC instant -> naive server wall-clock time."""
        local = ensure_utc(moment).astimezone(ZoneInfo(self.zone))
        return local.replace(tzinfo=None) + self.shift

    def to_utc(self, server_time: dt.datetime) -> dt.datetime:
        """Naive server wall-clock time -> true UTC instant."""
        if server_time.tzinfo is not None:
            raise ValueError(
                "server time must be naive (it is a wall-clock reading, not an instant)"
            )
        wall = server_time - self.shift
        return wall.replace(tzinfo=ZoneInfo(self.zone)).astimezone(UTC)

    def index_to_utc(self, index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        """Vectorised :meth:`to_utc` for a naive ``DatetimeIndex`` of server times."""
        if index.tz is not None:
            raise ValueError("expected a naive index of server wall-clock times")
        wall = index - self.shift
        if self.zone == "UTC":
            return wall.tz_localize("UTC")
        # Daylight-saving switches fall on Sundays while the gold market is shut,
        # so these two options only make the conversion crash-proof.
        localized = wall.tz_localize(
            ZoneInfo(self.zone),
            ambiguous=np.zeros(len(wall), dtype=bool),
            nonexistent="shift_forward",
        )
        return localized.tz_convert("UTC")


#: Used until the real offset is known: by far the most common convention.
DEFAULT_ASSUMED_RULE = ServerTimeRule(_NEW_YORK, dt.timedelta(hours=7))


@dataclass(frozen=True, slots=True)
class ServerTimeState:
    """The rule a broker client is using right now, and how much to trust it."""

    rule: ServerTimeRule
    source: ServerTimeSource
    #: False while the rule is a guess. An unconfirmed client withholds the
    #: newest bar, and later phases refuse to trade on it.
    confirmed: bool
    observed_offset: dt.timedelta | None = None
    #: Server clock minus PC clock in seconds, as seen on the last live tick.
    clock_skew_seconds: float | None = None
    warning: str | None = None


def offset_from_tick(
    tick_server_epoch: float,
    now_utc: dt.datetime,
    *,
    tolerance_seconds: float = 300.0,
) -> tuple[dt.timedelta, float] | None:
    """Estimate the server's UTC offset from a tick that is known to be live.

    ``tick_server_epoch`` is the tick time exactly as MT5 reports it (server
    wall clock encoded as epoch seconds). The raw difference to the PC clock is
    snapped to the nearest half hour; the remainder is clock skew plus tick age.

    Returns ``(offset, skew_seconds)``, or ``None`` when the difference is not
    close to a plausible offset, which means the tick was not live after all
    or the PC clock is badly wrong.
    """
    raw = tick_server_epoch - ensure_utc(now_utc).timestamp()
    snapped = round(raw / 1800.0) * 1800
    skew = raw - snapped
    if abs(skew) > tolerance_seconds:
        return None
    if not -12 * 3600 <= snapped <= 14 * 3600:
        return None
    return dt.timedelta(seconds=snapped), skew


def rule_for_offset(offset: dt.timedelta, at: dt.datetime) -> ServerTimeRule:
    """Most likely rule for a server offset observed at the instant ``at``.

    If the offset matches "New York + 7 hours" right now, that rule is chosen
    because it also follows US daylight saving for the rest of the year.
    Anything else becomes a fixed offset. A broker that follows EU daylight
    saving should be pinned to ``Europe/Athens`` in config.yaml.
    """
    if DEFAULT_ASSUMED_RULE.utc_offset(at) == offset:
        return DEFAULT_ASSUMED_RULE
    return ServerTimeRule("UTC", offset)

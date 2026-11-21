"""UTC clock and session calculator.

Rules enforced here (spec section 4 and ground rule 4):

* Every instant GoldSweep handles is a timezone-aware ``datetime`` in UTC.
  Naive datetimes are rejected instead of being silently interpreted.
* Sessions are wall-clock windows in their own IANA time zone and are
  converted to UTC for each calendar day with :mod:`zoneinfo`. No UTC offset
  is hard-coded anywhere, so daylight-saving changes need no code or config
  changes.

This module decides only whether the *clock* allows a new trade. The news
blackout, the broker's own trade mode, holidays, spread and risk limits are
separate checks that later phases layer on top.
"""

from __future__ import annotations

import datetime as dt
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from goldsweep.core.settings import SessionsConfig, TimeWindow

UTC = dt.UTC

ASIA = "asia"
LONDON = "london"
NEW_YORK = "newyork"
OVERLAP = "overlap"
ROLLOVER = "rollover_block"

SESSION_LABELS: dict[str, str] = {
    ASIA: "Asia / Tokyo",
    LONDON: "London kill zone",
    NEW_YORK: "New York kill zone",
    OVERLAP: "London-NY overlap",
    ROLLOVER: "Rollover block",
}

#: When several tradeable windows are active at once, the first one listed names the session.
SESSION_PRIORITY: tuple[str, ...] = (LONDON, NEW_YORK, ASIA, OVERLAP)

_ONE_DAY = dt.timedelta(days=1)
_FRIDAY, _SATURDAY, _SUNDAY = 4, 5, 6


# --------------------------------------------------------------------------- #
# Instants
# --------------------------------------------------------------------------- #


def ensure_utc(moment: dt.datetime) -> dt.datetime:
    """Return ``moment`` converted to UTC.

    Raises:
        ValueError: if ``moment`` is naive. GoldSweep never guesses a time zone.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(
            "naive datetime is not allowed: pass a timezone-aware datetime "
            "(GoldSweep works in UTC internally)"
        )
    return moment.astimezone(UTC)


def utc_now() -> dt.datetime:
    """Current time as an aware UTC datetime."""
    return dt.datetime.now(UTC)


def local_to_utc(day: dt.date, wall_time: dt.time, zone: str | ZoneInfo) -> dt.datetime:
    """Convert a wall-clock time on a local calendar date to a UTC instant.

    Daylight-saving edge cases are resolved deterministically: a wall time that
    does not exist (spring-forward gap) is read with the offset in force just
    before the change, and an ambiguous wall time (autumn fold) means its first
    occurrence.
    """
    tz = zone if isinstance(zone, ZoneInfo) else ZoneInfo(zone)
    local = dt.datetime.combine(day, wall_time.replace(tzinfo=None), tzinfo=tz)
    return local.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """Source of the current time. Injected everywhere so tests and backtests control time."""

    def now(self) -> dt.datetime:
        """Current time as an aware UTC datetime."""
        ...


class SystemClock:
    """The real wall clock."""

    def now(self) -> dt.datetime:
        return utc_now()


class ManualClock:
    """A clock that only moves when told to. Used by tests, the mock broker and backtests."""

    def __init__(self, start: dt.datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> dt.datetime:
        return self._now

    def set(self, moment: dt.datetime) -> None:
        """Jump to ``moment`` (must be timezone-aware)."""
        self._now = ensure_utc(moment)

    def advance(self, delta: dt.timedelta | None = None, **kwargs: float) -> dt.datetime:
        """Move forward by ``delta`` or by ``timedelta(**kwargs)`` and return the new time."""
        step = delta if delta is not None else dt.timedelta(**kwargs)
        if step < dt.timedelta(0):
            raise ValueError("a clock cannot move backwards")
        self._now = self._now + step
        return self._now


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionSpan:
    """One concrete occurrence of a session window, in UTC."""

    name: str
    label: str
    start: dt.datetime  # UTC, inclusive
    end: dt.datetime  # UTC, exclusive
    trade: bool  # new trades may open inside this window
    blocks_trading: bool  # True for "avoid" windows such as the rollover block
    local_date: dt.date  # calendar date the window belongs to, in its own time zone
    definition: str  # how config.yaml defines it, for display

    def contains(self, moment: dt.datetime) -> bool:
        """True when ``moment`` falls inside the window (start inclusive, end exclusive)."""
        return self.start <= ensure_utc(moment) < self.end

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class TradingStatus:
    """Whether the clock allows a new trade at one instant, and why not."""

    moment: dt.datetime
    allowed: bool
    session: str | None  # tradeable session active at ``moment``, if any
    active: tuple[str, ...]  # every window active at ``moment``
    reasons: tuple[str, ...]  # why new trades are not allowed (empty when allowed)
    #: True for a hard stop (weekend, rollover block, Friday cutoff), as opposed to
    #: simply being outside the session windows.
    blocked: bool = False


def _describe_window(window: TimeWindow) -> str:
    return f"{window.start:%H:%M}-{window.end:%H:%M} {window.tz}"


@functools.lru_cache(maxsize=4096)
def _window_bounds(window: TimeWindow, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """UTC start and end of ``window`` on local calendar date ``day``."""
    start = local_to_utc(day, window.start, window.tz)
    end_day = day if window.end > window.start else day + _ONE_DAY  # crosses local midnight
    end = local_to_utc(end_day, window.end, window.tz)
    if end < start:  # only possible for a window squeezed into a DST change
        end = start
    return start, end


class SessionCalculator:
    """Turns the ``sessions`` block of config.yaml into concrete UTC windows."""

    def __init__(self, sessions: SessionsConfig) -> None:
        self._cfg = sessions

    # -- windows for one calendar date ------------------------------------ #

    def span(self, name: str, day: dt.date) -> SessionSpan:
        """The window ``name`` for local calendar date ``day``.

        Raises:
            KeyError: unknown session name.
            ValueError: ``overlap`` requested for a date on which London and
                New York do not overlap.
        """
        if name == OVERLAP:
            overlap = self.overlap(day)
            if overlap is None:
                raise ValueError(f"the London and New York sessions do not overlap on {day}")
            return overlap
        window: TimeWindow
        if name == ROLLOVER:
            window, trade, blocks = self._cfg.rollover_block, False, True
        elif name in (ASIA, LONDON, NEW_YORK):
            session = getattr(self._cfg, name)
            window, trade, blocks = session, bool(session.trade), False
        else:
            raise KeyError(f"unknown session {name!r}")
        start, end = _window_bounds(window, day)
        return SessionSpan(
            name=name,
            label=SESSION_LABELS[name],
            start=start,
            end=end,
            trade=trade,
            blocks_trading=blocks,
            local_date=day,
            definition=_describe_window(window),
        )

    def overlap(self, day: dt.date) -> SessionSpan | None:
        """Hours on ``day`` when both the full London and full New York sessions are open."""
        cfg = self._cfg.overlap
        london_start, london_end = _window_bounds(cfg.london, day)
        ny_start, ny_end = _window_bounds(cfg.newyork, day)
        start, end = max(london_start, ny_start), min(london_end, ny_end)
        if end <= start:
            return None
        return SessionSpan(
            name=OVERLAP,
            label=SESSION_LABELS[OVERLAP],
            start=start,
            end=end,
            trade=bool(cfg.trade),
            blocks_trading=False,
            local_date=day,
            definition=f"{_describe_window(cfg.london)} + {_describe_window(cfg.newyork)}",
        )

    def spans_for_date(self, day: dt.date) -> list[SessionSpan]:
        """Every window belonging to calendar date ``day``, earliest first."""
        spans = [self.span(name, day) for name in (ASIA, LONDON, NEW_YORK)]
        overlap = self.overlap(day)
        if overlap is not None:
            spans.append(overlap)
        spans.append(self.span(ROLLOVER, day))
        return sorted(spans, key=lambda s: s.start)  # stable: ties keep the order above

    # -- what is happening at one instant ---------------------------------- #

    def active_spans(self, moment: dt.datetime) -> list[SessionSpan]:
        """Every window that contains ``moment``."""
        instant = ensure_utc(moment)
        found: list[SessionSpan] = []
        for name in (ASIA, LONDON, NEW_YORK, ROLLOVER):
            window: TimeWindow = (
                self._cfg.rollover_block if name == ROLLOVER else getattr(self._cfg, name)
            )
            local_day = instant.astimezone(ZoneInfo(window.tz)).date()
            # A window that crosses local midnight started on the previous local date.
            for day in (local_day - _ONE_DAY, local_day):
                span = self.span(name, day)
                if span.contains(instant):
                    found.append(span)
                    break
        overlap_days = {
            instant.astimezone(ZoneInfo(part.tz)).date() - back
            for part in (self._cfg.overlap.london, self._cfg.overlap.newyork)
            for back in (_ONE_DAY, dt.timedelta(0))
        }
        for day in sorted(overlap_days):
            overlap = self.overlap(day)
            if overlap is not None and overlap.contains(instant):
                found.append(overlap)
                break
        return found

    def after_friday_cutoff(self, moment: dt.datetime) -> bool:
        """True on Fridays from the configured cutoff time onwards."""
        cutoff = self._cfg.friday_cutoff
        local = ensure_utc(moment).astimezone(ZoneInfo(cutoff.tz))
        return local.weekday() == _FRIDAY and local.time() >= cutoff.time

    def friday_cutoff_instant(self, day: dt.date) -> dt.datetime:
        """UTC instant of the cutoff time on calendar date ``day`` (whatever its weekday)."""
        cutoff = self._cfg.friday_cutoff
        return local_to_utc(day, cutoff.time, cutoff.tz)

    @property
    def friday_cutoff_definition(self) -> str:
        cutoff = self._cfg.friday_cutoff
        return f"from {cutoff.time:%H:%M} {cutoff.tz}"

    def is_weekend(self, moment: dt.datetime) -> bool:
        """True from the Friday close until the Sunday reopen."""
        weekend = self._cfg.weekend
        local = ensure_utc(moment).astimezone(ZoneInfo(weekend.tz))
        weekday = local.weekday()
        if weekday == _SATURDAY:
            return True
        if weekday == _FRIDAY:
            return local.time() >= weekend.close
        if weekday == _SUNDAY:
            return local.time() < weekend.open
        return False

    def next_market_open(self, moment: dt.datetime) -> dt.datetime | None:
        """UTC instant of the Sunday reopen when ``moment`` is in the weekend, else ``None``."""
        if not self.is_weekend(moment):
            return None
        weekend = self._cfg.weekend
        local = ensure_utc(moment).astimezone(ZoneInfo(weekend.tz))
        days_to_sunday = (_SUNDAY - local.weekday()) % 7
        sunday = local.date() + dt.timedelta(days=days_to_sunday)
        return local_to_utc(sunday, weekend.open, weekend.tz)

    def status(self, moment: dt.datetime) -> TradingStatus:
        """Does the clock allow a new trade at ``moment``?"""
        instant = ensure_utc(moment)
        spans = self.active_spans(instant)
        reasons: list[str] = []
        if self.is_weekend(instant):
            reasons.append("weekend: market closed")
        if any(span.blocks_trading for span in spans):
            reasons.append("rollover block: no new trades")
        if self.after_friday_cutoff(instant):
            reasons.append(f"Friday cutoff: no new trades {self.friday_cutoff_definition}")
        blocked = bool(reasons)
        tradeable = {span.name for span in spans if span.trade and not span.blocks_trading}
        session = next((name for name in SESSION_PRIORITY if name in tradeable), None)
        if session is None and not self._cfg.trade_all_hours:
            reasons.append("outside the tradeable session windows")
        return TradingStatus(
            moment=instant,
            allowed=not reasons,
            session=session,
            active=tuple(span.name for span in spans),
            reasons=tuple(reasons),
            blocked=blocked,
        )

    def next_tradeable_span(self, moment: dt.datetime, *, max_days: int = 8) -> SessionSpan | None:
        """The first tradeable window that opens after ``moment``.

        Returns ``None`` when nothing tradeable opens within ``max_days`` days.
        """
        instant = ensure_utc(moment)
        first_day = instant.date() - _ONE_DAY
        candidates: list[SessionSpan] = []
        for offset in range(max_days + 2):
            day = first_day + dt.timedelta(days=offset)
            candidates.extend(
                span
                for span in self.spans_for_date(day)
                if span.trade and not span.blocks_trading and span.start > instant
            )
        for span in sorted(candidates, key=lambda s: s.start):
            if self.status(span.start).allowed:
                return span
        return None

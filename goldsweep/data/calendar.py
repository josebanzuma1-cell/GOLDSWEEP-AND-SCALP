"""Economic calendar: providers, the SQLite cache, blackout and post-news windows.

Two sources, behind one :class:`CalendarProvider` interface (spec section 2):

* **FinanceCalendar.com** (primary): needs ``FINANCECALENDAR_API_KEY``, publishes
  actual figures after release, and requires a visible attribution link wherever
  its data is shown (:attr:`CalendarProvider.attribution`). The free plan allows
  10 requests a day.
* **Forex Factory weekly JSON feed** (fallback): no key, this week only, forecast
  and previous but no actual figures. Personal use only.

Events are cached in the ``calendar_events`` table. Each source refreshes on its
own interval (faster near high-impact releases when it publishes actual figures)
and stops for the day at its request cap. When both sources list the same
release, the one listed first in config wins.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from goldsweep.core.clock import Clock, SystemClock, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import CalendarSourceConfig, NewsConfig
from goldsweep.data.http import Fetcher, HttpError, http_get
from goldsweep.risk.risk_manager import HIGH_IMPACT, NewsEvent
from goldsweep.storage.db import Database

log = get_logger(__name__)

IMPACTS = ("high", "medium", "low", "holiday", "none")
_STATE_LAST = "calendar.{name}.last_fetch"
_STATE_COUNT = "calendar.{name}.requests.{day}"


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """One scheduled release."""

    source: str
    event_id: str
    time: dt.datetime  # UTC
    currency: str
    title: str
    impact: str  # one of IMPACTS
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None
    all_day: bool = False
    url: str | None = None

    @property
    def released(self) -> bool:
        return self.actual is not None

    def describe(self) -> str:
        figures = []
        if self.actual is not None:
            figures.append(f"actual {self.actual}")
        if self.forecast is not None:
            figures.append(f"forecast {self.forecast}")
        if self.previous is not None:
            figures.append(f"previous {self.previous}")
        when = "all day" if self.all_day else f"{self.time:%a %H:%M} UTC"
        text = f"{when} {self.currency} {self.title} ({self.impact})"
        return f"{text}: {', '.join(figures)}" if figures else text


def _text(value: Any) -> str | None:
    """A figure as text; empty strings and nulls mean "not published"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _impact(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in IMPACTS:
        return text
    if text in ("non-economic", "non_economic", ""):
        return "none"
    return "low"


def _parse_time(value: str) -> dt.datetime:
    moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"time without a UTC offset: {value!r}")
    return moment.astimezone(dt.UTC)


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class CalendarProvider(ABC):
    """A source of scheduled releases."""

    name: str = ""
    #: Text that must be shown wherever this source's data is displayed.
    attribution: str | None = None
    #: True when the source fills in actual figures after a release.
    publishes_actuals: bool = False

    @abstractmethod
    def fetch(self) -> list[EconomicEvent]:
        """The releases the source currently lists (its own window)."""


#: FinanceCalendar.com reports countries; currencies follow from them.
COUNTRY_CURRENCY = {
    "US": "USD",
    "EU": "EUR",
    "DE": "EUR",
    "GB": "GBP",
    "JP": "JPY",
    "CA": "CAD",
    "AU": "AUD",
    "CH": "CHF",
    "NZ": "NZD",
    "CN": "CNY",
    "NO": "NOK",
    "SE": "SEK",
    "IN": "INR",
    "HK": "HKD",
}
CURRENCY_COUNTRY = {
    "USD": "US",
    "EUR": "EU",
    "GBP": "GB",
    "JPY": "JP",
    "CAD": "CA",
    "AUD": "AU",
    "CHF": "CH",
    "NZD": "NZ",
    "CNY": "CN",
}


def parse_financecalendar(payload: Any) -> list[EconomicEvent]:
    """Events from a FinanceCalendar.com ``/calendar`` response (``{"events": [...]}``)."""
    if isinstance(payload, dict):
        items = payload.get("events", payload.get("data", []))
    else:
        items = payload
    events: list[EconomicEvent] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("status") == "cancelled":
            continue
        stamp = item.get("release_at_utc") or item.get("time_utc")
        all_day = bool(item.get("all_day"))
        try:
            if stamp:
                time = _parse_time(str(stamp))
            elif item.get("date"):
                time = dt.datetime.fromisoformat(str(item["date"])).replace(tzinfo=dt.UTC)
                all_day = True
            else:
                continue
        except ValueError:
            log.warning("calendar event with an unreadable time skipped", source="financecalendar")
            continue
        country = str(item.get("country") or "").upper()
        title = _text(item.get("title")) or _text(item.get("name")) or "untitled release"
        currency = COUNTRY_CURRENCY.get(country, country or "?")
        events.append(
            EconomicEvent(
                source="financecalendar",
                event_id=str(item.get("id") or _stable_id(currency, title, time.isoformat())),
                time=time,
                currency=currency,
                title=title,
                impact=_impact(item.get("impact")),
                forecast=_text(item.get("consensus")),
                previous=_text(item.get("prior")),
                actual=_text(item.get("actual")),
                all_day=all_day,
                url=_text(item.get("url")) or _text(item.get("source_url")),
            )
        )
    return events


class FinanceCalendarProvider(CalendarProvider):
    """FinanceCalendar.com (needs a key; the key goes in a header, never in the URL)."""

    name = "financecalendar"
    attribution = "Economic calendar data: FinanceCalendar.com (https://www.financecalendar.com)"
    publishes_actuals = True
    BASE_URL = "https://www.financecalendar.com/wp-json/fc/v1"

    def __init__(
        self, api_key: str, *, currencies: Sequence[str] = ("USD",), fetch: Fetcher = http_get
    ) -> None:
        self._key = api_key
        self._fetch = fetch
        countries = [CURRENCY_COUNTRY[c] for c in currencies if c in CURRENCY_COUNTRY]
        self._countries = ",".join(countries[:3] or ["US"])  # the free plan allows three

    def fetch(self) -> list[EconomicEvent]:
        query = urllib.parse.urlencode({"country": self._countries, "impact": "high,medium"})
        body = self._fetch(
            f"{self.BASE_URL}/calendar?{query}",
            {"X-API-Key": self._key, "Accept": "application/json"},
        )
        return parse_financecalendar(json.loads(body))


def parse_forexfactory(payload: Any) -> list[EconomicEvent]:
    """Events from the Forex Factory weekly JSON feed (a plain list)."""
    events: list[EconomicEvent] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or not item.get("date"):
            continue
        try:
            time = _parse_time(str(item["date"]))
        except ValueError:
            continue
        currency = str(item.get("country") or "?").upper()
        title = _text(item.get("title")) or "untitled release"
        impact = _impact(item.get("impact"))
        events.append(
            EconomicEvent(
                source="forexfactory",
                event_id=_stable_id(currency, title, time.isoformat()),
                time=time,
                currency=currency,
                title=title,
                impact=impact,
                forecast=_text(item.get("forecast")),
                previous=_text(item.get("previous")),
                all_day=impact == "holiday",
            )
        )
    return events


class ForexFactoryProvider(CalendarProvider):
    """The public Forex Factory weekly feed (this week only, no actual figures)."""

    name = "forexfactory"
    attribution = "Economic calendar data: Forex Factory weekly feed (personal use)"
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(self, *, fetch: Fetcher = http_get) -> None:
        self._fetch = fetch

    def fetch(self) -> list[EconomicEvent]:
        return parse_forexfactory(json.loads(self._fetch(self.URL, {"Accept": "application/json"})))


# --------------------------------------------------------------------------- #
# Which releases matter
# --------------------------------------------------------------------------- #


def _keyword_pattern(keywords: Sequence[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    # A keyword must start a word ("ISM" matches "ISM Manufacturing", not "tourism").
    return re.compile(
        "|".join(rf"(?<![A-Za-z]){re.escape(k)}" for k in keywords), flags=re.IGNORECASE
    )


def is_blackout_event(event: EconomicEvent, config: NewsConfig) -> bool:
    """High impact in a watched currency, or a watched release whatever its rating."""
    if event.currency not in config.blackout_currencies or event.all_day:
        return False
    if event.impact == HIGH_IMPACT:
        return True
    pattern = _keyword_pattern(config.blackout_keywords)
    return bool(pattern and pattern.search(event.title))


def blackout_events(events: Sequence[EconomicEvent], config: NewsConfig) -> list[NewsEvent]:
    """The releases that block new entries, in the risk manager's terms."""
    return [
        NewsEvent(event.time, f"{event.currency} {event.title}", HIGH_IMPACT)
        for event in events
        if is_blackout_event(event, config)
    ]


def post_news_windows(
    events: Sequence[EconomicEvent], config: NewsConfig
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Spec 5.6 post-news sweep mode: 15 to 90 minutes after each blackout release."""
    if not config.post_news_sweep_mode:
        return []
    start = dt.timedelta(minutes=config.post_news_start_min)
    end = dt.timedelta(minutes=config.post_news_end_min)
    return [
        (event.time + start, event.time + end)
        for event in events
        if is_blackout_event(event, config)
    ]


# --------------------------------------------------------------------------- #
# The cache and refresh schedule
# --------------------------------------------------------------------------- #


@dataclass
class RefreshReport:
    fetched: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _normalise(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


class CalendarService:
    """Refreshes the calendar sources on schedule and answers questions from the cache."""

    def __init__(
        self,
        providers: Sequence[tuple[CalendarProvider, CalendarSourceConfig]],
        db: Database,
        config: NewsConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._providers = list(providers)
        self._db = db
        self._config = config
        self._clock = clock or SystemClock()

    @property
    def providers(self) -> list[CalendarProvider]:
        return [provider for provider, _cfg in self._providers]

    def _near_high_impact(self, now: dt.datetime) -> bool:
        window = dt.timedelta(minutes=self._config.fast_window_min)
        nearby = self.events(now - window, now + window)
        return any(is_blackout_event(event, self._config) for event in nearby)

    def refresh(self, now: dt.datetime | None = None, *, force: bool = False) -> RefreshReport:
        """Fetch every source that is due (or all of them with ``force``)."""
        instant = ensure_utc(now or self._clock.now())
        day = f"{instant:%Y-%m-%d}"
        report = RefreshReport()
        fast = self._near_high_impact(instant)
        for provider, cfg in self._providers:
            name = provider.name
            last = self._db.get_state(_STATE_LAST.format(name=name))
            interval = cfg.refresh_min
            if fast and provider.publishes_actuals and cfg.fast_refresh_min:
                interval = cfg.fast_refresh_min
            if not force and last is not None and instant.timestamp() - last < interval * 60:
                report.skipped[name] = f"not due (every {interval} min)"
                continue
            count_key = _STATE_COUNT.format(name=name, day=day)
            used = int(self._db.get_state(count_key, 0))
            if cfg.daily_request_limit is not None and used >= cfg.daily_request_limit:
                report.skipped[name] = f"daily limit of {cfg.daily_request_limit} requests reached"
                continue
            self._db.set_state(count_key, used + 1)
            self._db.set_state(_STATE_LAST.format(name=name), instant.timestamp())
            try:
                events = provider.fetch()
            except (HttpError, ValueError, TypeError, KeyError) as exc:
                report.errors[name] = str(exc)
                log.warning("calendar source failed", source=name, error=str(exc))
                continue
            self._store(events, instant)
            report.fetched[name] = len(events)
        return report

    def _store(self, events: Sequence[EconomicEvent], now: dt.datetime) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO calendar_events (source, event_id, time, currency, title, "
            "impact, forecast, previous, actual, all_day, url, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.source,
                    e.event_id,
                    int(e.time.timestamp()),
                    e.currency,
                    e.title,
                    e.impact,
                    e.forecast,
                    e.previous,
                    e.actual,
                    int(e.all_day),
                    e.url,
                    int(now.timestamp()),
                )
                for e in events
            ],
        )

    def events(self, start: dt.datetime, end: dt.datetime) -> list[EconomicEvent]:
        """Cached releases in ``[start, end)``, one per release, earliest first."""
        rows = self._db.query(
            "SELECT source, event_id, time, currency, title, impact, forecast, previous, "
            "actual, all_day, url FROM calendar_events WHERE time >= ? AND time < ? "
            "ORDER BY time",
            (int(ensure_utc(start).timestamp()), int(ensure_utc(end).timestamp())),
        )
        rank = {provider.name: n for n, (provider, _cfg) in enumerate(self._providers)}
        best: dict[tuple[str, str, int], EconomicEvent] = {}
        for row in rows:
            event = EconomicEvent(
                source=row[0],
                event_id=row[1],
                time=dt.datetime.fromtimestamp(row[2], tz=dt.UTC),
                currency=row[3],
                title=row[4],
                impact=row[5],
                forecast=row[6],
                previous=row[7],
                actual=row[8],
                all_day=bool(row[9]),
                url=row[10],
            )
            key = (event.currency, _normalise(event.title), row[2] // 60)
            current = best.get(key)
            if current is None or rank.get(event.source, 99) < rank.get(current.source, 99):
                best[key] = event
        return sorted(best.values(), key=lambda e: (e.time, e.currency, e.title))

    def blackout_events(self, start: dt.datetime, end: dt.datetime) -> list[NewsEvent]:
        return blackout_events(self.events(start, end), self._config)

    def post_news_windows(
        self, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        return post_news_windows(self.events(start, end), self._config)

    def next_blackout_event(self, now: dt.datetime) -> EconomicEvent | None:
        instant = ensure_utc(now)
        upcoming = self.events(instant, instant + dt.timedelta(days=8))
        return next((e for e in upcoming if is_blackout_event(e, self._config)), None)

    def attributions(self, events: Sequence[EconomicEvent]) -> list[str]:
        """The credit lines to show with ``events`` (FinanceCalendar.com requires one)."""
        shown = {event.source for event in events}
        return [p.attribution for p in self.providers if p.name in shown and p.attribution]

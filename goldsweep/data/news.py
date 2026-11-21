"""Market headlines: Finnhub and RSS feeds, de-duplicated and stored.

Sources (spec section 2):

* **Finnhub** ``/news`` for each configured category (``forex``, ``general``),
  with ``FINNHUB_API_KEY`` sent in the ``X-Finnhub-Token`` header;
* **RSS / Atom feeds** listed in ``news.rss_feeds`` (Federal Reserve and ECB
  press releases, FXStreet news by default).

The same story often arrives from several places. A headline's identity is a
hash of its normalised title (lower case, punctuation removed), so it is stored
and classified once. RSS descriptions are reduced to plain text and capped at
:data:`SUMMARY_CHARS` characters to keep the classifier's prompt short (spec:
ideally under 2,000 tokens).
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import xml.etree.ElementTree as ElementTree
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from goldsweep.core.clock import Clock, SystemClock, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import NewsConfig
from goldsweep.data.http import Fetcher, HttpError, http_get
from goldsweep.storage.db import Database

log = get_logger(__name__)

SUMMARY_CHARS = 600
TITLE_CHARS = 300
_ATOM = "{http://www.w3.org/2005/Atom}"
_STATE_LAST = "news.{name}.last_poll"


@dataclass(frozen=True, slots=True)
class Headline:
    key: str
    source: str
    title: str
    published: dt.datetime  # UTC
    summary: str | None = None
    url: str | None = None


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def headline_key(title: str) -> str:
    """Identity of a story: the same words give the same key, wherever they came from."""
    return hashlib.sha256(normalise_title(title).encode()).hexdigest()[:24]


def plain_text(value: str | None, limit: int) -> str | None:
    """HTML reduced to plain text on one line, cut at ``limit`` characters."""
    if not value:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_headline(
    source: str, title: str, published: dt.datetime, summary: str | None, url: str | None
) -> Headline | None:
    clean = plain_text(title, TITLE_CHARS)
    if not clean:
        return None
    return Headline(
        key=headline_key(clean),
        source=source,
        title=clean,
        published=ensure_utc(published),
        summary=plain_text(summary, SUMMARY_CHARS),
        url=(url or "").strip() or None,
    )


def parse_date(text: str | None) -> dt.datetime | None:
    """RFC 822 (RSS) or ISO 8601 (Atom). A time without a zone is read as UTC."""
    if not text:
        return None
    value = text.strip()
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


class HeadlineSource(ABC):
    name: str = ""

    @abstractmethod
    def fetch(self) -> list[Headline]:
        """The source's latest headlines."""


def parse_feed(body: bytes, source: str) -> list[Headline]:
    """Items of an RSS 2.0 or Atom feed.

    The standard library parser does not fetch external entities, and the expat
    bundled with Python 3.13 refuses entity-expansion bombs.
    """
    root = ElementTree.fromstring(body)
    headlines: list[Headline] = []
    if root.tag == f"{_ATOM}feed":
        for entry in root.iter(f"{_ATOM}entry"):
            link = entry.find(f"{_ATOM}link")
            published = parse_date(
                entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
            )
            item = make_headline(
                source,
                entry.findtext(f"{_ATOM}title") or "",
                published or dt.datetime.now(dt.UTC),
                entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content"),
                link.get("href") if link is not None else None,
            )
            if item is not None and published is not None:
                headlines.append(item)
        return headlines
    for node in root.iter("item"):
        published = parse_date(node.findtext("pubDate"))
        if published is None:
            continue  # without a time the headline cannot be weighed
        item = make_headline(
            source,
            node.findtext("title") or "",
            published,
            node.findtext("description"),
            node.findtext("link"),
        )
        if item is not None:
            headlines.append(item)
    return headlines


class RssSource(HeadlineSource):
    def __init__(self, name: str, url: str, *, fetch: Fetcher = http_get) -> None:
        self.name = name
        self._url = url
        self._fetch = fetch

    def fetch(self) -> list[Headline]:
        body = self._fetch(self._url, {"Accept": "application/rss+xml, application/xml"})
        return parse_feed(body, self.name)


def parse_finnhub(payload: Any) -> list[Headline]:
    """Items of a Finnhub ``/news`` response: a list with ``datetime`` in epoch seconds."""
    headlines: list[Headline] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        stamp = item.get("datetime")
        if not isinstance(stamp, int | float) or stamp <= 0:
            continue
        publisher = str(item.get("source") or "").strip()
        headline = make_headline(
            f"Finnhub/{publisher}" if publisher else "Finnhub",
            str(item.get("headline") or ""),
            dt.datetime.fromtimestamp(stamp, tz=dt.UTC),
            item.get("summary"),
            item.get("url"),
        )
        if headline is not None:
            headlines.append(headline)
    return headlines


class FinnhubSource(HeadlineSource):
    URL = "https://finnhub.io/api/v1/news"

    def __init__(self, api_key: str, category: str, *, fetch: Fetcher = http_get) -> None:
        self.name = f"finnhub-{category}"
        self._key = api_key
        self._category = category
        self._fetch = fetch

    def fetch(self) -> list[Headline]:
        body = self._fetch(
            f"{self.URL}?category={self._category}",
            {"X-Finnhub-Token": self._key, "Accept": "application/json"},
        )
        return parse_finnhub(json.loads(body))


# --------------------------------------------------------------------------- #
# Polling and storage
# --------------------------------------------------------------------------- #


@dataclass
class PollReport:
    new: list[Headline] = field(default_factory=list)
    fetched: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class NewsService:
    """Polls the headline sources and keeps one row per story in SQLite."""

    def __init__(
        self,
        sources: Sequence[HeadlineSource],
        db: Database,
        config: NewsConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._sources = list(sources)
        self._db = db
        self._config = config
        self._clock = clock or SystemClock()

    @property
    def sources(self) -> list[HeadlineSource]:
        return list(self._sources)

    def poll(self, now: dt.datetime | None = None, *, force: bool = False) -> PollReport:
        """Fetch the sources that are due; returns the headlines not seen before."""
        instant = ensure_utc(now or self._clock.now())
        report = PollReport()
        for source in self._sources:
            state = _STATE_LAST.format(name=source.name)
            last = self._db.get_state(state)
            interval = self._config.headline_poll_min * 60
            if not force and last is not None and instant.timestamp() - last < interval:
                report.skipped[source.name] = "not due"
                continue
            self._db.set_state(state, instant.timestamp())
            try:
                items = source.fetch()
            except (HttpError, ValueError, TypeError, ElementTree.ParseError) as exc:
                report.errors[source.name] = str(exc)
                log.warning("headline source failed", source=source.name, error=str(exc))
                continue
            report.fetched[source.name] = len(items)
            report.new.extend(self._store(items, instant))
        report.new.sort(key=lambda h: h.published)
        return report

    def _store(self, items: Sequence[Headline], now: dt.datetime) -> list[Headline]:
        unique = {item.key: item for item in items}
        if not unique:
            return []
        marks = ",".join("?" * len(unique))
        known = {
            row[0]
            for row in self._db.query(
                f"SELECT key FROM headlines WHERE key IN ({marks})", list(unique)
            )
        }
        fresh = [item for key, item in unique.items() if key not in known]
        self._db.executemany(
            "INSERT OR IGNORE INTO headlines (key, source, title, summary, url, published, "
            "fetched) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    h.key,
                    h.source,
                    h.title,
                    h.summary,
                    h.url,
                    int(h.published.timestamp()),
                    int(now.timestamp()),
                )
                for h in fresh
            ],
        )
        return fresh

    def pending(self, now: dt.datetime | None = None) -> list[Headline]:
        """Unclassified headlines young enough to matter, oldest first."""
        instant = ensure_utc(now or self._clock.now())
        oldest = instant - dt.timedelta(hours=self._config.headline_max_age_hours)
        rows = self._db.query(
            "SELECT key, source, title, published, summary, url FROM headlines "
            "WHERE method IS NULL AND published >= ? ORDER BY published",
            (int(oldest.timestamp()),),
        )
        return [
            Headline(r[0], r[1], r[2], dt.datetime.fromtimestamp(r[3], tz=dt.UTC), r[4], r[5])
            for r in rows
        ]

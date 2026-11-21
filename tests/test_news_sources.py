"""Economic calendar and headline sources, on fake HTTP (no network)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping

import pytest

from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import CalendarSourceConfig, NewsConfig
from goldsweep.data.calendar import (
    CalendarService,
    EconomicEvent,
    FinanceCalendarProvider,
    ForexFactoryProvider,
    blackout_events,
    is_blackout_event,
    parse_financecalendar,
    parse_forexfactory,
    post_news_windows,
)
from goldsweep.data.http import HttpError, http_get, safe_url
from goldsweep.data.news import (
    FinnhubSource,
    NewsService,
    RssSource,
    headline_key,
    parse_date,
    parse_feed,
    parse_finnhub,
    plain_text,
)
from goldsweep.storage.db import Database
from tests.helpers import utc

NOW = utc(2026, 11, 17, 12, 0)  # Tuesday
CONFIG = NewsConfig()

# Shapes taken from the live feeds on 2026-11-17.
FF_FEED = [
    {
        "title": "Core Retail Sales m/m",
        "country": "USD",
        "date": "2026-11-17T08:30:00-05:00",
        "impact": "High",
        "forecast": "0.4%",
        "previous": "0.9%",
    },
    {
        "title": "FOMC Member Barr Speaks",
        "country": "USD",
        "date": "2026-11-17T05:00:00-05:00",
        "impact": "Low",
        "forecast": "",
        "previous": "",
    },
    {
        "title": "Unemployment Claims",
        "country": "USD",
        "date": "2026-11-19T08:30:00-05:00",
        "impact": "Medium",
        "forecast": "199K",
        "previous": "200K",
    },
    {
        "title": "German ZEW Economic Sentiment",
        "country": "EUR",
        "date": "2026-11-17T05:00:00-05:00",
        "impact": "Medium",
        "forecast": "38.5",
        "previous": "39.3",
    },
    {
        "title": "Bank Holiday",
        "country": "JPY",
        "date": "2026-11-23T00:00:00-05:00",
        "impact": "Holiday",
        "forecast": "",
        "previous": "",
    },
    {"title": "broken", "country": "USD", "date": "next tuesday", "impact": "High"},
]
FC_RESPONSE = {
    "from": "2026-11-17",
    "to": "2026-11-24",
    "count": 3,
    "events": [
        {
            "id": 901,
            "title": "Core Retail Sales m/m",
            "name": "Retail sales",
            "country": "US",
            "impact": "high",
            "release_at_utc": "2026-11-17T13:30:00+00:00",
            "all_day": False,
            "consensus": "0.4",
            "prior": "0.9",
            "actual": "0.6",
            "status": "released",
            "url": "https://www.financecalendar.com/e/901",
        },
        {
            "id": 902,
            "title": "Consumer Price Index",
            "country": "US",
            "impact": "high",
            "release_at_utc": "2026-11-18T13:30:00+00:00",
            "consensus": "0.3",
            "prior": "0.4",
            "actual": None,
            "status": "confirmed",
        },
        {
            "id": 903,
            "title": "Cancelled speech",
            "country": "US",
            "impact": "high",
            "release_at_utc": "2026-11-18T15:00:00+00:00",
            "status": "cancelled",
        },
        {
            "id": 904,
            "title": "Thanksgiving",
            "country": "US",
            "impact": "holiday",
            "date": "2026-11-26",
            "all_day": True,
        },
    ],
    "attribution": {"source": "FinanceCalendar.com"},
}
FED_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Fed</title>
<item><title>Federal Reserve issues FOMC statement </title>
<link><![CDATA[https://www.federalreserve.gov/a.htm]]></link>
<description>Federal Reserve issues FOMC statement</description>
<pubDate>Wed, 18 Nov 2026 19:00:00 GMT</pubDate></item>
<item><title>No date here</title><link>https://x</link></item>
</channel></rss>"""
ECB_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>
<item><title> Frank Elderson: Smart supervision for small banks</title>
<link>https://www.ecb.europa.eu/x.html</link>
<pubDate>Mon, 16 Nov 2026 14:00:00 +0100</pubDate></item></channel></rss>"""
ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Gold &amp; silver rally</title><link href="https://example.com/g"/>
<updated>2026-11-17T09:15:00Z</updated>
<summary>&lt;p&gt;Bullion &lt;b&gt;up&lt;/b&gt;&lt;/p&gt;</summary>
</entry></feed>"""
FINNHUB = [
    {
        "category": "forex",
        "datetime": 1795430000,
        "headline": "Dollar slides as yields ease",
        "id": 7,
        "image": "",
        "related": "",
        "source": "Reuters",
        "summary": "The dollar fell.",
        "url": "https://example.com/1",
    },
    {"category": "forex", "datetime": "soon", "headline": "bad time"},
    {"category": "forex", "datetime": 1795430100, "headline": "   ", "source": ""},
]


class FakeWeb:
    """Serves canned bodies by URL prefix and records every request."""

    def __init__(self, routes: Mapping[str, bytes | Exception]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.calls.append((url, dict(headers)))
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body
        raise HttpError(f"HTTP 404 from {url}", status=404)


def event(
    title: str, impact: str = "high", currency: str = "USD", **kwargs: object
) -> EconomicEvent:
    values: dict[str, object] = {
        "source": "test",
        "event_id": title,
        "time": NOW,
        "currency": currency,
        "title": title,
        "impact": impact,
    }
    values.update(kwargs)
    return EconomicEvent(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Calendar parsing
# --------------------------------------------------------------------------- #


def test_forex_factory_feed() -> None:
    events = parse_forexfactory(FF_FEED)
    assert len(events) == 5  # the unreadable date is dropped
    retail = events[0]
    assert (retail.time, retail.currency, retail.impact) == (
        utc(2026, 11, 17, 13, 30),
        "USD",
        "high",
    )
    assert (retail.forecast, retail.previous, retail.actual) == ("0.4%", "0.9%", None)
    assert events[1].forecast is None  # empty strings mean "not published"
    holiday = events[4]
    assert holiday.impact == "holiday" and holiday.all_day
    assert parse_forexfactory({"not": "a list"}) == []
    assert events[0].event_id == parse_forexfactory(FF_FEED)[0].event_id  # stable


def test_financecalendar_response() -> None:
    events = parse_financecalendar(FC_RESPONSE)
    assert [e.event_id for e in events] == ["901", "902", "904"]  # cancelled left out
    released = events[0]
    assert (released.currency, released.actual, released.forecast) == ("USD", "0.6", "0.4")
    assert released.released and released.url == "https://www.financecalendar.com/e/901"
    assert not events[1].released
    assert events[2].all_day and events[2].time == utc(2026, 11, 26)
    assert parse_financecalendar(FC_RESPONSE["events"])[0].event_id == "901"  # a bare list too
    assert parse_financecalendar({"events": [{"title": "no time"}]}) == []


def test_financecalendar_key_goes_in_a_header() -> None:
    web = FakeWeb({FinanceCalendarProvider.BASE_URL: json.dumps(FC_RESPONSE).encode()})
    provider = FinanceCalendarProvider("secret-key", currencies=("USD", "EUR"), fetch=web)
    assert len(provider.fetch()) == 3
    url, headers = web.calls[0]
    assert "secret-key" not in url and headers["X-API-Key"] == "secret-key"
    assert "country=US%2CEU" in url and "impact=high%2Cmedium" in url


def test_event_description() -> None:
    text = event("CPI m/m", forecast="0.3%", previous="0.4%", actual="0.5%").describe()
    assert text == "Tue 12:00 UTC USD CPI m/m (high): actual 0.5%, forecast 0.3%, previous 0.4%"
    assert event("Holiday", impact="holiday", all_day=True).describe().startswith("all day")


# --------------------------------------------------------------------------- #
# Blackouts and post-news windows
# --------------------------------------------------------------------------- #


def test_which_releases_block_new_entries() -> None:
    assert is_blackout_event(event("Core Retail Sales m/m"), CONFIG)  # high impact
    assert is_blackout_event(event("Unemployment Claims", impact="medium"), CONFIG)  # keyword
    assert is_blackout_event(event("Fed Chairman Warsh Speaks", impact="low"), CONFIG)
    assert is_blackout_event(event("FOMC Meeting Minutes", impact="low"), CONFIG)
    assert not is_blackout_event(event("FOMC Member Barr Speaks", impact="low"), CONFIG)
    assert not is_blackout_event(event("German ZEW", currency="EUR"), CONFIG)
    assert not is_blackout_event(event("Tourism Index", impact="low"), CONFIG)  # not "ISM"
    assert not is_blackout_event(event("Holiday", all_day=True), CONFIG)


def test_blackout_list_and_post_news_windows() -> None:
    events = parse_forexfactory(FF_FEED)
    (first, second) = blackout_events(events, CONFIG)
    assert (first.title, first.time) == ("USD Core Retail Sales m/m", utc(2026, 11, 17, 13, 30))
    assert second.title == "USD Unemployment Claims"
    windows = post_news_windows(events, CONFIG)
    assert windows[0] == (utc(2026, 11, 17, 13, 45), utc(2026, 11, 17, 15, 0))
    off = CONFIG.model_copy(update={"post_news_sweep_mode": False})
    assert post_news_windows(events, off) == []


# --------------------------------------------------------------------------- #
# The calendar service
# --------------------------------------------------------------------------- #


def service(
    db: Database, web: FakeWeb, clock: ManualClock, *, fc_limit: int | None = 10
) -> CalendarService:
    return CalendarService(
        [
            (
                FinanceCalendarProvider("k", fetch=web),
                CalendarSourceConfig(
                    name="financecalendar",
                    refresh_min=180,
                    fast_refresh_min=1,
                    daily_request_limit=fc_limit,
                ),
            ),
            (
                ForexFactoryProvider(fetch=web),
                CalendarSourceConfig(name="forexfactory", refresh_min=60),
            ),
        ],
        db,
        CONFIG,
        clock=clock,
    )


def routes() -> dict[str, bytes | Exception]:
    return {
        FinanceCalendarProvider.BASE_URL: json.dumps(FC_RESPONSE).encode(),
        ForexFactoryProvider.URL: json.dumps(FF_FEED).encode(),
    }


def test_refresh_stores_merges_and_credits(db: Database) -> None:
    web, clock = FakeWeb(routes()), ManualClock(NOW)
    calendar = service(db, web, clock)
    report = calendar.refresh()
    assert report.fetched == {"financecalendar": 3, "forexfactory": 5}
    events = calendar.events(utc(2026, 11, 17), utc(2026, 11, 20))
    retail = [e for e in events if e.title == "Core Retail Sales m/m"]
    assert len(retail) == 1 and retail[0].source == "financecalendar"  # first source wins
    assert retail[0].actual == "0.6"
    assert calendar.attributions(events) == [
        FinanceCalendarProvider.attribution,
        ForexFactoryProvider.attribution,
    ]
    assert calendar.next_blackout_event(NOW).title == "Core Retail Sales m/m"
    assert calendar.blackout_events(utc(2026, 11, 17), utc(2026, 11, 20))
    assert calendar.post_news_windows(utc(2026, 11, 17), utc(2026, 11, 20))
    assert [p.name for p in calendar.providers] == ["financecalendar", "forexfactory"]


def test_refresh_follows_each_sources_schedule(db: Database) -> None:
    web, clock = FakeWeb(routes()), ManualClock(NOW)
    calendar = service(db, web, clock)
    calendar.refresh()
    clock.advance(dt.timedelta(minutes=30))
    report = calendar.refresh()
    assert report.fetched == {}
    assert report.skipped["forexfactory"] == "not due (every 60 min)"
    clock.advance(dt.timedelta(minutes=31))
    assert set(calendar.refresh().fetched) == {"forexfactory"}  # 61 min: Forex Factory only
    assert set(calendar.refresh(force=True).fetched) == {"financecalendar", "forexfactory"}


def test_faster_refresh_next_to_a_release(db: Database) -> None:
    web, clock = FakeWeb(routes()), ManualClock(utc(2026, 11, 17, 13, 25))  # 5 min before
    calendar = service(db, web, clock)
    calendar.refresh()
    clock.advance(dt.timedelta(minutes=2))
    report = calendar.refresh()
    assert "financecalendar" in report.fetched  # every minute near the release
    assert report.skipped["forexfactory"].startswith("not due")  # no actuals: no hurry


def test_daily_request_cap(db: Database) -> None:
    web, clock = FakeWeb(routes()), ManualClock(NOW)
    calendar = service(db, web, clock, fc_limit=2)
    calendar.refresh(force=True)
    calendar.refresh(force=True)
    report = calendar.refresh(force=True)
    assert report.skipped["financecalendar"] == "daily limit of 2 requests reached"
    clock.advance(dt.timedelta(days=1))
    assert "financecalendar" in calendar.refresh(force=True).fetched  # a new UTC day


def test_a_failing_source_does_not_stop_the_other(db: Database) -> None:
    broken = routes() | {FinanceCalendarProvider.BASE_URL: HttpError("HTTP 401", status=401)}
    calendar = service(db, FakeWeb(broken), ManualClock(NOW))
    report = calendar.refresh()
    assert report.errors == {"financecalendar": "HTTP 401"}
    assert report.fetched == {"forexfactory": 5}
    garbled = routes() | {ForexFactoryProvider.URL: b"<html>rate limited</html>"}
    report = service(db, FakeWeb(garbled), ManualClock(NOW)).refresh(force=True)
    assert "forexfactory" in report.errors


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #


def test_http_helper_refuses_plain_http_and_hides_queries() -> None:
    with pytest.raises(HttpError, match="non-HTTPS"):
        http_get("http://example.com/feed")
    assert safe_url("https://a.example/path?token=secret&x=1") == "https://a.example/path"


# --------------------------------------------------------------------------- #
# Headlines
# --------------------------------------------------------------------------- #


def test_rss_and_atom_parsing() -> None:
    (fed,) = parse_feed(FED_RSS, "Federal Reserve")  # the undated item is skipped
    assert fed.title == "Federal Reserve issues FOMC statement"
    assert fed.published == utc(2026, 11, 18, 19, 0)
    assert fed.url == "https://www.federalreserve.gov/a.htm"
    (ecb,) = parse_feed(ECB_RSS, "ECB")
    assert ecb.published == utc(2026, 11, 16, 13, 0)  # +0100 converted to UTC
    assert ecb.summary is None
    (atom,) = parse_feed(ATOM, "Atom")
    assert atom.title == "Gold & silver rally"
    assert atom.summary == "Bullion up"  # tags stripped
    assert (atom.url, atom.published) == ("https://example.com/g", utc(2026, 11, 17, 9, 15))


def test_dates_and_text_helpers() -> None:
    assert parse_date("Mon, 9 Nov 2026 21:00:00 GMT") == utc(2026, 11, 9, 21, 0)
    assert parse_date("2026-11-17 13:43:19") == utc(2026, 11, 17, 13, 43, 19)  # no zone: UTC
    assert parse_date("yesterday") is None and parse_date(None) is None
    assert plain_text("<p>a&nbsp;&amp;   b</p>", 50) == "a & b"  # every kind of space
    assert plain_text("x" * 20, 10) == "xxxxxxxxx…"
    assert plain_text("<br/>", 10) is None
    assert headline_key("Gold rises!") == headline_key("  gold RISES ")


def test_finnhub_items_and_header() -> None:
    (item,) = parse_finnhub(FINNHUB)
    assert (item.source, item.title) == ("Finnhub/Reuters", "Dollar slides as yields ease")
    assert item.published == dt.datetime.fromtimestamp(1795430000, tz=dt.UTC)
    assert parse_finnhub({"error": "bad"}) == []
    web = FakeWeb({FinnhubSource.URL: json.dumps(FINNHUB).encode()})
    assert len(FinnhubSource("tok", "forex", fetch=web).fetch()) == 1
    url, headers = web.calls[0]
    assert url.endswith("?category=forex") and headers["X-Finnhub-Token"] == "tok"


def test_news_service_keeps_one_row_per_story(db: Database) -> None:
    same_story = FED_RSS.replace(
        b"Federal Reserve issues FOMC statement ", b"FEDERAL RESERVE issues FOMC statement!"
    )
    web = FakeWeb({"https://fed": FED_RSS, "https://copy": same_story, "https://ecb": ECB_RSS})
    clock = ManualClock(utc(2026, 11, 18, 20, 0))
    news = NewsService(
        [
            RssSource("Fed", "https://fed", fetch=web),
            RssSource("Copy", "https://copy", fetch=web),
            RssSource("ECB", "https://ecb", fetch=web),
        ],
        db,
        CONFIG,
        clock=clock,
    )
    report = news.poll()
    assert [h.title for h in report.new] == [
        "Frank Elderson: Smart supervision for small banks",
        "Federal Reserve issues FOMC statement",
    ]
    assert report.fetched == {"Fed": 1, "Copy": 1, "ECB": 1}
    assert news.poll().skipped == {"Fed": "not due", "Copy": "not due", "ECB": "not due"}
    clock.advance(dt.timedelta(minutes=5))
    assert news.poll().new == []  # nothing new the second time
    # Only young, unclassified headlines wait for the classifier.
    assert [h.title for h in news.pending()] == ["Federal Reserve issues FOMC statement"]
    assert len(news.sources) == 3


def test_news_service_reports_failures(db: Database) -> None:
    web = FakeWeb({"https://bad": b"not xml at all <", "https://down": HttpError("timeout")})
    news = NewsService(
        [RssSource("Bad", "https://bad", fetch=web), RssSource("Down", "https://down", fetch=web)],
        db,
        CONFIG,
        clock=ManualClock(NOW),
    )
    report = news.poll()
    assert set(report.errors) == {"Bad", "Down"} and report.new == []


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


def test_http_get_on_a_stubbed_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    seen: list[object] = []

    def ok(request: urllib.request.Request, timeout: float, context: object) -> FakeResponse:
        seen.append(request)
        return FakeResponse(b"payload")

    monkeypatch.setattr(urllib.request, "urlopen", ok)
    assert http_get("https://example.com/feed", {"X-Test": "1"}) == b"payload"
    request = seen[0]
    assert request.get_header("User-agent").startswith("GoldSweep/")  # type: ignore[attr-defined]
    assert request.get_header("X-test") == "1"  # type: ignore[attr-defined]

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"x" * 20))
    with pytest.raises(HttpError, match="larger than 10 bytes"):
        http_get("https://example.com/big", max_bytes=10)

    def status(*_a: object, **_k: object) -> FakeResponse:
        raise urllib.error.HTTPError("https://example.com/f?key=s", 429, "Too Many", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", status)
    with pytest.raises(HttpError, match="HTTP 429") as caught:
        http_get("https://example.com/f?key=secret")
    assert caught.value.status == 429 and "secret" not in str(caught.value)

    def offline(*_a: object, **_k: object) -> FakeResponse:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(HttpError, match="could not reach https://example.com/f: no route"):
        http_get("https://example.com/f")

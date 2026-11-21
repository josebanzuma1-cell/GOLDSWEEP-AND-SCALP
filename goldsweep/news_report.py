"""``python main.py --news``: the news layer in one report.

Refreshes the economic calendar and the headline feeds (each on its own
schedule, so running the report often does not hammer the sources), classifies
new headlines, and prints the news bias, the releases that block new entries,
the buyer/seller pressure meter with its news part, a scenario brief and the
day's LLM usage. Nothing here places a trade.
"""

from __future__ import annotations

import datetime as dt
import sys
from collections.abc import Callable
from typing import Any, TextIO

from goldsweep.ai.news_analyzer import NewsAnalyzer
from goldsweep.ai.news_bias import current_news_bias
from goldsweep.ai.scenarios import ScenarioBuilder, build_context, due_triggers
from goldsweep.analysis.bias import compute_bias
from goldsweep.analysis.indicators import latest_atr
from goldsweep.analysis.pressure import STRUCTURE_BARS, pressure_meter
from goldsweep.analysis.signals import SignalEngine
from goldsweep.core.clock import SESSION_LABELS, Clock, SessionCalculator, SystemClock
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError
from goldsweep.data.calendar import (
    CalendarService,
    FinanceCalendarProvider,
    ForexFactoryProvider,
    is_blackout_event,
)
from goldsweep.data.factory import create_broker
from goldsweep.data.http import Fetcher, http_get
from goldsweep.data.news import FinnhubSource, HeadlineSource, NewsService, RssSource
from goldsweep.storage.db import Database

EXIT_OK = 0
LOOKAHEAD = dt.timedelta(hours=48)
LABEL_WIDTH = 12


def _kv(label: str, value: str) -> str:
    return f"{label:<{LABEL_WIDTH}}: {value}"


def build_calendar(
    settings: Settings, db: Database, *, fetch: Fetcher = http_get, clock: Clock | None = None
) -> tuple[CalendarService, list[str]]:
    """The calendar service, plus notes on sources left out."""
    news = settings.config.news
    providers = []
    notes = []
    for cfg in news.calendar_sources:
        if not cfg.enabled:
            notes.append(f"{cfg.name}: disabled in config.yaml")
        elif cfg.name == "financecalendar":
            key = settings.env.financecalendar_api_key
            if key is None:
                notes.append("financecalendar: no FINANCECALENDAR_API_KEY in .env")
                continue
            provider = FinanceCalendarProvider(
                key.get_secret_value(), currencies=news.blackout_currencies, fetch=fetch
            )
            providers.append((provider, cfg))
        else:
            providers.append((ForexFactoryProvider(fetch=fetch), cfg))
    return CalendarService(providers, db, news, clock=clock), notes


def build_news(
    settings: Settings, db: Database, *, fetch: Fetcher = http_get, clock: Clock | None = None
) -> tuple[NewsService, list[str]]:
    news = settings.config.news
    sources: list[HeadlineSource] = []
    notes = []
    key = settings.env.finnhub_api_key
    if key is None:
        notes.append("finnhub: no FINNHUB_API_KEY in .env")
    else:
        sources += [
            FinnhubSource(key.get_secret_value(), category, fetch=fetch)
            for category in news.finnhub_categories
        ]
    sources += [RssSource(feed.name, feed.url, fetch=fetch) for feed in news.rss_feeds]
    return NewsService(sources, db, news, clock=clock), notes


def run_news(
    settings: Settings,
    *,
    use_mock: bool = False,
    use_llm: bool = True,
    clock: Clock | None = None,
    fetch: Fetcher = http_get,
    broker_factory: Callable[[Settings], BrokerClient] | None = None,
    llm_client: Any | None = None,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout

    def emit(line: str = "") -> None:
        print(line, file=stream, flush=True)

    active_clock = clock or SystemClock()
    now = active_clock.now()
    news = settings.config.news
    zone = settings.display_zone
    emit(
        f"GoldSweep news report   {now:%Y-%m-%d %H:%M} UTC "
        f"({now.astimezone(zone):%H:%M} {settings.config.display_timezone})"
    )
    path = settings.mock_db_path if use_mock else settings.db_path
    with Database(path) as db:
        calendar, calendar_notes = build_calendar(settings, db, fetch=fetch, clock=active_clock)
        report = calendar.refresh(now)
        parts = [f"{name} {count} events (fetched)" for name, count in report.fetched.items()]
        parts += [f"{name}: {reason}" for name, reason in report.skipped.items()]
        parts += [f"{name}: FAILED ({error})" for name, error in report.errors.items()]
        parts += calendar_notes
        emit(_kv("Calendar", " | ".join(parts) or "no source configured"))
        events = calendar.events(now - dt.timedelta(hours=24), now + LOOKAHEAD)
        watched = [e for e in events if is_blackout_event(e, news)]
        upcoming = [e for e in watched if e.time >= now - dt.timedelta(minutes=90)]
        emit(_kv("Next 48 h", f"{len(upcoming)} release(s) that block new entries"))
        before = dt.timedelta(minutes=news.blackout_before_min)
        after = dt.timedelta(minutes=news.blackout_after_min)
        for event in upcoming[:12]:
            window = f"no entries {event.time - before:%H:%M}-{event.time + after:%H:%M}"
            if news.post_news_sweep_mode:
                start = event.time + dt.timedelta(minutes=news.post_news_start_min)
                end = event.time + dt.timedelta(minutes=news.post_news_end_min)
                window += f", post-news {start:%H:%M}-{end:%H:%M}"
            emit(f"  {event.describe()}  [{window}]")

        headlines, headline_notes = build_news(settings, db, fetch=fetch, clock=active_clock)
        poll = headlines.poll(now)
        parts = [f"{name} {count}" for name, count in poll.fetched.items()]
        parts += [f"{name}: FAILED ({error})" for name, error in poll.errors.items()]
        parts += [f"{name}: {reason}" for name, reason in poll.skipped.items()]
        parts += headline_notes
        emit(_kv("Headlines", f"{len(poll.new)} new | " + " | ".join(parts)))

        analyzer = NewsAnalyzer(
            news,
            db,
            client=(llm_client if llm_client is not None else _client(settings))
            if use_llm
            else None,
            clock=active_clock,
            setup_note=_setup_note(settings),
        )
        emit(_kv("Classifier", analyzer.llm_status(now) if use_llm else "off (--no-llm): rules"))
        results = analyzer.classify_pending(headlines.pending(now), now)
        by_llm = sum(1 for _h, r in results if r.method == "llm")
        relevant = [(h, r) for h, r in results if r.analysis.relevant_to_gold]
        emit(
            _kv(
                "Classified",
                f"{len(results)} (model {by_llm}, rules {len(results) - by_llm})"
                f"; {len(relevant)} relevant to gold",
            )
        )
        if use_llm:
            # Why the rules stepped in, once per distinct reason (e.g. no API credit).
            for note in sorted({r.note for _h, r in results if r.method == "rules" and r.note}):
                emit(f"  rules used because: {note}")
        for headline, result in relevant[-6:]:
            a = result.analysis
            emit(
                f"  {a.direction:<8} {a.impact:<6} {headline.published:%H:%M}  "
                f"{headline.title[:90]}"
            )

        bias = current_news_bias(
            db, events, now, news.headline_half_life_min, news.blackout_currencies
        )
        emit(_kv("News bias", bias.describe()))
        for item, weight in bias.top[:4]:
            emit(f"  {weight:+.2f}  {item.time:%a %H:%M}  {item.text[:100]} ({item.kind})")

        _market_section(
            settings,
            db,
            emit,
            now,
            events,
            bias.score,
            use_mock,
            use_llm,
            llm_client,
            broker_factory,
            active_clock,
        )
        emit(
            _kv(
                "LLM today",
                analyzer.usage.day(now).describe()
                + f" (budget US${news.llm_daily_budget_usd:.2f})",
            )
        )
        for credit in calendar.attributions(events):
            emit(_kv("Credit", credit))
    emit("News and scenarios inform the engine; they never place a trade by themselves.")
    return EXIT_OK


def _client(settings: Settings) -> Any | None:
    from goldsweep.ai.llm import make_client

    return make_client(settings)


def _setup_note(settings: Settings) -> str | None:
    from goldsweep.ai.llm import setup_problem

    return setup_problem(settings)


def _market_section(
    settings: Settings,
    db: Database,
    emit: Callable[[str], None],
    now: dt.datetime,
    events: list[Any],
    news_score: float,
    use_mock: bool,
    use_llm: bool,
    llm_client: Any | None,
    broker_factory: Callable[[Settings], BrokerClient] | None,
    clock: Clock,
) -> None:
    """Prices, bias, pressure meter and the scenario brief (needs the broker)."""
    config = settings.config
    factory = broker_factory or (lambda s: create_broker(s, mock=use_mock, clock=clock))
    try:
        with factory(settings) as broker:
            spec = broker.resolve_symbol(config.symbol_candidates)
            trigger = broker.get_rates(spec.name, config.timeframes.trigger, 600)
            liquidity = broker.get_rates(spec.name, config.timeframes.liquidity, 500)
            hourly = broker.get_rates(spec.name, Timeframe.H1, 400)
            meter_bars = broker.get_rates(
                spec.name, config.pressure.timeframe, config.pressure.window
            )
            spread = broker.spread_points(spec.name)
    except BrokerError as exc:
        emit(_kv("Market", f"unavailable ({exc}); no scenario brief"))
        return
    if trigger.empty or hourly.empty:
        emit(_kv("Market", "no bars yet; no scenario brief"))
        return
    engine = SignalEngine.from_settings(settings, point=spec.point, symbol=spec.name)
    engine.on_bar(trigger, liquidity, hourly, spread_points=spread, news_bias=news_score)
    price = float(trigger["close"].iloc[-1])
    levels = engine.day_levels
    bias = compute_bias(
        hourly,
        price=price,
        previous_mid=levels.previous_mid if levels else None,
        news_bias=news_score,
        config=config.strategy.bias,
        swing_lookback=config.strategy.swing_lookback,
    )
    atr = latest_atr(liquidity, config.strategy.atr_period, config.strategy.atr_method)
    source = "synthetic prices (--mock)" if use_mock else spec.name
    emit(
        _kv(
            "Market",
            f"{source} {price:.2f} | H1 bias {bias.bias.value} ({bias.score:+.2f})"
            f" | ATR(M15) {atr or 0:.2f}",
        )
    )
    reading = pressure_meter(
        meter_bars,
        config.pressure,
        hourly=hourly.iloc[-STRUCTURE_BARS:],
        news_bias=news_score,
        swing_lookback=config.strategy.swing_lookback,
    )
    if reading is not None:
        emit(_kv("Pressure", f"{reading.describe()} (news part {reading.components['news']:+.2f})"))
    sessions = SessionCalculator(config.sessions)
    triggers = due_triggers(now, sessions, events, config.news)
    if triggers:
        key, text = triggers[0].key, triggers[0].text
    else:
        status = sessions.status(now)
        upcoming = sessions.next_tradeable_span(now)
        if status.allowed and status.session:
            text = f"now ({SESSION_LABELS[status.session]} open)"
        elif upcoming is not None:
            text = f"now (next window: {upcoming.label} {upcoming.start:%H:%M} UTC)"
        else:
            text = "now"
        key = f"manual-{now:%Y%m%dT%H%M}"
    context = build_context(
        now=now,
        trigger=text,
        price=price,
        atr=atr,
        pools=engine.pools,
        bias=bias.bias.value,
        news_bias=news_score,
        pressure=reading.describe() if reading else None,
        events=[e for e in events if now.date() <= e.time.date() <= now.date()],
        config=config.news,
        min_strength=config.strategy.liquidity.min_sweep_pool_strength,
    )
    builder = ScenarioBuilder(
        config.news,
        db,
        client=(llm_client if llm_client is not None else _client(settings)) if use_llm else None,
        clock=clock,
    )
    for line in builder.build(context, key).describe():
        emit(line)

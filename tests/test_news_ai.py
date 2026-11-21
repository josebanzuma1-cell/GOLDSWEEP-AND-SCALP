"""The AI news layer, with a fake Anthropic client: no network, no cost."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from goldsweep.ai.llm import AnthropicChat, Usage, UsageLog, make_client, response_text
from goldsweep.ai.news_analyzer import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    NewsAnalyzer,
    classify_with_rules,
)
from goldsweep.ai.news_bias import (
    NewsItem,
    combine,
    current_news_bias,
    drivers_of,
    parse_figure,
    release_items,
    surprise_sign,
)
from goldsweep.ai.scenarios import (
    ScenarioBuilder,
    build_context,
    due_triggers,
    nearest_pools,
    template_brief,
)
from goldsweep.analysis.liquidity import PoolKind, Scope, Side, make_pool
from goldsweep.core.clock import ManualClock, SessionCalculator
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, NewsConfig, load_settings
from goldsweep.data.calendar import EconomicEvent
from goldsweep.data.news import Headline, NewsService, RssSource, make_headline
from goldsweep.storage.db import Database
from tests.helpers import utc

NOW = utc(2026, 11, 17, 12, 0)
CONFIG = NewsConfig()
GOOD = {
    "relevant_to_gold": True,
    "direction": "bullish",
    "impact": "high",
    "drivers": ["real_yields", "usd"],
    "confidence": 0.8,
    "one_line_reason": "Rate-cut expectations lower real yields.",
}
_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def answer(
    payload: dict[str, Any] | str,
    *,
    stop: str = "end_turn",
    tokens: tuple[int, int, int, int] = (40, 70, 0, 600),
) -> SimpleNamespace:
    """A Messages API response: a thinking block, then the JSON text."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        stop_reason=stop,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_creation_input_tokens=tokens[2],
            cache_read_input_tokens=tokens[3],
        ),
    )


class FakeClient:
    """Replays prepared answers (or raises prepared errors) and records each request."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("error", response=httpx2.Response(code, request=_REQUEST), body=None)


def headline(title: str, summary: str | None = None, when: dt.datetime = NOW) -> Headline:
    item = make_headline("Test", title, when, summary, None)
    assert item is not None
    return item


def claude(client: Any) -> AnthropicChat | None:
    """The fake SDK client behind the real Claude adapter."""
    return AnthropicChat(client, "claude-haiku-5-5") if client is not None else None


def analyzer(db: Database, client: Any, config: NewsConfig = CONFIG) -> NewsAnalyzer:
    return NewsAnalyzer(config, db, client=claude(client), clock=ManualClock(NOW))


# --------------------------------------------------------------------------- #
# Keyword rules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("title", "relevant", "direction"),
    [
        ("Gold struggles as traders bet on back-to-back Fed rate hikes", True, "bearish"),
        ("Fed signals rate cuts ahead; gold climbs", True, "bullish"),
        ("US Dollar Index: DXY eases from 19-month peak", True, "bullish"),
        ("Ceasefire agreed as dollar strengthens", True, "bearish"),
        ("Missiles hit port as conflict escalates", False, "neutral"),  # no gold-related term
        ("Missile strikes escalate war; safe-haven bullion bid", True, "bullish"),
        ("Norges Bank turns hawkish", False, "neutral"),  # not the Fed
        ("Euro: ECB hawkish tone lifts the single currency", False, "neutral"),  # FX story
        ("Fed Chairman Warsh warns on inflation", True, "neutral"),  # "Warsh" is not "war"
        ("Tech company beats earnings", False, "neutral"),
    ],
)
def test_keyword_rules(title: str, relevant: bool, direction: str) -> None:
    result = classify_with_rules(headline(title))
    assert (result.relevant_to_gold, result.direction) == (relevant, direction)
    assert result.confidence == 0.35  # the rules are never confident


def test_rules_read_gold_in_the_summary_but_direction_in_the_title() -> None:
    result = classify_with_rules(headline("Markets drift", "Gold stays near the trade war highs"))
    assert result.relevant_to_gold and result.direction == "neutral"
    strong = classify_with_rules(headline("Gold jumps as Fed cuts rates"))
    assert (strong.direction, strong.impact) == ("bullish", "high")
    assert "real_yields" in strong.drivers


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #


def test_model_answer_is_used_and_costed(db: Database) -> None:
    client = FakeClient(answer(GOOD))
    news = analyzer(db, client)
    result = news.classify(headline("Fed signals two more rate cuts"))
    assert (result.method, result.model) == ("llm", "claude-haiku-5-5")
    assert result.analysis.direction == "bullish" and result.analysis.drivers == (
        "real_yields",
        "usd",
    )
    (request,) = client.calls
    assert request["model"] == "claude-haiku-5-5"
    assert request["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    assert request["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
    }
    assert "temperature" not in request  # Claude Haiku 5.5 rejects sampling parameters
    assert "Headline: Fed signals two more rate cuts" in request["messages"][0]["content"]
    usage = news.usage.day(NOW)
    assert (usage.calls, usage.input_tokens, usage.cache_read_tokens) == (1, 40, 600)
    # 40 x 0.10 + 600 x 0.10 x 0.1 (cached) + 70 x 0.50, per million tokens.
    assert usage.cost_usd == pytest.approx((40 * 0.10 + 600 * 0.01 + 70 * 0.50) / 1e6)


def test_invalid_answer_is_retried_once(db: Database) -> None:
    client = FakeClient(answer("not json"), answer(GOOD))
    result = analyzer(db, client).classify(headline("Fed cuts"))
    assert result.method == "llm" and len(client.calls) == 2
    outcomes = [row[0] for row in db.query("SELECT outcome FROM llm_usage ORDER BY id")]
    assert outcomes == ["invalid", "ok"]


@pytest.mark.parametrize(
    "bad",
    [
        answer({**GOOD, "direction": "up"}),  # not one of the allowed values
        answer({**GOOD, "confidence": 1.5}),
        answer(GOOD, stop="max_tokens"),
        SimpleNamespace(stop_reason="end_turn", content=[], usage=None),
    ],
)
def test_two_bad_answers_fall_back_to_the_rules(db: Database, bad: Any) -> None:
    client = FakeClient(bad, bad)
    result = analyzer(db, client).classify(headline("Gold jumps as Fed cuts rates"))
    assert result.method == "rules" and result.note.startswith("invalid answer")
    assert result.analysis.direction == "bullish"  # the rules still give an answer
    assert len(client.calls) == 2


def test_refusal_falls_back_to_the_rules(db: Database) -> None:
    client = FakeClient(answer("", stop="refusal"))
    result = analyzer(db, client).classify(headline("Fed cuts"))
    assert (result.method, result.note) == ("rules", "the model declined this headline")
    assert len(client.calls) == 1


def test_a_rejected_key_switches_the_model_off_for_the_run(db: Database) -> None:
    client = FakeClient(status_error(anthropic.AuthenticationError, 401))
    news = analyzer(db, client)
    first = news.classify(headline("Fed cuts"))
    second = news.classify(headline("Fed hikes"))
    assert (first.method, second.method) == ("rules", "rules")
    assert len(client.calls) == 1  # no second attempt with a bad key
    assert "off for this run" in news.llm_status(NOW)


@pytest.mark.parametrize(
    "error",
    [
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.InternalServerError, 500),
        anthropic.APIConnectionError(request=_REQUEST),
    ],
)
def test_passing_errors_skip_one_headline_only(db: Database, error: Exception) -> None:
    client = FakeClient(error, answer(GOOD))
    news = analyzer(db, client)
    assert news.classify(headline("Fed cuts")).note.startswith("API unavailable")
    assert news.classify(headline("Fed cuts again")).method == "llm"


def test_daily_budget_stops_model_calls(db: Database) -> None:
    tiny = CONFIG.model_copy(update={"llm_daily_budget_usd": 0.00001})
    client = FakeClient(answer(GOOD), answer(GOOD))
    news = analyzer(db, client, tiny)
    assert news.classify(headline("Fed cuts")).method == "llm"  # spends about US$0.00004
    after = news.classify(headline("Fed hikes"))
    assert (after.method, after.note) == ("rules", "daily LLM budget reached")
    assert len(client.calls) == 1
    assert news.llm_status(NOW).startswith("daily LLM budget reached")
    tomorrow = NOW + dt.timedelta(days=1)
    assert news.usage.budget_left(tomorrow) == pytest.approx(0.00001)  # resets at 00:00 UTC


def test_status_without_a_model(db: Database) -> None:
    assert "no API key for the model" in analyzer(db, None).llm_status(NOW)
    off = CONFIG.model_copy(update={"llm_enabled": False})
    assert "llm_enabled is false" in analyzer(db, None, off).llm_status(NOW)
    assert analyzer(db, None).classify(headline("Fed cuts")).note == "LLM off"
    assert "today's budget left" in analyzer(db, FakeClient()).llm_status(NOW)


def test_client_comes_from_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    settings = load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)
    client = make_client(settings)
    assert isinstance(client, AnthropicChat)  # nothing is sent yet
    client.close()
    with Database(":memory:") as db:
        analyzer_ = NewsAnalyzer.from_settings(settings, db)
        assert analyzer_.model == "claude-haiku-5-5"
        analyzer_._client.close()  # type: ignore[union-attr]  # noqa: SLF001
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert make_client(load_settings(DEFAULT_CONFIG_PATH, read_env_file=False)) is None


def test_classified_headlines_are_stored(db: Database) -> None:
    feed = (
        b'<rss version="2.0"><channel><item><title>Fed signals rate cuts</title>'
        b"<pubDate>Tue, 17 Nov 2026 11:30:00 GMT</pubDate></item></channel></rss>"
    )
    news = NewsService(
        [RssSource("Feed", "https://feed", fetch=lambda url, headers: feed)],
        db,
        CONFIG,
        clock=ManualClock(NOW),
    )
    news.poll()
    results = analyzer(db, FakeClient(answer(GOOD))).classify_pending(news.pending(), NOW)
    assert [r.method for _h, r in results] == ["llm"]
    row = db.query("SELECT relevant, direction, drivers, method, model FROM headlines")[0]
    assert row == (1, "bullish", '["real_yields", "usd"]', "llm", "claude-haiku-5-5")
    assert news.pending() == []  # classified headlines no longer wait
    assert drivers_of(db, NOW - dt.timedelta(hours=1)) == {"real_yields": 1, "usd": 1}


def test_usage_helpers() -> None:
    assert Usage.from_response(SimpleNamespace(usage=None)) == Usage()
    assert response_text(SimpleNamespace(content=[SimpleNamespace(type="thinking")])) is None
    with Database(":memory:") as db:
        assert "0 calls" in UsageLog(db, CONFIG).day(NOW).describe()


# --------------------------------------------------------------------------- #
# News bias
# --------------------------------------------------------------------------- #


def item(sign: int, minutes_ago: float, impact: float = 1.0, confidence: float = 0.8) -> NewsItem:
    return NewsItem(
        NOW - dt.timedelta(minutes=minutes_ago), sign, impact, confidence, "x", "headline"
    )


def test_news_bias_decays_and_averages() -> None:
    assert combine([item(+1, 0)], NOW, 120).score == pytest.approx(0.8)
    # One half-life old: contribution 0.8 x 0.5, total weight floored at 1.
    assert combine([item(+1, 120)], NOW, 120).score == pytest.approx(0.4)
    mixed = combine([item(+1, 0), item(-1, 0)], NOW, 120)
    assert mixed.score == pytest.approx(0.0) and mixed.items == 2
    many = combine([item(+1, 0)] * 10, NOW, 120)
    assert many.score == pytest.approx(0.8)  # agreement does not exceed the average
    stale = combine([item(+1, 121 * 6)], NOW, 120)
    assert stale.items == 0 and stale.describe().startswith("0.00")
    assert combine([item(0, 0), item(+1, -10)], NOW, 120).items == 0  # neutral, future
    top = combine([item(+1, 0, impact=0.3), item(-1, 0)], NOW, 120).top
    assert top[0][1] < 0  # the largest contribution first


def test_data_surprises() -> None:
    def release(title: str, actual: str | None, forecast: str | None, **kw: Any) -> EconomicEvent:
        values: dict[str, Any] = {
            "source": "t",
            "event_id": title,
            "time": NOW,
            "currency": "USD",
            "title": title,
            "impact": "high",
            "actual": actual,
            "forecast": forecast,
        }
        values.update(kw)
        return EconomicEvent(**values)

    assert parse_figure("0.4%") == 0.4 and parse_figure("199K") == 199_000
    assert parse_figure("-1.2") == -1.2 and parse_figure("1,250K") == 1_250_000
    assert parse_figure("n/a") is None and parse_figure(None) is None
    assert surprise_sign(release("CPI m/m", "0.5%", "0.3%")) == -1  # hot inflation: bearish
    assert surprise_sign(release("Non-Farm Employment Change", "90K", "150K")) == +1
    assert surprise_sign(release("Unemployment Claims", "220K", "199K")) == +1  # weaker: bullish
    assert surprise_sign(release("Retail Sales m/m", "0.4%", "0.4%")) == 0
    assert surprise_sign(release("Fed Chair Speaks", "x", "y")) == 0
    assert surprise_sign(release("Mystery Index", "5", "4")) == 0
    items = release_items(
        [
            release("CPI m/m", "0.5%", "0.3%"),
            release("CPI y/y", None, "3.0%"),  # not released yet
            release("German CPI", "0.5%", "0.3%", currency="EUR"),
        ]
    )
    assert [i.sign for i in items] == [-1]
    assert "bearish for gold" in items[0].text


def test_current_news_bias_from_the_database(db: Database) -> None:
    feed = (
        b'<rss version="2.0"><channel><item><title>Fed signals rate cuts</title>'
        b"<pubDate>Tue, 17 Nov 2026 11:30:00 GMT</pubDate></item></channel></rss>"
    )
    news = NewsService(
        [RssSource("F", "https://f", fetch=lambda u, h: feed)], db, CONFIG, clock=ManualClock(NOW)
    )
    news.poll()
    analyzer(db, FakeClient(answer(GOOD))).classify_pending(news.pending(), NOW)
    cpi = EconomicEvent(
        "t",
        "1",
        NOW - dt.timedelta(minutes=30),
        "USD",
        "CPI m/m",
        "high",
        forecast="0.3%",
        actual="0.5%",
    )
    bias = current_news_bias(db, [cpi], NOW, 120)
    assert bias.items == 2
    # Headline +0.8 at 30 min old, CPI surprise -0.6 at 30 min old: they almost cancel.
    assert bias.score == pytest.approx((0.8 - 0.6) * 0.5**0.25 / (2 * 0.5**0.25), abs=1e-9)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

POOLS = [
    make_pool(PoolKind.ASIAN_LOW, Side.SELL_SIDE, 3840.0, NOW),
    make_pool(PoolKind.PREVIOUS_DAY_LOW, Side.SELL_SIDE, 3825.0, NOW),
    make_pool(PoolKind.ASIAN_HIGH, Side.BUY_SIDE, 3862.0, NOW),
    make_pool(PoolKind.ROUND_NUMBER, Side.BUY_SIDE, 3870.0, NOW, scope=Scope.INTERNAL),
    make_pool(PoolKind.PREVIOUS_DAY_HIGH, Side.BUY_SIDE, 3851.0, NOW).as_swept(),
]
RETAIL = EconomicEvent(
    "t",
    "r",
    utc(2026, 11, 17, 13, 30),
    "USD",
    "Retail Sales m/m",
    "high",
    forecast="0.1%",
    previous="0.8%",
)


def context(bias: str = "LONG", now: dt.datetime = NOW) -> Any:
    return build_context(
        now=now,
        trigger="New York kill zone opening 13:00 UTC",
        price=3850.0,
        atr=4.0,
        pools=POOLS,
        bias=bias,
        news_bias=0.25,
        pressure="Buyers 60% / Sellers 40% (buyers)",
        events=[RETAIL],
        config=CONFIG,
    )


def test_nearest_pools() -> None:
    above, below = nearest_pools(POOLS, 3850.0, 4.0)
    assert [p.price for p in above] == [3862.0, 3870.0]  # the swept PDH is left out
    assert [p.price for p in below] == [3840.0, 3825.0]
    assert (above[0].distance, above[0].atr_multiple) == (12.0, 3.0)
    assert nearest_pools(POOLS, 3850.0, None)[0][0].atr_multiple is None


def test_template_brief() -> None:
    brief = template_brief(context("LONG"), "k")
    names = [case.name for case in brief.cases]
    assert names == ["Bullish case", "Bearish case", "No-trade case"]
    bullish, bearish, no_trade = (case.text for case in brief.cases)
    assert "Asian low 3840.00 ($10.00 / 2.5 ATR below)" in bullish
    assert "toward Asian high 3862.00" in bullish and "may trade this case" in bullish
    assert "Against the LONG H1 bias" in bearish
    assert "13:00-13:45 UTC around USD Retail Sales m/m" in no_trade
    assert any("not forecasts" in note for note in brief.notes)
    assert "needs a higher score" in template_brief(context("NEUTRAL"), "k").cases[0].text
    later = template_brief(context("SHORT", utc(2026, 11, 17, 14, 0)), "k")
    assert "Retail Sales" not in later.cases[-1].text  # that window has passed
    assert json.loads(context().as_json())["pools_above"][0]["label"] == "Asian high"
    assert brief.describe()[0] == "Scenarios for New York kill zone opening 13:00 UTC (template)"


SCENARIOS = {
    "cases": [
        {"name": "Bullish case", "text": "A raid of the Asian low at 3840 that closes back above."},
        {
            "name": "Bearish case",
            "text": "A raid of the Asian high at 3862 that closes back below.",
        },
        {"name": "No-trade case", "text": "No entries 13:00-13:45 UTC around US retail sales."},
    ]
}


def builder(db: Database, client: Any, config: NewsConfig = CONFIG) -> ScenarioBuilder:
    return ScenarioBuilder(config, db, client=claude(client), clock=ManualClock(NOW))


def test_model_written_brief_is_stored(db: Database) -> None:
    client = FakeClient(answer(SCENARIOS))
    scenarios = builder(db, client)
    brief = scenarios.build(context(), "newyork-20261117T1300")
    assert brief.method == "llm" and len(brief.cases) == 3
    request = client.calls[0]
    assert json.loads(request["messages"][0]["content"])["price"] == 3850.0
    assert request["output_config"]["effort"] == "low" and "temperature" not in request
    assert scenarios.built("newyork-20261117T1300")
    stored = db.query("SELECT method, body FROM scenarios")[0]
    assert stored[0] == "llm" and json.loads(stored[1])["cases"][0]["name"] == "Bullish case"


@pytest.mark.parametrize(
    ("reply", "note"),
    [
        (
            answer(
                {
                    "cases": [
                        {"name": "Bullish case", "text": "Guaranteed profit on the raid."},
                        {"name": "Bearish case", "text": "A raid of the Asian high."},
                    ]
                }
            ),
            "made a promise",
        ),
        (answer("", stop="refusal"), "declined"),
        (answer({"cases": []}), "unusable answer"),
        (status_error(anthropic.RateLimitError, 429), "model unavailable"),
    ],
)
def test_unusable_model_briefs_fall_back_to_the_template(
    db: Database, reply: Any, note: str
) -> None:
    brief = builder(db, FakeClient(reply)).build(context(), "k")
    assert brief.method == "template"
    assert any(note in text for text in brief.notes)


def test_template_without_a_model_or_budget(db: Database) -> None:
    assert builder(db, None).build(context(), "a").method == "template"
    broke = CONFIG.model_copy(update={"llm_daily_budget_usd": 0.0})
    client = FakeClient()
    assert builder(db, client, broke).build(context(), "b").method == "template"
    assert client.calls == []


def test_when_briefs_are_due(settings: Any) -> None:
    sessions = SessionCalculator(settings.config.sessions)
    before_london = due_triggers(utc(2026, 11, 17, 6, 40), sessions, [RETAIL], CONFIG)
    assert [t.key for t in before_london] == ["london-20261117T0700"]
    assert before_london[0].text == "London kill zone opening 07:00 UTC"
    before_ny = due_triggers(utc(2026, 11, 17, 12, 45), sessions, [RETAIL], CONFIG)
    assert [t.key for t in before_ny] == ["newyork-20261117T1300", "event-t-r"]
    assert due_triggers(utc(2026, 11, 17, 10, 0), sessions, [RETAIL], CONFIG) == []


def test_pool_names_keep_their_price() -> None:
    crt = make_pool(PoolKind.CRT_LOW, Side.SELL_SIDE, 3840.0, NOW, label="H4 CRT range low")
    round_number = make_pool(
        PoolKind.ROUND_NUMBER, Side.BUY_SIDE, 3860.0, NOW, label="round number 3860"
    )
    ctx = build_context(
        now=NOW,
        trigger="t",
        price=3850.0,
        atr=4.0,
        pools=[crt, round_number],
        bias="LONG",
        news_bias=0.0,
        pressure=None,
        events=[],
        config=CONFIG,
    )
    bullish, bearish = (case.text for case in template_brief(ctx, "k").cases[:2])
    assert "H4 CRT range low 3840.00 ($10.00" in bullish  # a digit in the label is not a price
    assert "round number 3860 ($10.00" in bearish  # the price is not repeated
    weak = build_context(
        now=NOW,
        trigger="t",
        price=3850.0,
        atr=4.0,
        pools=[crt, round_number],
        bias="LONG",
        news_bias=0.0,
        pressure=None,
        events=[],
        config=CONFIG,
        min_strength=0.6,
    )
    assert [p.label for p in weak.below] == ["H4 CRT range low"] and weak.above == ()


def test_the_apis_own_reason_is_reported(db: Database) -> None:
    broke = anthropic.BadRequestError(
        "Error code: 400",
        response=httpx2.Response(400, request=_REQUEST),
        body={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
            },
        },
    )
    news = analyzer(db, FakeClient(broke))
    result = news.classify(headline("Fed cuts"))
    assert result.note == (
        "API rejected the request: BadRequestError: Your credit balance is too low to "
        "access the Anthropic API."
    )
    assert "credit balance is too low" in news.llm_status(NOW)
    brief = builder(db, FakeClient(broke)).build(context(), "k")
    assert any("credit balance is too low" in note for note in brief.notes)


# --------------------------------------------------------------------------- #
# Other providers: Groq (free plan) and any OpenAI-compatible service
# --------------------------------------------------------------------------- #


class FakePost:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __call__(self, url: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((url, payload, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def chat_answer(content: str | None, finish: str = "stop") -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {
            "prompt_tokens": 700,
            "completion_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 500},
        },
    }


def groq(post: FakePost) -> Any:
    from goldsweep.ai.llm import GROQ_URL, PROVIDERS, OpenAICompatibleChat

    return OpenAICompatibleChat(
        GROQ_URL,
        "gsk-test",
        "openai/gpt-oss-20b",
        provider=PROVIDERS["groq"],
        reasoning_effort="low",
        post=post,
    )


def test_groq_request_and_answer() -> None:
    post = FakePost(chat_answer(json.dumps(GOOD)))
    reply = groq(post).complete(
        system="sys",
        user="u",
        schema=OUTPUT_SCHEMA,
        schema_name="headline_analysis",
        max_tokens=1024,
    )
    url, payload, kwargs = post.calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert kwargs["headers"] == {"Authorization": "Bearer gsk-test"}
    assert kwargs["redact"] == ("gsk-test",)
    assert payload["model"] == "openai/gpt-oss-20b"
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
    ]
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "headline_analysis", "strict": True, "schema": OUTPUT_SCHEMA},
    }
    assert payload["reasoning_effort"] == "low" and payload["max_completion_tokens"] == 1024
    assert "temperature" not in payload
    assert reply.stop == "end" and json.loads(reply.text or "") == GOOD
    assert (reply.usage.input_tokens, reply.usage.cache_read_tokens) == (200, 500)
    assert reply.usage.output_tokens == 150


def test_groq_answers_and_errors_map_to_our_terms() -> None:
    from goldsweep.ai.llm import LlmFatalError, LlmTransientError
    from goldsweep.data.http import HttpError

    def ask(answer: Any) -> Any:
        return groq(FakePost(answer)).complete(
            system="s", user="u", schema={}, schema_name="x", max_tokens=10
        )

    assert ask(chat_answer("{}", "length")).stop == "max_tokens"
    assert ask(chat_answer(None, "content_filter")).stop == "refusal"
    assert ask({"choices": []}).text is None
    with pytest.raises(LlmFatalError, match="invalid_api_key"):
        ask(
            HttpError('HTTP 401 from https://api.groq.com {"error": "invalid_api_key"}', status=401)
        )
    with pytest.raises(LlmTransientError):
        ask(HttpError("HTTP 429 rate limit", status=429))
    with pytest.raises(LlmTransientError):
        ask(HttpError("could not reach the server"))
    with pytest.raises(LlmTransientError, match="JSON object"):
        ask(["not", "an", "object"])


def test_classifier_on_groq_costs_nothing(db: Database) -> None:
    post = FakePost(chat_answer(json.dumps(GOOD)))
    news = NewsAnalyzer(CONFIG, db, client=groq(post), clock=ManualClock(NOW))
    result = news.classify(headline("Fed signals two more rate cuts"))
    assert (result.method, result.model) == ("llm", "openai/gpt-oss-20b")
    assert news.usage.day(NOW).cost_usd == 0.0  # the free plan
    assert "on (groq openai/gpt-oss-20b, call caps 6/min and 180/day)" in news.llm_status(NOW)


def test_free_plan_caps(db: Database) -> None:
    replies = [chat_answer(json.dumps(GOOD)) for _ in range(10)]
    post = FakePost(*replies)
    capped = CONFIG.model_copy(update={"llm_max_calls_per_minute": 2, "llm_max_calls_per_day": 3})
    news = NewsAnalyzer(capped, db, client=groq(post), clock=ManualClock(NOW))
    items = [
        headline(f"Fed story number {n}", when=NOW - dt.timedelta(minutes=n)) for n in range(4)
    ]
    first = news.classify_pending(items, NOW)
    assert len(first) == 2 and len(post.calls) == 2  # the minute is full: the rest wait
    later = NOW + dt.timedelta(minutes=2)
    second = news.classify_pending(items[2:], later)
    assert [r.method for _h, r in second] == ["llm", "rules"]  # then the day is full
    assert second[1][1].note == "daily limit of 3 model calls reached"
    assert news.llm_status(later).startswith("daily limit of 3 model calls reached")


def test_prefilter_keeps_unrelated_headlines_away_from_the_model(db: Database) -> None:
    post = FakePost()
    news = NewsAnalyzer(CONFIG, db, client=groq(post), clock=ManualClock(NOW))
    result = news.classify(headline("Apple unveils its new iPhone lineup"))
    assert (result.method, result.note) == ("rules", "no gold-related words (pre-filter)")
    assert post.calls == []
    off = CONFIG.model_copy(update={"llm_prefilter": False})
    post = FakePost(chat_answer(json.dumps({**GOOD, "relevant_to_gold": False})))
    NewsAnalyzer(off, db, client=groq(post), clock=ManualClock(NOW)).classify(
        headline("Apple unveils its new iPhone lineup")
    )
    assert len(post.calls) == 1


def test_provider_choice_from_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from goldsweep.ai.llm import OpenAICompatibleChat, setup_problem

    def load() -> Any:
        return load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)

    monkeypatch.setenv("LLM_PROVIDER", "Groq")
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")  # left over from the Claude setup
    assert setup_problem(load()) == "no GROQ_API_KEY in .env"
    assert make_client(load()) is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    client = make_client(load())
    assert isinstance(client, OpenAICompatibleChat)
    assert client.model == "openai/gpt-oss-20b"  # a Claude id is no use on Groq
    monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.8-27b")
    assert make_client(load()).model == "qwen/qwen3.8-27b"  # type: ignore[union-attr]
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    assert setup_problem(load()) == "no LLM_BASE_URL in .env"
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    local = make_client(load())
    assert isinstance(local, OpenAICompatibleChat) and local.provider.name == "openai_compatible"
    monkeypatch.delenv("LLM_MODEL")
    assert setup_problem(load()) == "no LLM_MODEL in .env"


def test_provider_follows_the_keys_when_not_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goldsweep.ai.llm import provider_name

    def load() -> Any:
        return load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)

    assert provider_name(load()) == "anthropic"
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert provider_name(load()) == "groq"  # the only key there is
    monkeypatch.setenv("LLM_MODEL", "groq")  # a provider written where the model goes
    client = make_client(load())
    assert client is not None and (client.provider.name, client.model) == (
        "groq",
        "openai/gpt-oss-20b",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("LLM_MODEL")
    assert provider_name(load()) == "anthropic"  # both keys: the default provider
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert provider_name(load()) == "groq"  # named outright


def test_model_text_is_made_plain() -> None:
    from goldsweep.ai.scenarios import plain_text

    assert plain_text("market‑structure shift – “FVG” mid") == (
        'market-structure shift - "FVG" mid'
    )

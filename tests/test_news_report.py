"""``python main.py --news`` end to end: fake feeds, the mock broker, a fake model."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from goldsweep import cli
from goldsweep.ai.llm import AnthropicChat
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from goldsweep.data.broker import BrokerConnectionError
from goldsweep.data.calendar import FinanceCalendarProvider, ForexFactoryProvider
from goldsweep.data.mock_broker import MockBroker
from goldsweep.news_report import build_calendar, build_news, run_news
from goldsweep.storage.db import Database
from tests.helpers import utc
from tests.test_news_ai import GOOD, SCENARIOS, FakeClient, answer
from tests.test_news_sources import ECB_RSS, FC_RESPONSE, FED_RSS, FF_FEED, FakeWeb

NOW = utc(2026, 11, 18, 12, 40)  # Wednesday, 20 min before the New York kill zone
FX_RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Gold jumps as Fed signals rate cuts</title>
<pubDate>Wed, 18 Nov 2026 11:50:00 GMT</pubDate></item>
<item><title>Euro: ECB hawkish tone lifts the single currency</title>
<pubDate>Wed, 18 Nov 2026 11:55:00 GMT</pubDate></item>
</channel></rss>"""


def web() -> FakeWeb:
    return FakeWeb(
        {
            ForexFactoryProvider.URL: json.dumps(FF_FEED).encode(),
            FinanceCalendarProvider.BASE_URL: json.dumps(FC_RESPONSE).encode(),
            "https://www.federalreserve.gov": FED_RSS,
            "https://www.ecb.europa.eu": ECB_RSS,
            "https://www.fxstreet.com": FX_RSS,
        }
    )


def report(settings: Settings, **kwargs: Any) -> str:
    out = io.StringIO()
    clock = ManualClock(NOW)
    arguments: dict[str, Any] = {
        "use_mock": True,
        "clock": clock,
        "fetch": web(),
        "broker_factory": lambda s: MockBroker(clock=clock),
        "out": out,
    }
    arguments.update(kwargs)
    assert run_news(settings, **arguments) == 0
    return out.getvalue()


def test_report_with_keyword_rules(settings: Settings) -> None:
    text = report(settings, use_llm=False)
    assert "forexfactory 5 events (fetched)" in text
    assert "financecalendar: no FINANCECALENDAR_API_KEY in .env" in text
    assert "USD Unemployment Claims (medium): forecast 199K" in text
    assert "no entries 13:00-13:45, post-news 13:45-15:00" in text
    assert "finnhub: no FINNHUB_API_KEY in .env" in text
    assert "Classifier  : off (--no-llm): rules" in text
    # The gold headline counts; the euro story does not.
    assert "bullish  high" in text and "Gold jumps as Fed signals rate cuts" in text
    assert "News bias   : +0.26 from 1 recent item(s)" in text  # 0.35 x 0.5 ** (50 / 120)
    assert "Market      : synthetic prices (--mock)" in text
    assert "Pressure    : Buyers" in text and "news part +0.26" in text
    assert "round number 2560 2560" not in text  # labels that hold a price are not repeated
    assert "Scenarios for New York kill zone opening 13:00 UTC (template)" in text
    assert "Credit      : Economic calendar data: Forex Factory weekly feed" in text
    assert "never place a trade" in text


def test_report_with_the_model(settings: Settings) -> None:
    unrelated = answer({**GOOD, "relevant_to_gold": False, "direction": "neutral"})
    # Three young headlines (gold, euro, the Fed statement), then the scenario brief.
    # The gold headline and the Fed statement go to the model; the euro story has no
    # gold-related words, so the pre-filter keeps it away. Then the scenario brief.
    client = FakeClient(answer(GOOD), unrelated, answer(SCENARIOS))
    text = report(settings, llm_client=AnthropicChat(client, "claude-haiku-5-5"))
    assert "Classified  : 3 (model 2, rules 1); 1 relevant to gold" in text
    assert "(llm)" in text  # the model wrote the scenario brief
    assert "LLM today   : 2026-11-18: 3 calls" in text
    assert len(client.calls) == 3


def test_report_without_a_broker(settings: Settings) -> None:
    def broken(_settings: Settings) -> MockBroker:
        raise BrokerConnectionError("terminal not running")

    text = report(settings, use_llm=False, broker_factory=broken)
    assert "Market      : unavailable (terminal not running); no scenario brief" in text
    assert "LLM today" in text  # the rest of the report still runs


def test_second_run_uses_the_cache(settings: Settings) -> None:
    first = web()
    report(settings, use_llm=False, fetch=first)
    second = web()
    text = report(settings, use_llm=False, fetch=second)
    assert "forexfactory: not due (every 60 min)" in text
    assert "Federal Reserve: not due" in text
    assert second.calls == []  # nothing fetched again within the intervals


def test_sources_follow_the_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCECALENDAR_API_KEY", "fc-key")
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-key")
    settings = load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)
    with Database(tmp_path / "x.sqlite3") as db:
        calendar, notes = build_calendar(settings, db, fetch=web())
        assert [p.name for p in calendar.providers] == ["financecalendar", "forexfactory"]
        assert notes == []
        news, notes = build_news(settings, db, fetch=web())
        assert [s.name for s in news.sources][:2] == ["finnhub-forex", "finnhub-general"]
        assert notes == []


def test_cli_routes_news(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_news(settings: Settings, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("goldsweep.news_report.run_news", fake_run_news)
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["--news", "--no-llm", "--mock"]) == 0
    assert calls == [{"use_mock": True, "use_llm": False}]


def test_report_says_why_the_rules_were_used(settings: Settings) -> None:
    import anthropic
    import httpx2

    broke = anthropic.BadRequestError(
        "Error code: 400",
        response=httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com")),
        body={"error": {"message": "Your credit balance is too low to access the Anthropic API."}},
    )
    text = report(settings, llm_client=AnthropicChat(FakeClient(broke, broke), "claude-haiku-5-5"))
    assert "rules used because: API rejected the request: BadRequestError: Your credit" in text

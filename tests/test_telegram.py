"""Telegram client, notifiers, message texts and the JSON POST helper."""

from __future__ import annotations

import datetime as dt
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from goldsweep.alerts import messages
from goldsweep.alerts.telegram import ConsoleNotifier, TelegramBot, make_notifier
from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, load_settings
from goldsweep.data.calendar import EconomicEvent
from goldsweep.data.http import HttpError, http_post_json
from goldsweep.execution.book import (
    BookEvent,
    EventKind,
    ExitReason,
    Leg,
    Trade,
    TradePlan,
)
from goldsweep.storage.db import Database
from tests.helpers import utc

TOKEN = "123456:SECRET-token"
CHAT = 4242
NOW = utc(2026, 11, 17, 12, 0)


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


def update(update_id: int, chat: int, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


def test_send_message(db: Database) -> None:
    post = FakePost({"ok": True, "result": {}})
    bot = TelegramBot(TOKEN, CHAT, db=db, post=post)
    assert bot.send("hello")
    url, payload, kwargs = post.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload == {
        "chat_id": CHAT,
        "text": "hello",
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    assert kwargs["redact"] == (TOKEN,)


def test_long_messages_are_cut_and_failures_reported(db: Database) -> None:
    post = FakePost({"ok": True}, HttpError("HTTP 502"), {"ok": False, "description": "Bad"})
    bot = TelegramBot(TOKEN, CHAT, db=db, post=post)
    assert bot.send("x" * 5000)
    assert len(post.calls[0][1]["text"]) == 4096
    assert not bot.send("down")  # a network error is not a crash
    assert not bot.send("refused")  # neither is a refusal


def test_commands_only_from_the_owners_chat(db: Database) -> None:
    post = FakePost(
        {
            "ok": True,
            "result": [
                update(10, CHAT, "/status"),
                update(11, 999, "/stop"),
                update(12, CHAT, "hello"),
                update(13, CHAT, "/Pause@GoldBot now"),
            ],
        },
        {"ok": True, "result": []},
    )
    bot = TelegramBot(TOKEN, CHAT, db=db, post=post)
    commands = bot.commands(wait_seconds=5)
    assert [(c.name, c.argument) for c in commands] == [("status", ""), ("pause", "now")]
    assert post.calls[0][1]["offset"] == 0 and post.calls[0][1]["timeout"] == 5
    bot.commands()
    assert post.calls[1][1]["offset"] == 14  # never the same update twice
    assert TelegramBot(TOKEN, CHAT, db=db, post=FakePost())._offset == 14  # noqa: SLF001
    broken = TelegramBot(TOKEN, CHAT, post=FakePost(HttpError("timeout")))
    assert broken.commands() == []


def test_make_notifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)
    notifier, bot = make_notifier(settings)
    assert isinstance(notifier, ConsoleNotifier) and bot is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(CHAT))
    configured = load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)
    out = io.StringIO()
    post = FakePost({"ok": True})
    notifier, bot = make_notifier(configured, post=post, stream=out)
    assert isinstance(notifier, TelegramBot) and bot is notifier
    notifier.send("both")
    assert "both" in out.getvalue() and len(post.calls) == 1  # printed and sent


def test_console_survives_a_console_without_emoji() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    assert ConsoleNotifier(stream).send("🟢 gold")
    stream.flush()
    assert raw.getvalue().startswith(b"? gold")


# --------------------------------------------------------------------------- #
# JSON POST
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


def test_post_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    def ok(request: urllib.request.Request, **_kw: Any) -> FakeResponse:
        seen.append(request)
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", ok)
    assert http_post_json("https://api.example/x", {"a": 1}) == {"ok": True}
    assert seen[0].data == b'{"a": 1}' and seen[0].get_method() == "POST"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"<html>"))
    with pytest.raises(HttpError, match="did not answer with JSON"):
        http_post_json("https://api.example/x", {})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"x" * 50))
    with pytest.raises(HttpError, match="too large"):
        http_post_json("https://api.example/x", {}, max_bytes=10)
    with pytest.raises(HttpError, match="non-HTTPS"):
        http_post_json("http://api.example/x", {})


def test_the_bot_token_never_appears_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    def refused(*_a: object, **_k: object) -> FakeResponse:
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"bad token"))  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    with pytest.raises(HttpError) as caught:
        http_post_json(url, {}, redact=(TOKEN,))
    assert TOKEN not in str(caught.value) and "<redacted>" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__

    def offline(*_a: object, **_k: object) -> FakeResponse:
        raise urllib.error.URLError(f"cannot resolve {url}")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(HttpError) as caught:
        http_post_json(url, {}, redact=(TOKEN,))
    assert TOKEN not in str(caught.value)


# --------------------------------------------------------------------------- #
# Message texts
# --------------------------------------------------------------------------- #

PLAN = TradePlan(
    "2026-11-17-NY-01",
    NOW,
    Direction.SHORT,
    "newyork",
    "fvg_mid",
    2031.0,
    2033.0,
    2027.0,
    2023.0,
    NOW + dt.timedelta(minutes=30),
    82.0,
    2.0,
)


def test_trade_messages() -> None:
    def event(kind: EventKind, **kw: Any) -> BookEvent:
        return BookEvent(kind, NOW, PLAN, **kw)

    leg = Leg(0.1, 2027.0, NOW, ExitReason.TP1, 40.0, 0.7)
    stop_leg = Leg(0.1, 2033.1, NOW, ExitReason.STOP, -21.0, 0.7)
    trade = Trade(PLAN, None, NOW, 2031.0, 2033.0, 2027.0, 2023.0, 0.2, 40.0, (leg, stop_leg))
    assert "filled at 2031.00, 0.2 lots" in messages.trade_message(
        event(EventKind.FILLED, price=2031.0, lots=0.2), "PAPER"
    )
    assert "TP1 hit, 0.1 lots closed at 2027.00 (+$40.00)" in messages.trade_message(
        event(EventKind.LEG_CLOSED, leg=leg), "PAPER"
    )
    assert "stop moved (trailed) at 2030.00" in messages.trade_message(
        event(EventKind.STOP_MOVED, detail="trail", price=2030.0), "PAPER"
    )
    closed = messages.trade_message(event(EventKind.CLOSED, trade=trade, equity=10_019.0), "PAPER")
    assert closed.startswith("🏁 PAPER SHORT 2026-11-17-NY-01 closed (stop) at 2033.10: +$19.00")
    assert "withdrawn (the entry was not reached)" in messages.trade_message(
        event(EventKind.EXPIRED, detail="the entry was not reached"), "PAPER"
    )
    assert messages.trade_message(event(EventKind.PLACED), "PAPER") is None


def test_warning_and_helpers() -> None:
    cpi = EconomicEvent(
        "t", "1", NOW + dt.timedelta(minutes=15), "USD", "CPI m/m", "high", forecast="0.3%"
    )
    text = messages.pre_news_warning(cpi, NOW, 30, 15)
    assert text == (
        "⚠️ USD CPI m/m in 15m (12:15 UTC) (forecast 0.3%). No new entries 11:45-12:30 UTC."
    )
    assert messages.duration(dt.timedelta(hours=3, minutes=10)) == "3h10m"
    assert messages.session_name("post_news") == "post-news window"
    assert messages.session_name(None) == "outside the kill zones"
    assert messages.news_line(None, None, NOW) == "News bias: n/a"

"""Telegram: outgoing alerts and incoming commands (spec sections 6 and 8).

Messages go to the one chat in ``TELEGRAM_CHAT_ID``, and commands are accepted
only from that chat; anything else is logged and ignored. Without a token and a
chat id, alerts are printed to the console and logged instead, so the bot runs
the same either way.

The bot token travels in the URL path (that is how the Bot API works), so it is
blanked out of every error message and log line.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO

from goldsweep.core.logging import get_logger
from goldsweep.core.settings import Settings
from goldsweep.data.http import HttpError, http_post_json
from goldsweep.storage.db import Database

log = get_logger(__name__)

MAX_MESSAGE = 4096  # Telegram's limit per message
OFFSET_KEY = "telegram.update_offset"

Poster = Callable[..., object]


class Notifier(ABC):
    @abstractmethod
    def send(self, text: str, *, quiet: bool = False) -> bool:
        """Deliver one message; ``False`` when it could not be delivered.

        ``quiet`` messages arrive without a sound (the hourly heartbeat).
        """


class ConsoleNotifier(Notifier):
    """Prints alerts (used when Telegram is not configured, and in tests)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self.sent: list[str] = []

    def send(self, text: str, *, quiet: bool = False) -> bool:
        self.sent.append(text)
        stream = self._stream or sys.stdout
        try:
            print(text + "\n", file=stream, flush=True)
        except UnicodeEncodeError:
            # A Windows console on an old code page cannot show emoji: replace them.
            encoding = getattr(stream, "encoding", None) or "ascii"
            print(text.encode(encoding, "replace").decode(encoding) + "\n", file=stream, flush=True)
        log.info("alert", text=text[:300])
        return True


@dataclass(frozen=True, slots=True)
class Command:
    name: str  # "status", "pause", ... (without the slash, lower case)
    argument: str
    chat_id: int


class TelegramBot(Notifier):
    """A minimal Bot API client: sendMessage and getUpdates."""

    API = "https://api.telegram.org"

    def __init__(
        self,
        token: str,
        chat_id: int,
        *,
        db: Database | None = None,
        post: Poster = http_post_json,
        echo: Notifier | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._post = post
        self._echo = echo
        self._offset = int(db.get_state(OFFSET_KEY, 0)) if db is not None else 0

    def _call(self, method: str, payload: Mapping[str, Any], timeout: float = 20.0) -> Any:
        answer = self._post(
            f"{self.API}/bot{self._token}/{method}",
            payload,
            timeout=timeout,
            redact=(self._token,),
        )
        if not isinstance(answer, dict) or not answer.get("ok"):
            description = answer.get("description") if isinstance(answer, dict) else answer
            raise HttpError(f"Telegram {method} failed: {description}")
        return answer.get("result")

    def send(self, text: str, *, quiet: bool = False) -> bool:
        if self._echo is not None:
            self._echo.send(text)
        body = text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": self._chat_id,
                    "text": body,
                    "disable_web_page_preview": True,
                    "disable_notification": quiet,
                },
            )
        except HttpError as exc:
            log.warning("telegram message not delivered", error=str(exc))
            return False
        return True

    def commands(self, *, wait_seconds: int = 0) -> list[Command]:
        """New commands from the owner's chat (``/status``, ``/pause``, ...)."""
        try:
            updates = self._call(
                "getUpdates",
                {"offset": self._offset, "timeout": wait_seconds, "allowed_updates": ["message"]},
                timeout=wait_seconds + 15.0,
            )
        except HttpError as exc:
            log.warning("telegram updates not read", error=str(exc))
            return []
        found: list[Command] = []
        for update in updates or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = (message.get("chat") or {}).get("id")
            text = str(message.get("text") or "").strip()
            if chat != self._chat_id:
                log.warning("telegram message from an unknown chat ignored", chat=chat)
                continue
            if not text.startswith("/"):
                continue
            head, _, rest = text[1:].partition(" ")
            found.append(Command(head.split("@")[0].lower(), rest.strip(), int(chat)))
        if self._db is not None:
            self._db.set_state(OFFSET_KEY, self._offset)
        return found


def make_notifier(
    settings: Settings,
    db: Database | None = None,
    *,
    post: Poster = http_post_json,
    stream: TextIO | None = None,
) -> tuple[Notifier, TelegramBot | None]:
    """Telegram when it is configured (alerts are also printed), the console otherwise."""
    console = ConsoleNotifier(stream)
    env = settings.env
    token, chat = env.telegram_bot_token, env.telegram_chat_id
    if not settings.config.alerts.telegram_enabled or token is None or chat is None:
        return console, None
    bot = TelegramBot(token.get_secret_value(), chat, db=db, post=post, echo=console)
    return bot, bot

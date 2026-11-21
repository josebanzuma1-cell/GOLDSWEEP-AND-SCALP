"""Language-model access: providers, cost estimates and the usage log.

The classifier and the scenario writer ask one question of a model: "here is a
system prompt, a user message and a JSON schema; answer with JSON that fits".
:class:`LlmChat` is that question; two adapters answer it:

* :class:`AnthropicChat`: Claude through the Anthropic SDK (spec default,
  ``claude-haiku-5-5``), with structured outputs, low effort and a cached
  system prompt.
* :class:`OpenAICompatibleChat`: any service with an OpenAI-style
  ``/chat/completions`` endpoint and JSON-schema output. **Groq** is preset
  (free plan, ``openai/gpt-oss-20b``); Gemini, OpenRouter or a local Ollama work
  through ``LLM_PROVIDER=openai_compatible`` with ``LLM_BASE_URL``.

Choose with ``LLM_PROVIDER`` in ``.env``. Every call is recorded in the
``llm_usage`` table with its tokens and an estimated cost (zero on a free plan),
and per-minute and per-day call caps keep a free plan inside its limits.

Days are UTC calendar days.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from goldsweep.core.clock import ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import NewsConfig, Settings
from goldsweep.data.http import HttpError, http_post_json
from goldsweep.storage.db import Database

log = get_logger(__name__)

CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10
CLIENT_TIMEOUT_S = 30.0
CLIENT_RETRIES = 2  # the Anthropic SDK retries 408/409/429/5xx and connection errors itself
GROQ_URL = "https://api.groq.com/openai/v1"


@dataclass(frozen=True, slots=True)
class Provider:
    name: str
    default_model: str | None
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    calls_per_minute: int | None  # None: no cap
    calls_per_day: int | None
    key_name: str  # the .env key that enables it


#: Defaults per provider. Groq's free plan allows about 8,000 tokens a minute and
#: 200,000 a day for openai/gpt-oss-20b; one classification uses roughly 1,000
#: tokens, hence about 6 calls a minute and 180 a day.
PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        "anthropic", "claude-haiku-5-5", 0.10, 0.50, None, None, "ANTHROPIC_API_KEY"
    ),
    "groq": Provider("groq", "openai/gpt-oss-20b", 0.0, 0.0, 6, 180, "GROQ_API_KEY"),
    "openai_compatible": Provider("openai_compatible", None, 0.0, 0.0, None, None, "LLM_BASE_URL"),
}


class LlmFatalError(Exception):
    """The provider refused the request itself (key, model, account): stop for this run."""


class LlmTransientError(Exception):
    """A rate limit, an outage or a network problem: try again later."""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_response(cls, response: Any) -> Usage:
        """Token counts of an Anthropic SDK response."""
        usage = getattr(response, "usage", None)

        def count(name: str) -> int:
            return int(getattr(usage, name, 0) or 0)

        return cls(
            input_tokens=count("input_tokens"),
            output_tokens=count("output_tokens"),
            cache_write_tokens=count("cache_creation_input_tokens"),
            cache_read_tokens=count("cache_read_input_tokens"),
        )

    def cost(self, usd_per_mtok_in: float, usd_per_mtok_out: float) -> float:
        rate_in = usd_per_mtok_in / 1_000_000
        rate_out = usd_per_mtok_out / 1_000_000
        return (
            self.input_tokens * rate_in
            + self.cache_write_tokens * rate_in * CACHE_WRITE_FACTOR
            + self.cache_read_tokens * rate_in * CACHE_READ_FACTOR
            + self.output_tokens * rate_out
        )


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str | None
    stop: str  # "end", "max_tokens" or "refusal"
    usage: Usage


class LlmChat(ABC):
    """One question with a JSON-schema answer."""

    provider: Provider
    model: str

    @abstractmethod
    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, max_tokens: int
    ) -> LlmReply:
        """Ask once. Raises :class:`LlmFatalError` or :class:`LlmTransientError`."""

    def close(self) -> None:  # noqa: B027 - optional for adapters
        """Release network resources."""


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #


def describe_api_error(exc: Exception, limit: int = 180) -> str:
    """The API's own explanation (for example "credit balance is too low"), shortened."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"{type(exc).__name__}: {error['message']}"[:limit]
    return f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:limit]


def response_text(response: Any) -> str | None:
    """The first text block of an Anthropic response (thinking blocks may come first)."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return str(block.text)
    return None


class AnthropicChat(LlmChat):
    """Claude via the Anthropic SDK client handed in (or a test double of it)."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model
        self.provider = PROVIDERS["anthropic"]

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, max_tokens: int
    ) -> LlmReply:
        # The SDK loads thousands of small modules, so it is imported only here,
        # when a call is really made.
        import anthropic

        fatal = (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        )
        passing = (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except fatal as exc:
            raise LlmFatalError(describe_api_error(exc)) from None
        except passing as exc:
            raise LlmTransientError(describe_api_error(exc)) from None
        stop = getattr(response, "stop_reason", None)
        return LlmReply(
            text=response_text(response),
            stop="refusal"
            if stop == "refusal"
            else "max_tokens"
            if stop == "max_tokens"
            else "end",
            usage=Usage.from_response(response),
        )

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


# --------------------------------------------------------------------------- #
# OpenAI-compatible services (Groq and others)
# --------------------------------------------------------------------------- #

Poster = Callable[..., object]


class OpenAICompatibleChat(LlmChat):
    """``POST {base_url}/chat/completions`` with a strict JSON-schema answer."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        provider: Provider,
        reasoning_effort: str | None = None,
        post: Poster = http_post_json,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.model = model
        self.provider = provider
        self._effort = reasoning_effort
        self._post = post

    def complete(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, max_tokens: int
    ) -> LlmReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        if self._effort:
            payload["reasoning_effort"] = self._effort
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            answer = self._post(
                self._url,
                payload,
                headers=headers,
                timeout=CLIENT_TIMEOUT_S,
                redact=(self._key,) if self._key else (),
            )
        except HttpError as exc:
            message = str(exc)[:300]
            if exc.status in (400, 401, 403, 404, 422):
                raise LlmFatalError(message) from None
            raise LlmTransientError(message) from None
        return self._reply(answer)

    @staticmethod
    def _reply(answer: object) -> LlmReply:
        if not isinstance(answer, Mapping):
            raise LlmTransientError("the service did not answer with a JSON object")
        choices = answer.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        usage = answer.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
        content = message.get("content")
        return LlmReply(
            text=str(content) if content else None,
            stop="max_tokens"
            if finish == "length"
            else "refusal"
            if finish == "content_filter" or message.get("refusal")
            else "end",
            usage=Usage(
                input_tokens=max(0, int(usage.get("prompt_tokens") or 0) - cached),
                output_tokens=int(usage.get("completion_tokens") or 0),
                cache_read_tokens=cached,
            ),
        )


# --------------------------------------------------------------------------- #
# Choosing the provider
# --------------------------------------------------------------------------- #


def provider_name(settings: Settings) -> str:
    """``LLM_PROVIDER``; when it is empty, the provider the rest of ``.env`` points to.

    A provider name written in ``LLM_MODEL`` (``LLM_MODEL=groq``) counts as the
    choice; otherwise the provider whose key is set, Anthropic first.
    """
    env = settings.env
    if env.llm_provider:
        return env.llm_provider
    written = (env.llm_model or "").strip().lower()
    if written in PROVIDERS:
        return written
    if env.anthropic_api_key is None and env.groq_api_key is not None:
        return "groq"
    return "anthropic"


def resolve_model(provider: Provider, configured: str | None) -> str | None:
    """``LLM_MODEL``, unless it is a provider name or another provider's model.

    ``LLM_MODEL=groq`` names the provider, not a model; a Claude id is no use on
    Groq. Either way the provider's default model is used, with a warning.
    """
    wrong = configured is not None and (
        configured.strip().lower() in PROVIDERS
        or (provider.name != "anthropic" and configured.startswith("claude-"))
    )
    if configured and not wrong:
        return configured
    if configured:
        log.warning(
            "LLM_MODEL ignored: it is not a model of this provider",
            provider=provider.name,
            model=configured,
        )
    return provider.default_model


def setup_problem(settings: Settings) -> str | None:
    """What is missing for the configured provider, or ``None`` when it can be used."""
    if not settings.config.news.llm_enabled:
        return "news.llm_enabled is false"
    env = settings.env
    name = provider_name(settings)
    provider = PROVIDERS[name]
    if name == "anthropic" and env.anthropic_api_key is None:
        return "no ANTHROPIC_API_KEY in .env"
    if name == "groq" and env.groq_api_key is None:
        return "no GROQ_API_KEY in .env"
    if name == "openai_compatible":
        if not env.llm_base_url:
            return "no LLM_BASE_URL in .env"
        if resolve_model(provider, env.llm_model) is None:
            return "no LLM_MODEL in .env"
    return None


def make_client(settings: Settings) -> LlmChat | None:
    """The configured model, or ``None`` when it is switched off or not set up."""
    if setup_problem(settings) is not None:
        return None
    env = settings.env
    provider = PROVIDERS[provider_name(settings)]
    model = resolve_model(provider, env.llm_model)
    assert model is not None
    if provider.name == "anthropic":
        import anthropic

        key = env.anthropic_api_key
        assert key is not None
        sdk = anthropic.Anthropic(
            api_key=key.get_secret_value(), timeout=CLIENT_TIMEOUT_S, max_retries=CLIENT_RETRIES
        )
        return AnthropicChat(sdk, model)
    if provider.name == "groq":
        key = env.groq_api_key
        assert key is not None
        effort = "low" if "gpt-oss" in model else None
        return OpenAICompatibleChat(
            GROQ_URL, key.get_secret_value(), model, provider=provider, reasoning_effort=effort
        )
    other = env.llm_api_key
    return OpenAICompatibleChat(
        str(env.llm_base_url),
        other.get_secret_value() if other else None,
        model,
        provider=provider,
    )


# --------------------------------------------------------------------------- #
# Usage and spending
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DailyUsage:
    day: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float

    def describe(self) -> str:
        return (
            f"{self.day}: {self.calls} calls, {self.input_tokens:,} input + "
            f"{self.cache_read_tokens:,} cached + {self.output_tokens:,} output tokens, "
            f"about US${self.cost_usd:.4f}"
        )


@dataclass(frozen=True, slots=True)
class Limits:
    """What a provider may spend: prices and call caps (config overrides the defaults)."""

    usd_per_mtok_in: float
    usd_per_mtok_out: float
    calls_per_minute: int | None
    calls_per_day: int | None
    daily_budget_usd: float

    @classmethod
    def of(cls, provider: Provider | None, config: NewsConfig) -> Limits:
        base = provider or PROVIDERS["anthropic"]
        return cls(
            usd_per_mtok_in=config.llm_input_usd_per_mtok
            if config.llm_input_usd_per_mtok is not None
            else base.usd_per_mtok_in,
            usd_per_mtok_out=config.llm_output_usd_per_mtok
            if config.llm_output_usd_per_mtok is not None
            else base.usd_per_mtok_out,
            calls_per_minute=config.llm_max_calls_per_minute
            if config.llm_max_calls_per_minute is not None
            else base.calls_per_minute,
            calls_per_day=config.llm_max_calls_per_day
            if config.llm_max_calls_per_day is not None
            else base.calls_per_day,
            daily_budget_usd=config.llm_daily_budget_usd,
        )


class UsageLog:
    """Records each model call and answers "how much today?" and "may I call now?"."""

    def __init__(self, db: Database, config: NewsConfig, provider: Provider | None = None) -> None:
        self._db = db
        self.limits = Limits.of(provider, config)

    def record(
        self, now: dt.datetime, *, purpose: str, model: str, usage: Usage, outcome: str
    ) -> float:
        instant = ensure_utc(now)
        cost = usage.cost(self.limits.usd_per_mtok_in, self.limits.usd_per_mtok_out)
        self._db.execute(
            "INSERT INTO llm_usage (time, day, purpose, model, input_tokens, output_tokens, "
            "cache_write_tokens, cache_read_tokens, cost_usd, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(instant.timestamp()),
                f"{instant:%Y-%m-%d}",
                purpose,
                model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_write_tokens,
                usage.cache_read_tokens,
                cost,
                outcome,
            ),
        )
        log.info(
            "llm call",
            purpose=purpose,
            model=model,
            outcome=outcome,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=round(cost, 6),
        )
        return cost

    def day(self, now: dt.datetime) -> DailyUsage:
        day = f"{ensure_utc(now):%Y-%m-%d}"
        (row,) = self._db.query(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cache_write_tokens), 0), COALESCE(SUM(cache_read_tokens), 0), "
            "COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE day = ?",
            (day,),
        )
        return DailyUsage(
            day, int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]), float(row[5])
        )

    def budget_left(self, now: dt.datetime) -> float:
        return self.limits.daily_budget_usd - self.day(now).cost_usd

    def calls_since(self, moment: dt.datetime) -> int:
        (row,) = self._db.query(
            "SELECT COUNT(*) FROM llm_usage WHERE time > ?", (int(ensure_utc(moment).timestamp()),)
        )
        return int(row[0])

    def minute_full(self, now: dt.datetime) -> bool:
        cap = self.limits.calls_per_minute
        return (
            cap is not None and self.calls_since(ensure_utc(now) - dt.timedelta(minutes=1)) >= cap
        )

    def day_full(self, now: dt.datetime) -> bool:
        cap = self.limits.calls_per_day
        return cap is not None and self.day(now).calls >= cap

    def blocked(self, now: dt.datetime) -> str | None:
        """Why no call may be made today, or ``None``."""
        if self.budget_left(now) <= 0:
            return "daily LLM budget reached"
        if self.day_full(now):
            return f"daily limit of {self.limits.calls_per_day} model calls reached"
        return None

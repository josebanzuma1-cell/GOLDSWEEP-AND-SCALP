"""Plain HTTPS GET for the news layer: standard library only, injectable for tests.

Every network read of the calendar and news sources goes through a
:data:`Fetcher`, so tests hand in a fake and never touch the network. Secrets
travel in request headers, never in URLs, and error messages drop the query
string so nothing sensitive reaches a log.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence

import truststore

from goldsweep import __version__

USER_AGENT = f"GoldSweep/{__version__}"
DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 5_000_000

#: Certificates are checked against the Windows (or macOS / Linux) trust store, which
#: also completes chains whose server leaves out an intermediate certificate (the
#: ECB feed does); Python's default bundle would reject those.
_TLS = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

#: ``fetch(url, headers) -> body``
Fetcher = Callable[[str, Mapping[str, str]], bytes]


class HttpError(Exception):
    """A request failed: no answer, an error status, or an oversized body."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def safe_url(url: str) -> str:
    """``url`` without its query string, for messages and logs."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def http_get(
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> bytes:
    """GET ``url`` and return the body. Raises :class:`HttpError` on any failure."""
    if not url.startswith("https://"):
        raise HttpError(f"refusing a non-HTTPS url: {safe_url(url)}")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", **dict(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_TLS) as response:
            body: bytes = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise HttpError(f"HTTP {exc.code} from {safe_url(url)}", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise HttpError(f"could not reach {safe_url(url)}: {reason}") from exc
    if len(body) > max_bytes:
        raise HttpError(f"response from {safe_url(url)} is larger than {max_bytes:,} bytes")
    return body


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def http_post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    redact: Sequence[str] = (),
    max_bytes: int = MAX_BYTES,
) -> object:
    """POST ``payload`` as JSON and return the decoded JSON answer.

    ``redact`` lists secrets that appear in the URL itself (Telegram puts the bot
    token in the path); they are blanked out of every error message.
    """
    if not url.startswith("https://"):
        raise HttpError(_redact(f"refusing a non-HTTPS url: {safe_url(url)}", redact))
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            **dict(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_TLS) as response:
            raw: bytes = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(500).decode("utf-8", "replace")
        except (OSError, AttributeError):
            pass
        message = f"HTTP {exc.code} from {safe_url(url)} {detail}".strip()
        raise HttpError(_redact(message, redact), status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise HttpError(_redact(f"could not reach {safe_url(url)}: {reason}", redact)) from None
    if len(raw) > max_bytes:
        raise HttpError(_redact(f"response from {safe_url(url)} is too large", redact))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HttpError(_redact(f"{safe_url(url)} did not answer with JSON", redact)) from exc

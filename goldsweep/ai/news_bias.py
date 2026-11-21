"""News bias: classified headlines and data surprises combined into one score.

``news_bias`` runs from -1 (bearish for gold) to +1 (bullish). It feeds the
higher-timeframe bias, the signal score's news component and the news part of
the buyer/seller pressure meter.

Each item contributes ``sign x impact x confidence``, weighted by
``0.5 ** (age / half-life)`` (spec 5.6: half-life 2 hours). The score is the
weighted average, with the total weight floored at 1 so that one old or weak
item cannot swing the score on its own::

    bias = sum(contribution * weight) / max(1, sum(weight))

Two kinds of items:

* **Headlines** that the classifier marked relevant and not neutral.
* **Released US data** with an actual figure and a forecast: hotter than
  forecast (inflation, jobs, growth, retail sales) usually lifts the dollar and
  yields, which is bearish for gold; weaker is bullish. Unemployment and jobless
  claims work the other way round. Only the direction of the surprise counts,
  not its size. The Forex Factory feed has no actual figures, so this needs
  FinanceCalendar.com.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from goldsweep.core.clock import ensure_utc
from goldsweep.data.calendar import EconomicEvent
from goldsweep.storage.db import Database

IMPACT_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
SIGN = {"bullish": 1, "bearish": -1, "neutral": 0}
SURPRISE_CONFIDENCE = 0.6
#: Items older than this many half-lives weigh under 1/64 and are left out.
MAX_HALF_LIVES = 6


@dataclass(frozen=True, slots=True)
class NewsItem:
    time: dt.datetime
    sign: int  # +1 bullish for gold, -1 bearish
    impact: float  # 0..1
    confidence: float  # 0..1
    text: str
    kind: str  # "headline" or "release"


@dataclass(frozen=True, slots=True)
class NewsBias:
    score: float  # -1..+1
    items: int
    top: tuple[tuple[NewsItem, float], ...]  # (item, weighted contribution), largest first

    def describe(self) -> str:
        if self.items == 0:
            return "0.00 (no recent gold-relevant news)"
        return f"{self.score:+.2f} from {self.items} recent item(s)"


def combine(
    items: Sequence[NewsItem], now: dt.datetime, half_life_min: int, *, top: int = 5
) -> NewsBias:
    """The time-decayed news bias of ``items`` at ``now``."""
    instant = ensure_utc(now)
    total = weight_sum = 0.0
    parts: list[tuple[NewsItem, float]] = []
    for item in items:
        age_min = (instant - item.time).total_seconds() / 60.0
        if age_min < 0 or age_min > half_life_min * MAX_HALF_LIVES or item.sign == 0:
            continue
        weight = 0.5 ** (age_min / half_life_min)
        contribution = item.sign * item.impact * item.confidence
        total += contribution * weight
        weight_sum += weight
        parts.append((item, contribution * weight))
    score = total / max(1.0, weight_sum) if parts else 0.0
    parts.sort(key=lambda part: abs(part[1]), reverse=True)
    return NewsBias(max(-1.0, min(1.0, score)), len(parts), tuple(parts[:top]))


def headline_items(db: Database, since: dt.datetime) -> list[NewsItem]:
    """Classified, relevant, non-neutral headlines published since ``since``."""
    rows = db.query(
        "SELECT published, direction, impact, confidence, title, reason FROM headlines "
        "WHERE relevant = 1 AND direction IN ('bullish', 'bearish') AND published >= ? "
        "ORDER BY published",
        (int(ensure_utc(since).timestamp()),),
    )
    return [
        NewsItem(
            time=dt.datetime.fromtimestamp(row[0], tz=dt.UTC),
            sign=SIGN[row[1]],
            impact=IMPACT_WEIGHT.get(row[2], 0.3),
            confidence=float(row[3] or 0.0),
            text=str(row[4]),
            kind="headline",
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# Data surprises
# --------------------------------------------------------------------------- #

#: Higher than forecast is bullish for gold (a weaker economy).
_INVERSE = re.compile(r"unemployment|jobless|claims", re.IGNORECASE)
#: Higher than forecast is bearish for gold (a stronger dollar, higher yields).
_STANDARD = re.compile(
    r"cpi|ppi|pce|inflation|gdp|retail sales|non-?farm|payrolls|employment change|"
    r"hourly earnings|ism|pmi|durable goods|consumer confidence|sentiment|jolts|"
    r"federal funds rate|interest rate",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"([-+]?\d+(?:[.,]\d+)?)\s*([KMBT%]?)", re.IGNORECASE)
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "%": 1.0, "": 1.0}


def parse_figure(text: str | None) -> float | None:
    """``"0.4%"`` -> 0.4, ``"165K"`` -> 165000.0; ``None`` when there is no number."""
    if not text:
        return None
    match = _NUMBER.search(text.replace(",", ""))
    if match is None:
        return None
    return float(match.group(1)) * _SCALE[match.group(2).lower()]


def surprise_sign(event: EconomicEvent) -> int:
    """+1 bullish for gold, -1 bearish, 0 when there is no usable surprise."""
    actual, forecast = parse_figure(event.actual), parse_figure(event.forecast)
    if actual is None or forecast is None or actual == forecast:
        return 0
    hotter = 1 if actual > forecast else -1
    if _INVERSE.search(event.title):
        return hotter
    if _STANDARD.search(event.title):
        return -hotter
    return 0


def release_items(
    events: Sequence[EconomicEvent], currencies: Sequence[str] = ("USD",)
) -> list[NewsItem]:
    items: list[NewsItem] = []
    for event in events:
        if event.currency not in currencies or not event.released:
            continue
        sign = surprise_sign(event)
        if sign == 0:
            continue
        direction = "bullish" if sign > 0 else "bearish"
        items.append(
            NewsItem(
                time=event.time,
                sign=sign,
                impact=IMPACT_WEIGHT.get(event.impact, 0.3),
                confidence=SURPRISE_CONFIDENCE,
                text=f"{event.title}: actual {event.actual} vs forecast {event.forecast} "
                f"({direction} for gold)",
                kind="release",
            )
        )
    return items


def current_news_bias(
    db: Database,
    events: Sequence[EconomicEvent],
    now: dt.datetime,
    half_life_min: int,
    currencies: Sequence[str] = ("USD",),
) -> NewsBias:
    """The news bias at ``now`` from stored headlines and released data."""
    since = ensure_utc(now) - dt.timedelta(minutes=half_life_min * MAX_HALF_LIVES)
    items = headline_items(db, since) + release_items(events, currencies)
    return combine(items, now, half_life_min)


def drivers_of(db: Database, since: dt.datetime) -> dict[str, int]:
    """How often each driver appeared in relevant headlines since ``since``."""
    counts: dict[str, int] = {}
    for (raw,) in db.query(
        "SELECT drivers FROM headlines WHERE relevant = 1 AND published >= ?",
        (int(ensure_utc(since).timestamp()),),
    ):
        for name in json.loads(raw or "[]"):
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

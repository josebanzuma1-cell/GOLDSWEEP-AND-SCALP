"""Grading a scalp signal A+, A or B, and the market picture it is sent with (S11, D118).

The owner trades every signal by hand, so each scalp is graded the way the main
strategy's setups are, from what the bot knows when its retest is confirmed:

* **A+**: backed by a live A+ main setup (or "A+ possible") its way, with the H1 bias,
  no high-impact release within 30 minutes;
* **A**: backed by a live A or A+ main setup, or at a key level (a session or twin-bar
  level, or within 0.2 ATR of a liquidity pool) with the H1 bias; no release within
  30 minutes;
* **B**: with the H1 bias, or the bias is neutral;
* **none**: against the H1 bias with no main setup behind it: recorded, not sent.

The pressure meter is shown, never scored (it is an estimate, CLAUDE.md). Pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from goldsweep.analysis.structure import Direction

#: Grades in order, best first.
GRADES = ("A+", "A", "B")
#: A high-impact release this close (minutes) keeps a scalp at B.
NEWS_MINUTES = 30
#: A retest level this close to a liquidity pool (in ATR) is at a key level.
POOL_ATR = 0.2
#: Engine level kinds that are key levels by themselves (session ranges, twin bars).
KEY_KINDS = frozenset({"asia", "london", "twin"})


@dataclass(frozen=True, slots=True)
class Analysis:
    """The main strategy's picture of the market, given to the scalper each minute."""

    bias: Direction | None = None  # the H1 bias (None: neutral or not known yet)
    bias_score: float | None = None
    news_bias: float | None = None
    pressure: str | None = None  # the meter's own description (an estimate)
    next_event: str = ""  # "USD CPI in 3h10m"
    minutes_to_event: float | None = None
    #: Unswept liquidity pools, nearest first: (price, label).
    above: tuple[tuple[float, str], ...] = field(default_factory=tuple)
    below: tuple[tuple[float, str], ...] = field(default_factory=tuple)

    @property
    def bias_text(self) -> str:
        if self.bias is None:
            return "neutral"
        side = "LONG" if self.bias is Direction.LONG else "SHORT"
        return f"{side} ({self.bias_score:+.2f})" if self.bias_score is not None else side


@dataclass(frozen=True, slots=True)
class Grade:
    """A scalp's grade (None: not sent) and why."""

    letter: str | None
    reasons: tuple[str, ...]

    def at_least(self, lowest: str) -> bool:
        """Is this grade ``lowest`` or better?"""
        if self.letter is None:
            return False
        return GRADES.index(self.letter) <= GRADES.index(lowest)


def key_level(
    level: float, kind: str | None, atr: float, pools: Sequence[tuple[float, str]]
) -> str | None:
    """What makes ``level`` a key level, or None."""
    if kind in KEY_KINDS:
        return {"asia": "an Asian session level", "london": "a London session level"}.get(
            str(kind), "a twin-bar level"
        )
    for price, label in pools:
        if abs(price - level) <= POOL_ATR * atr:
            return f"the {label} pool {price:.2f}"
    return None


def grade_scalp(
    direction: Direction,
    *,
    backing: str,
    backing_label: str,
    analysis: Analysis,
    level: float,
    level_kind: str | None,
    atr: float,
) -> Grade:
    """The grade of a scalp whose retest has been confirmed.

    ``backing`` is the best live main setup its way ("A+", "A", "B" or "none";
    ``backing_label`` names it).
    """
    reasons: list[str] = []
    with_bias = analysis.bias is direction
    against = analysis.bias is not None and not with_bias
    news_soon = analysis.minutes_to_event is not None and analysis.minutes_to_event <= NEWS_MINUTES
    key = key_level(level, level_kind, atr, [*analysis.above, *analysis.below])
    if backing != "none":
        reasons.append(f"backed by the {backing_label} main setup")
    if with_bias:
        reasons.append(f"with the H1 bias ({analysis.bias_text})")
    elif against:
        reasons.append(f"against the H1 bias ({analysis.bias_text})")
    else:
        reasons.append("H1 bias neutral")
    if key is not None:
        reasons.append(f"retest at {key}")
    if news_soon:
        reasons.append(f"high-impact news soon ({analysis.next_event})")
    if not news_soon and backing == "A+" and with_bias:
        return Grade("A+", tuple(reasons))
    if not news_soon and (backing in ("A+", "A") or (key is not None and with_bias)):
        return Grade("A", tuple(reasons))
    if not against:
        return Grade("B", tuple(reasons))
    return Grade(None, tuple(reasons))

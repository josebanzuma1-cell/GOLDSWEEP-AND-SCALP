"""Setup grades and session playbooks (Phase 10, D86).

Each trading session (the Asian/Tokyo, London and New York sessions) has a
playbook: the rule that auto-trades there now, and a *candidate*: the A+ rule the
2024-2026 analysis found for that session, monitored before it may trade. A rule
names one setup model, a minimum score and the confluence tags it needs.

A setup's grade, for alerts and reports:

* **A+**: it meets its session's candidate rule;
* **A**: it clears the alert threshold;
* **B**: below that (not alerted).

Grades are pure functions of a signal and the configuration, so the cached
backtest signals stay valid whatever the rules are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from goldsweep.analysis.bias import Bias
from goldsweep.analysis.signals import POST_NEWS
from goldsweep.core.clock import ASIA, LONDON, NEW_YORK, OVERLAP

if TYPE_CHECKING:
    from goldsweep.analysis.signals import FormingSetup, Signal
    from goldsweep.core.settings import PlaybookRule, PlaybooksConfig, SessionPlaybook

PLAYBOOK_SESSIONS = ("asia", "london", "newyork")
_PLAYBOOK_NAMES = {"asia": "Asia", "london": "London", "newyork": "New York"}
#: What the owner may type after /approve and /revoke.
_PLAYBOOK_ALIASES = {
    "asia": "asia",
    "asian": "asia",
    "tokyo": "asia",
    "london": "london",
    "ldn": "london",
    "newyork": "newyork",
    "ny": "newyork",
}
#: The confluence tags (D85) that ``min_tags`` counts; trend and structure context
#: tags (D86) are asked for by name with ``tags_all``.
CONFLUENCE = frozenset({"smt", "unicorn", "bpr", "judas", "silver_bullet", "ote"})
_EPSILON = 1e-9


def playbook_session(session: str | None) -> str | None:
    """The playbook a setup belongs to: asia, london or newyork.

    The London-New York overlap and the post-news windows count as New York (most
    of the overlap is New York's morning, and the releases are US ones).
    """
    if session == ASIA:
        return "asia"
    if session == LONDON:
        return "london"
    if session in (NEW_YORK, OVERLAP, POST_NEWS):
        return "newyork"
    return None


def playbook_name(playbook: str) -> str:
    """Asia, London or New York."""
    return _PLAYBOOK_NAMES.get(playbook, playbook)


def parse_playbook(text: str) -> str | None:
    """``"New York"``, ``"ny"``, ``"tokyo"`` ... -> the playbook key, or None."""
    key = "".join(ch for ch in text.lower() if ch.isalpha())
    return _PLAYBOOK_ALIASES.get(key)


def meets(signal: Signal, rule: PlaybookRule, *, neutral_extra: float) -> bool:
    """Does ``signal`` satisfy ``rule``?"""
    if signal.model != rule.model:
        return False
    needed = rule.min_score + (neutral_extra if signal.bias.bias is Bias.NEUTRAL else 0.0)
    if signal.score < needed - _EPSILON:
        return False
    tags = set(signal.tags)
    if rule.tags_any and not tags.intersection(rule.tags_any):
        return False
    if not tags.issuperset(rule.tags_all):
        return False
    return len(tags & CONFLUENCE) >= rule.min_tags


def session_playbook(signal: Signal, playbooks: PlaybooksConfig) -> SessionPlaybook | None:
    name = playbook_session(signal.session)
    return getattr(playbooks, name) if name is not None else None


def grade(signal: Signal, playbooks: PlaybooksConfig, *, neutral_extra: float) -> str:
    """A+, A or B (see the module docstring)."""
    book = session_playbook(signal, playbooks)
    if book is not None and book.candidate is not None:
        if meets(signal, book.candidate, neutral_extra=neutral_extra):
            return "A+"
    return "A" if signal.meets_alert else "B"


def a_plus_possible(forming: FormingSetup, playbooks: PlaybooksConfig, session: str | None) -> bool:
    """Can this raid under way still become its session's A+ setup (S8, D114)?

    Only what is known before the signal counts: the model and the Judas tag (the
    raid went beyond the New York midnight open). A candidate that also needs a tag
    the raid cannot show yet (SMT, a trend tag ...) gets no "possible", so the chart
    never promises more than it knows; the score is checked when the signal forms.
    """
    name = playbook_session(session)
    book: SessionPlaybook | None = getattr(playbooks, name) if name is not None else None
    rule = book.candidate if book is not None else None
    if rule is None or rule.model != forming.model:
        return False
    known = {"judas"} if forming.judas else set()
    if rule.tags_all and not set(rule.tags_all) <= known:
        return False
    if rule.tags_any and not known.intersection(rule.tags_any):
        return False
    return len(known & CONFLUENCE) >= rule.min_tags


def rule_conditions(rule: PlaybookRule) -> int:
    """How many conditions a rule adds to the plain model (ties go to fewer)."""
    return (
        int(rule.min_score > 60)
        + int(bool(rule.tags_any))
        + int(rule.min_tags > 0)
        + len(rule.tags_all)
    )


def describe_rule(rule: PlaybookRule) -> str:
    parts = [rule.model, f"score >= {rule.min_score:g}"]
    if rule.tags_any:
        parts.append("with " + " or ".join(rule.tags_any))
    if rule.min_tags:
        parts.append(f"{rule.min_tags}+ confluence tags")
    if rule.tags_all:
        parts.append("and " + " and ".join(rule.tags_all))
    return ", ".join(parts)

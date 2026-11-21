"""Finding each session's A+ candidate rule (Phase 10, D86).

The search is deliberately small and fixed, so it cannot hunt for a lucky rule:

* every model, by a minimum score of 60 / 70 / 80, by the confluence asked for
  (none, Unicorn, Judas, Unicorn or Judas) and by a minimum count of tags (0 / 1 / 2);
* a rule is judged on the trades it would have made in one session only;
* it is **chosen** on the first two-thirds of the period, among rules with at least
  :data:`MIN_TRADES` trades there, by the average result per trade (ties go to the
  rule with fewer conditions, then to more trades);
* the chosen rule must also be **positive on the last third**, which played no
  part in choosing it. If it is not, the session gets no candidate (the next-best
  rule is not tried: that would let the held-out part do the choosing).

A candidate is then monitored on a shadow book before it may trade (stage 2).
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from goldsweep.analysis.grades import (
    PLAYBOOK_SESSIONS,
    describe_rule,
    meets,
    playbook_session,
    rule_conditions,
)
from goldsweep.backtest.metrics import Metrics, summarize
from goldsweep.core.settings import PlaybookRule

if TYPE_CHECKING:
    from goldsweep.backtest.engine import MarketData
    from goldsweep.backtest.simulator import SignalRecord, Simulator
    from goldsweep.core.settings import Settings

MIN_TRADES = 15
SCORES = (60.0, 70.0, 80.0)
TAG_OPTIONS: tuple[tuple[str, ...], ...] = ((), ("unicorn",), ("judas",), ("unicorn", "judas"))
MIN_TAGS = (0, 1, 2)
#: Trend and structure conditions (context tags, all required).
TREND_OPTIONS: tuple[tuple[str, ...], ...] = ((), ("d1_trend",), ("h4_trend",), ("ema_trend",))
DISCLAIMER = (
    "Past results do not predict future results. A candidate is a hypothesis to "
    "monitor, not a promise: every trade can lose."
)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """How one rule did in one session, on the choosing and the held-out parts."""

    session: str
    rule: PlaybookRule
    choosing: Metrics
    held_out: Metrics


@dataclass(frozen=True, slots=True)
class Choice:
    """A session's candidate, or why it has none."""

    session: str
    result: RuleResult | None
    reason: str


def candidate_rules(models: Sequence[str]) -> list[PlaybookRule]:
    """The fixed grid of A+ rules tried for every session."""
    rules: list[PlaybookRule] = []
    for model in models:
        for score in SCORES:
            for tags in TAG_OPTIONS:
                for count in MIN_TAGS:
                    if tags and count == 1:
                        continue  # asking for one of the tags already means one tag
                    for trend in TREND_OPTIONS:
                        rules.append(
                            PlaybookRule(
                                model=model,  # type: ignore[arg-type]
                                min_score=score,
                                tags_any=tags,  # type: ignore[arg-type]
                                min_tags=count,
                                tags_all=trend,  # type: ignore[arg-type]
                            )
                        )
    return rules


def evaluate(
    simulator: Simulator,
    records: Sequence[SignalRecord],
    rules: Sequence[PlaybookRule],
    *,
    start: dt.datetime,
    split: dt.datetime,
    end: dt.datetime,
    neutral_extra: float,
    balance: float,
    sessions: Sequence[str] = PLAYBOOK_SESSIONS,
    progress: Callable[[int, int], None] | None = None,
) -> list[RuleResult]:
    """Trade every rule in every session; split the trades at ``split``."""
    results: list[RuleResult] = []
    total, done = len(sessions) * len(rules), 0
    for session in sessions:
        in_session = [r for r in records if playbook_session(r.signal.session) == session]
        for rule in rules:
            chosen = [r for r in in_session if meets(r.signal, rule, neutral_extra=neutral_extra)]
            trades = []
            if chosen:
                run = simulator.run(chosen, select=lambda _record: None, start=start, end=end)
                trades = run.trades
            first = [t for t in trades if t.plan.time < split]
            second = [t for t in trades if t.plan.time >= split]
            results.append(
                RuleResult(session, rule, summarize(first, balance), summarize(second, balance))
            )
            done += 1
            if progress is not None:
                progress(done, total)
    return results


def choose(
    results: Sequence[RuleResult],
    *,
    sessions: Sequence[str] = PLAYBOOK_SESSIONS,
    min_trades: int = MIN_TRADES,
) -> list[Choice]:
    """Each session's candidate by the rules in the module docstring."""
    choices: list[Choice] = []
    for session in sessions:
        eligible = [
            r
            for r in results
            if r.session == session
            and r.choosing.trades >= min_trades
            and r.choosing.expectancy_r is not None
        ]
        if not eligible:
            choices.append(
                Choice(session, None, f"no rule had {min_trades} trades in the choosing part")
            )
            continue
        best = max(
            eligible,
            key=lambda r: (
                round(r.choosing.expectancy_r or 0.0, 6),
                -rule_conditions(r.rule),
                r.choosing.trades,
            ),
        )
        if (best.choosing.expectancy_r or 0.0) <= 0:
            choices.append(
                Choice(
                    session,
                    None,
                    f"no rule made money in the choosing part (the best, "
                    f"{describe_rule(best.rule)}, made {best.choosing.expectancy_r:+.2f} R "
                    "per trade)",
                )
            )
            continue
        held = best.held_out
        if held.trades == 0 or held.expectancy_r is None or held.expectancy_r <= 0:
            choices.append(
                Choice(
                    session,
                    None,
                    f"the best rule ({describe_rule(best.rule)}, "
                    f"{best.choosing.expectancy_r:+.2f} R per trade) was not positive on the "
                    "held-out part",
                )
            )
            continue
        choices.append(Choice(session, best, "chosen"))
    return choices


def signals_for(args: tuple[Settings, MarketData, dt.datetime, dt.datetime, Path, str]) -> Any:
    """One model's signal pass, cached like the backtester's (a worker process)."""
    from goldsweep.backtest.engine import load_or_generate_signals

    settings, data, start, end, cache_dir, model = args
    records, _cached = load_or_generate_signals(
        settings, data, start, end, cache_dir=cache_dir, model=model
    )
    return records


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def _rule_yaml(rule: PlaybookRule) -> str:
    tags = "[" + ", ".join(rule.tags_any) + "]"
    needed = "[" + ", ".join(rule.tags_all) + "]"
    return (
        f"{{model: {rule.model}, min_score: {rule.min_score:g}, tags_any: {tags}, "
        f"min_tags: {rule.min_tags}, tags_all: {needed}}}"
    )


def suggested_yaml(choices: Sequence[Choice]) -> str:
    """The candidates as lines for the ``playbooks:`` block of config.yaml."""
    lines = []
    for choice in choices:
        value = "null" if choice.result is None else _rule_yaml(choice.result.rule)
        lines.append(f"  {choice.session}:\n    candidate: {value}")
    return "\n".join(lines)


def _fmt(metrics: Metrics) -> tuple[str, str, str, str]:
    if metrics.trades == 0:
        return ("0", "", "", "")
    win = "" if metrics.win_rate is None else f"{metrics.win_rate:.0%}"
    exp = "" if metrics.expectancy_r is None else f"{metrics.expectancy_r:+.2f}"
    pf = "" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
    return (str(metrics.trades), win, exp, pf)


def write_report(
    results: Sequence[RuleResult],
    choices: Sequence[Choice],
    folder: Path,
    *,
    period: str,
    split: dt.datetime,
    top: int = 12,
) -> Path:
    """``report.html`` (every session's best rules) and ``playbooks.yaml``."""
    folder.mkdir(parents=True, exist_ok=True)
    e = html.escape
    parts = [
        "<!doctype html><meta charset='utf-8'><title>GoldSweep session playbooks</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:1100px}"
        "table{border-collapse:collapse;margin:8px 0 24px}td,th{border:1px solid #ccc;"
        "padding:4px 8px;text-align:right}td:first-child{text-align:left}"
        ".chosen{background:#e8f5e9}</style>",
        "<h1>Session playbooks</h1>",
        f"<p>{e(period)}. Rules chosen on trades before {split:%Y-%m-%d}; checked on the "
        f"rest (held out). A rule needs {MIN_TRADES} trades in the choosing part.</p>",
    ]
    for choice in choices:
        parts.append(f"<h2>{e(choice.session)}</h2>")
        if choice.result is None:
            parts.append(f"<p><b>No candidate</b>: {e(choice.reason)}.</p>")
        else:
            parts.append(f"<p><b>Candidate</b>: {e(describe_rule(choice.result.rule))}</p>")
        ranked = sorted(
            (r for r in results if r.session == choice.session and r.choosing.trades > 0),
            key=lambda r: -(r.choosing.expectancy_r or -99.0),
        )[:top]
        parts.append(
            "<table><tr><th>Rule</th><th>Trades</th><th>Won</th><th>R / trade</th>"
            "<th>PF</th><th>Held-out trades</th><th>Won</th><th>R / trade</th><th>PF</th></tr>"
        )
        for r in ranked:
            mark = " class='chosen'" if choice.result is r else ""
            cells = [e(describe_rule(r.rule)), *_fmt(r.choosing), *_fmt(r.held_out)]
            parts.append(f"<tr{mark}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        parts.append("</table>")
    parts.append(f"<p>{e(DISCLAIMER)}</p>")
    report = folder / "report.html"
    report.write_text("\n".join(parts), encoding="utf-8")
    (folder / "playbooks.yaml").write_text(suggested_yaml(choices) + "\n", encoding="utf-8")
    return report

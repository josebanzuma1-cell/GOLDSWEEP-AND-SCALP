"""Session playbooks and A+ grades (Phase 10, D86)."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from goldsweep.analysis.bias import Bias
from goldsweep.analysis.grades import (
    describe_rule,
    grade,
    meets,
    playbook_session,
    rule_conditions,
)
from goldsweep.analysis.signals import Signal
from goldsweep.core.settings import PlaybookRule, PlaybooksConfig, SessionPlaybook
from goldsweep.playbook.analysis import (
    MIN_TRADES,
    RuleResult,
    candidate_rules,
    choose,
    evaluate,
    suggested_yaml,
    write_report,
)
from tests.test_simulator import FILL, QUIET, TO_TP1, TO_TP2, record, simulate


def test_sessions_map_to_three_playbooks() -> None:
    assert [playbook_session(s) for s in ("asia", "london", "newyork")] == [
        "asia",
        "london",
        "newyork",
    ]
    # The overlap and the post-news windows belong to New York's playbook.
    assert playbook_session("overlap") == "newyork"
    assert playbook_session("post_news") == "newyork"
    assert playbook_session(None) is None


def neutral(signal: Signal) -> Signal:
    return dataclasses.replace(signal, bias=dataclasses.replace(signal.bias, bias=Bias.NEUTRAL))


def test_a_rule_checks_model_score_and_confluence(base_signal: Signal) -> None:
    # The textbook long: sweep_mss, score 91.1, tags unicorn, bpr, judas, LONG bias.
    assert base_signal.tags == ("unicorn", "bpr", "judas")
    assert meets(base_signal, PlaybookRule(), neutral_extra=5)
    assert not meets(base_signal, PlaybookRule(model="cisd"), neutral_extra=5)
    assert meets(base_signal, PlaybookRule(min_score=90), neutral_extra=5)
    assert not meets(neutral(base_signal), PlaybookRule(min_score=90), neutral_extra=5)  # 95
    assert meets(base_signal, PlaybookRule(tags_any=("judas",), min_tags=3), neutral_extra=5)
    assert not meets(base_signal, PlaybookRule(tags_any=("smt", "ote")), neutral_extra=5)
    assert not meets(base_signal, PlaybookRule(min_tags=4), neutral_extra=5)


def test_grades(base_signal: Signal) -> None:
    plain = PlaybooksConfig()  # no candidates: nothing can be A+
    assert grade(base_signal, plain, neutral_extra=5) == "A"
    with_candidate = plain.model_copy(
        update={"london": SessionPlaybook(candidate=PlaybookRule(tags_any=("unicorn",)))}
    )
    assert grade(base_signal, with_candidate, neutral_extra=5) == "A+"
    weak = dataclasses.replace(base_signal, score=55.0, tags=())
    assert grade(weak, with_candidate, neutral_extra=5) == "B"


def test_the_rule_grid_is_small_and_fixed() -> None:
    rules = candidate_rules(["sweep_mss", "cisd", "continuation"])
    # 9 score/confluence combinations x 3 scores x 4 trend conditions, per model.
    assert len(rules) == 324 and len(set(rules)) == 324
    assert not [r for r in rules if r.tags_any and r.min_tags == 1]  # redundant pairs left out
    assert describe_rule(PlaybookRule(min_score=70, tags_any=("unicorn", "judas"))) == (
        "sweep_mss, score >= 70, with unicorn or judas"
    )
    assert rule_conditions(PlaybookRule()) == 0
    assert rule_conditions(PlaybookRule(min_score=80, tags_any=("judas",), min_tags=2)) == 3


def result(rule: PlaybookRule, trades: int, avg: float, held: int, held_avg: float) -> RuleResult:
    def metrics(n: int, r: float) -> SimpleNamespace:
        return SimpleNamespace(
            trades=n, expectancy_r=r if n else None, win_rate=None, profit_factor=None
        )

    return RuleResult("london", rule, metrics(trades, avg), metrics(held, held_avg))  # type: ignore[arg-type]


def test_choosing_needs_trades_and_a_positive_held_out_part() -> None:
    plain, strict = PlaybookRule(), PlaybookRule(min_score=80, tags_any=("judas",))
    few = [result(strict, MIN_TRADES - 1, 3.0, 5, 1.0)]
    (choice,) = choose(few, sessions=["london"])
    assert choice.result is None and "15 trades" in choice.reason
    # The best on the choosing part lost on the held-out part: no candidate, and the
    # runner-up is not tried (the held-out part must not do the choosing).
    failing = [result(strict, 20, 1.5, 8, -0.2), result(plain, 30, 1.0, 12, 0.5)]
    (choice,) = choose(failing, sessions=["london"])
    assert choice.result is None and "held-out" in choice.reason
    good = [result(strict, 20, 1.5, 8, 0.4), result(plain, 30, 1.0, 12, 0.5)]
    (choice,) = choose(good, sessions=["london"])
    assert choice.result is not None and choice.result.rule == strict
    # A tie goes to the rule with fewer conditions.
    tie = [result(strict, 20, 1.0, 8, 0.4), result(plain, 20, 1.0, 8, 0.4)]
    (choice,) = choose(tie, sessions=["london"])
    assert choice.result is not None and choice.result.rule == plain


def test_rules_are_validated() -> None:
    with pytest.raises(ValidationError):
        PlaybookRule(model="silver_bullet")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PlaybookRule(tags_any=("rainbow",))  # type: ignore[arg-type]
    assert PlaybooksConfig().asia.trading is None  # Asia trades nothing until approved
    assert PlaybooksConfig().london.trading == PlaybookRule()


def test_evaluate_trades_each_rule_and_splits_the_trades(base_signal: Signal) -> None:
    from tests.test_signals import SIGNAL_TIME
    from tests.test_simulator import make_signal

    signal = make_signal(base_signal)  # London, 07:55, the textbook long
    run = simulate([QUIET, FILL, TO_TP1, TO_TP2], [signal])
    assert len(run.trades) == 1
    # The same simulator setup, used through evaluate().
    from goldsweep.backtest.simulator import PriceSeries, Simulator
    from goldsweep.core.settings import DayBoundaryConfig, NewsConfig
    from goldsweep.core.types import Timeframe
    from goldsweep.data.mock_broker import default_gold_spec
    from tests.helpers import bars
    from tests.test_simulator import BAR, FREE, TWO_TARGETS

    frame = bars([QUIET, FILL, TO_TP1, TO_TP2], SIGNAL_TIME - BAR)
    simulator = Simulator(
        PriceSeries.from_candles(frame, Timeframe.M5, point=default_gold_spec().point),
        default_gold_spec(),
        TWO_TARGETS,
        NewsConfig(),
        FREE,
        DayBoundaryConfig(),
    )
    day = dt.timedelta(days=1)
    results = evaluate(
        simulator,
        [record(signal)],
        [PlaybookRule(), PlaybookRule(model="cisd")],
        start=SIGNAL_TIME - day,
        split=SIGNAL_TIME + day,  # the trade falls in the choosing part
        end=SIGNAL_TIME + 2 * day,
        neutral_extra=5,
        balance=10_000.0,
        sessions=["london", "asia"],
    )
    by_key = {(r.session, r.rule.model): r for r in results}
    assert by_key[("london", "sweep_mss")].choosing.trades == 1
    assert by_key[("london", "sweep_mss")].held_out.trades == 0
    assert by_key[("london", "cisd")].choosing.trades == 0  # another model's rule
    assert by_key[("asia", "sweep_mss")].choosing.trades == 0  # another session


def test_report_and_suggested_config(tmp_path: Path) -> None:
    rule = PlaybookRule(min_score=70, tags_any=("unicorn",))
    good = result(rule, 20, 1.2, 8, 0.6)
    choices = choose([good], sessions=["london"]) + choose([], sessions=["asia"])
    text = suggested_yaml(choices)
    assert "  london:\n    candidate: {model: sweep_mss, min_score: 70, tags_any: [unicorn]" in text
    assert "  asia:\n    candidate: null" in text
    report = write_report(
        [good],
        choices,
        tmp_path,
        period="2024-01-01 .. 2026-10-31",
        split=dt.datetime(2025, 12, 1, tzinfo=dt.UTC),
    )
    page = report.read_text(encoding="utf-8")
    assert "Candidate</b>: sweep_mss, score &gt;= 70, with unicorn" in page
    assert "No candidate" in page and "every trade can lose" in page
    assert (tmp_path / "playbooks.yaml").read_text(encoding="utf-8").startswith("  london:")

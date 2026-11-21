"""The trade rules shared by backtests, paper and live trading (execution/book.py)."""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import RiskConfig
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import (
    ExitReason,
    InvalidPlan,
    TradePlan,
    breakeven_due,
    plan_legs,
    third_target,
    trailed_stop,
)
from tests.helpers import utc

GOLD = default_gold_spec()  # volume step and minimum 0.01


def plan(**changes: object) -> TradePlan:
    values: dict[str, object] = {
        "id": "T-01",
        "time": utc(2026, 11, 18, 8, 0),
        "direction": Direction.LONG,
        "session": "london",
        "entry_mode": "fvg_mid",
        "entry": 2029.0,
        "stop_loss": 2027.0,
        "take_profit_1": 2033.0,
        "take_profit_2": 2034.0,
        "take_profit_3": 2035.0,
        "expires_at": utc(2026, 11, 18, 8, 30),
    }
    values.update(changes)
    return TradePlan(**values)  # type: ignore[arg-type]


def split(the_plan: TradePlan, lots: float, **risk: float) -> list[tuple[float, str]]:
    legs = plan_legs(the_plan, lots, GOLD, RiskConfig(**risk))
    return [(leg.lots, leg.reason.value) for leg in legs]


def test_the_spec_split() -> None:
    assert split(plan(), 0.10) == [(0.05, "tp1"), (0.03, "tp2"), (0.02, "tp3")]
    assert split(plan(), 0.25) == [(0.12, "tp1"), (0.07, "tp2"), (0.06, "tp3")]


def test_missing_targets_hand_their_share_to_an_earlier_one() -> None:
    assert split(plan(take_profit_3=None), 0.10) == [(0.05, "tp1"), (0.05, "tp2")]
    assert split(plan(take_profit_2=None), 0.10) == [(0.08, "tp1"), (0.02, "tp3")]
    assert split(plan(take_profit_2=None, take_profit_3=None), 0.10) == [(0.1, "tp1")]


def test_small_positions_fold_from_the_far_end() -> None:
    assert split(plan(), 0.01) == [(0.01, "tp1")]
    assert split(plan(), 0.02) == [(0.01, "tp1"), (0.01, "tp2")]
    assert split(plan(), 0.03) == [(0.01, "tp1"), (0.02, "tp2")]


def test_split_settings() -> None:
    assert split(plan(), 0.10, partial_close_at_tp1_pct=100) == [(0.1, "tp1")]
    assert split(plan(), 0.10, partial_close_at_tp1_pct=0, partial_close_at_tp2_pct=0) == [
        (0.1, "tp3")
    ]
    assert split(plan(), 0.10, partial_close_at_tp1_pct=60, partial_close_at_tp2_pct=60) == [
        (0.06, "tp1"),
        (0.04, "tp2"),
    ]


def test_third_target() -> None:
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2034.0), 3.0) == 2035.0
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2037.0), 3.0) is None
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, None), 3.0) == 2035.0
    assert third_target(Direction.SHORT, 2029.0, 2031.0, (2025.0, 2024.0), 3.0) == 2023.0
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2034.0), 0.0) is None


def test_tp3_is_checked_and_saved() -> None:
    with pytest.raises(InvalidPlan, match="TP3"):
        plan(take_profit_3=2028.0)
    original = plan()
    assert TradePlan.from_state(original.to_state()) == original
    old = original.to_state()
    del old["take_profit_3"]  # state saved before Phase 6
    assert TradePlan.from_state(old).take_profit_3 is None


def test_breakeven_and_trailing_rules() -> None:
    common = {"fill_price": 2029.0, "initial_stop": 2027.0, "tp1_done": False, "at_r": 1.0}
    assert breakeven_due(long=True, best_price=2031.0, **common)  # type: ignore[arg-type]
    assert not breakeven_due(long=True, best_price=2030.9, **common)  # type: ignore[arg-type]
    assert breakeven_due(long=True, best_price=2029.5, **{**common, "tp1_done": True})  # type: ignore[arg-type]
    nan = float("nan")
    assert (
        trailed_stop(
            long=True, stop=2029.0, swing_low=2030.5, swing_high=nan, close=2031.0, spread=0.2
        )
        == 2030.5
    )
    assert (
        trailed_stop(
            long=True, stop=2029.0, swing_low=2031.5, swing_high=nan, close=2031.0, spread=0.2
        )
        is None
    )
    assert trailed_stop(
        long=False, stop=2031.0, swing_low=nan, swing_high=2029.5, close=2028.0, spread=0.2
    ) == pytest.approx(2029.7)
    assert ExitReason.TP3.value == "tp3"
    assert dt.timedelta(0) == dt.timedelta(0)

"""From a scalp setup to an order: stop, TP ladder, cost gate, children (S1, section 4.4).

The regression case is the SELL of the owner's reference video 2: entry 4058.34,
stop 4067.64 (9.30 away), TP1 4053.69, TP2 4049.05, TP3 4044.40 at 0.5 / 1.0 / 1.5 R.
The video's TP2 and TP3 are one cent further than 4058.34 - 9.30 and - 13.95 give,
which is the rounding of its displayed prices; the test allows that cent.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import AppConfig, CostConfig, ScalperConfig, Settings
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import ExitReason, TradePlan, plan_legs
from goldsweep.scalping.engine import SWEEP_RECLAIM, ScalpSetup
from goldsweep.scalping.plan import LadderCosts, ladder, scalp_management, scalp_plan, scalp_risk
from tests.helpers import utc

MINUTE = dt.timedelta(minutes=1)
FREE = LadderCosts(0.0, 0.0, 0.0)
SIGNAL = utc(2026, 8, 3, 2, 38)


def setup(
    reference: float,
    extreme: float,
    *,
    direction: Direction = Direction.SHORT,
    atr: float = 6.2,
    line: float = math.nan,
) -> ScalpSetup:
    return ScalpSetup(
        index=0,
        time=SIGNAL,
        direction=direction,
        mode=SWEEP_RECLAIM,
        reference=reference,
        extreme=extreme,
        trail_line=line,
        atr=atr,
        level_price=extreme - 1.0,
        level_kind="twin",
        flipped=False,
        sweep_time=SIGNAL - MINUTE,
    )


def plan_of(made_setup: ScalpSetup, costs: LadderCosts = FREE, **changes: object) -> TradePlan:
    made = scalp_plan(
        made_setup,
        ScalperConfig(**({"sl_buffer_atr": 0.0} | changes)),  # type: ignore[arg-type]
        costs,
        plan_id="SC-00001",
        session="asia",
        bar=MINUTE,
    )
    assert made.plan is not None, made.reason
    return made.plan


def test_the_video_two_ladder() -> None:
    plan = plan_of(setup(4058.34, 4067.64))  # 9.30 = 1.5 ATR
    assert (plan.entry, plan.stop_loss) == (4058.34, pytest.approx(4067.64))
    assert [plan.take_profit_1, plan.take_profit_2, plan.take_profit_3] == [
        pytest.approx(4053.69, abs=0.011),
        pytest.approx(4049.05, abs=0.011),
        pytest.approx(4044.40, abs=0.011),
    ]
    assert (plan.entry_mode, plan.risk_reward_1, plan.session) == ("market", 0.5, "asia")
    assert plan.expires_at == SIGNAL + MINUTE and plan.score == 70.0  # a twin-bar level
    assert ladder(4058.34, 4067.64, Direction.SHORT, 0.5) == pytest.approx(
        [4053.69, 4049.04, 4044.39]
    )


def test_the_wide_ladder_and_a_short_stop_on_the_ask() -> None:
    costs = LadderCosts(spread=0.2, slippage=0.0, commission=0.0)
    plan = plan_of(setup(4058.34, 4067.64), costs, tp_step_r=2.0)
    assert plan.stop_loss == pytest.approx(4067.84)  # + the spread: a short is stopped on the ask
    assert plan.take_profit_1 == pytest.approx(4058.34 - 2.0 * 9.5)
    buy = plan_of(setup(2000.0, 1998.0, direction=Direction.LONG, atr=2.0), costs)
    assert buy.stop_loss == pytest.approx(1998.0)  # a long's stop is on the bid
    assert buy.take_profit_3 == pytest.approx(2003.0)


def test_the_cost_gate_widens_the_ladder_or_refuses_the_trade() -> None:
    costs = LadderCosts(spread=0.0, slippage=0.1, commission=0.3)  # 0.40 a round trip
    plan = plan_of(setup(2000.0, 2001.0, atr=1.0), costs)  # R = 1.00, TP1 needs 0.80
    assert plan.risk_reward_1 == pytest.approx(0.8)
    assert plan.take_profit_1 == pytest.approx(1999.2)
    refused = scalp_plan(
        setup(2000.0, 2001.0, atr=1.0),
        ScalperConfig(sl_buffer_atr=0.0, tp_step_r_max=0.7),
        costs,
        plan_id="x",
        session=None,
        bar=MINUTE,
    )
    assert refused.plan is None
    assert refused.reason.startswith("TP1 cannot clear 2x the round-trip cost (0.40)")


def test_the_step_line_stands_in_for_a_stop_too_far_away() -> None:
    far = setup(2000.0, 2004.0, atr=1.0, line=2001.5)  # the X is 4 ATR away, the line 1.5
    assert plan_of(far).stop_loss == pytest.approx(2001.5)
    neither = scalp_plan(
        setup(2000.0, 2004.0, atr=1.0, line=2000.2),
        ScalperConfig(sl_buffer_atr=0.0),
        FREE,
        plan_id="x",
        session=None,
        bar=MINUTE,
    )
    assert neither.plan is None
    assert neither.reason == "no stop within 0.5-3 ATR (4.00 ATR, 0.20 ATR)"


def test_round_trip_costs_from_the_broker_figures() -> None:
    costs = LadderCosts.of(default_gold_spec(), CostConfig(), spread_points=20)
    assert (costs.spread, costs.slippage, costs.commission) == pytest.approx((0.2, 0.1, 0.07))
    assert costs.round_trip == pytest.approx(0.37)
    assert costs.breakeven_offset == pytest.approx(0.17)  # the spread is in the fill already


def test_three_children_forty_thirty_thirty() -> None:
    spec = default_gold_spec()
    plan = plan_of(setup(4058.34, 4067.64))
    risk, split = scalp_risk(AppConfig()), ScalperConfig().split
    legs = plan_legs(plan, 0.10, spec, risk, split=split)
    assert [(leg.lots, leg.reason) for leg in legs] == [
        (0.04, ExitReason.TP1),
        (0.03, ExitReason.TP2),
        (0.03, ExitReason.TP3),
    ]
    assert [leg.target for leg in legs] == [
        plan.take_profit_1,
        plan.take_profit_2,
        plan.take_profit_3,
    ]
    # Too few lots to split: never below the broker's minimum, all of it at TP1.
    (single,) = plan_legs(plan, 0.02, spec, risk, split=split)
    assert (single.lots, single.reason) == (0.02, ExitReason.TP1)


def test_the_scalper_risk_settings_and_management(settings: Settings) -> None:
    risk = scalp_risk(settings.config)
    assert (risk.risk_per_trade_pct, risk.max_open_positions, risk.max_trades_per_day) == (
        0.25,
        1,
        8,
    )
    assert (risk.daily_loss_limit_pct, risk.max_spread_multiple, risk.breakeven_at_r) == (
        1.0,
        1.5,
        0.0,
    )
    assert settings.config.risk.risk_per_trade_pct == 0.5  # the main strategy's own: untouched
    rules = scalp_management(settings.config.scalper, 0.17)
    assert rules.split == (0.4, 0.3, 0.3) and rules.breakeven_offset == 0.17
    assert rules.stop_to_tp1_after_tp2 and rules.step_trail and rules.trail_buffer_atr == 0.1
    assert rules.time_stop == dt.timedelta(minutes=15) and rules.time_stop_min_r == 0.3
    plain = scalp_management(
        settings.config.scalper.model_copy(
            update={
                "adaptive_trail": False,
                "risk": settings.config.scalper.risk.model_copy(update={"time_stop_sec": 0}),
            }
        ),
        0.17,
    )
    assert (plain.breakeven_offset, plain.stop_to_tp1_after_tp2, plain.step_trail) == (
        0.0,
        False,
        False,
    )
    assert plain.time_stop is None


def test_the_config_refuses_impossible_ranges() -> None:
    with pytest.raises(ValueError, match="add up to 1"):
        ScalperConfig(split=(0.5, 0.3, 0.3))
    with pytest.raises(ValueError, match="sl_min_atr must be below"):
        ScalperConfig(sl_min_atr=3.0, sl_max_atr=2.0)
    with pytest.raises(ValueError, match="tp_step_r must not exceed"):
        ScalperConfig(tp_step_r=2.5, tp_step_r_max=2.0)
    with pytest.raises(ValueError, match="duplicates"):
        ScalperConfig(sessions=("asia", "asia"))
    with pytest.raises(ValueError):
        ScalperConfig(strategy="momentum_burst")  # no rules given for it yet (D94)

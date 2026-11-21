"""From a scalp setup to an order: the stop, the TP ladder and the cost gate (section 4.4).

* **Stop.** Beyond the setup's anchor plus ``sl_buffer_atr`` x ATR (a short's stop
  also adds the spread: it is hit on the ask). The anchor is the sweep's X (or the
  rejection wick); when that stop is outside ``sl_min_atr``..``sl_max_atr`` x ATR
  from the entry, the step-trail line is tried instead; when neither fits, no trade.
* **Ladder.** ``TPn = entry +/- n x step`` with ``step = tp_step_r x R``: 0.5 gives
  TP1/TP2/TP3 at 0.5/1.0/1.5 R. One target per child (``split``).
* **Cost gate.** TP1 must be at least ``cost_multiple`` x the round-trip cost away
  (spread, the market entry's slippage, commission). If it is not, the step widens up
  to ``tp_step_r_max``; beyond that, no trade.

Every plan is a :class:`~goldsweep.execution.book.TradePlan`, so it cannot exist
without a valid stop-loss and take-profit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import AppConfig, CostConfig, RiskConfig, ScalperConfig
from goldsweep.data.broker import SymbolSpec
from goldsweep.execution.book import Management, TradePlan
from goldsweep.scalping.engine import ScalpSetup

_EPSILON = 1e-9


def scalp_risk(config: AppConfig) -> RiskConfig:
    """The risk settings of the scalper's own book (D91): the main ones, with its limits."""
    limits = config.scalper.risk
    return config.risk.model_copy(
        update={
            "risk_per_trade_pct": limits.risk_per_scalp_pct,
            "max_open_positions": 1,  # one active scalp (its children are one position)
            "max_trades_per_day": limits.max_scalps_per_day,
            "max_trades_per_session": limits.max_scalps_per_day,
            "daily_loss_limit_pct": limits.daily_loss_limit_pct,
            "max_consecutive_losses": limits.max_consecutive_losses,
            "max_spread_multiple": limits.max_spread_multiple,
            "breakeven_at_r": 0.0,  # breakeven comes with TP1, as for every trade
            "target_mode": "pools",  # the ladder is the scalper's own
        }
    )


def scalp_management(config: ScalperConfig, breakeven_offset: float) -> Management:
    """The scalper's position rules (section 4.5); ``adaptive_trail: false`` keeps only
    the split, the plain breakeven at TP1 and the time stop."""
    adaptive = config.adaptive_trail
    seconds = config.risk.time_stop_sec
    return Management(
        split=config.split,
        breakeven_offset=breakeven_offset if adaptive else 0.0,
        stop_to_tp1_after_tp2=adaptive,
        step_trail=adaptive,
        trail_buffer_atr=config.sl_buffer_atr,
        time_stop=dt.timedelta(seconds=seconds) if seconds > 0 else None,
        time_stop_min_r=config.risk.time_stop_min_r,
    )


@dataclass(frozen=True, slots=True)
class LadderCosts:
    """What a round trip costs, in price units."""

    spread: float
    slippage: float  # on the market entry
    commission: float  # the commission per lot over the value of a 1.00 price move per lot

    @property
    def round_trip(self) -> float:
        return self.spread + self.slippage + self.commission

    @classmethod
    def of(cls, spec: SymbolSpec, costs: CostConfig, spread_points: float) -> LadderCosts:
        value = spec.value_per_price_unit
        return cls(
            spread=spread_points * spec.point,
            slippage=costs.slippage_points * spec.point,
            commission=costs.commission_per_lot / value if value > 0 else 0.0,
        )

    @property
    def breakeven_offset(self) -> float:
        """How far past the entry a stop must sit to lose nothing (spread is in the fill)."""
        return self.slippage + self.commission


@dataclass(frozen=True, slots=True)
class ScalpPlan:
    """A plan, or why there is none."""

    plan: TradePlan | None
    reason: str = ""
    step_r: float = 0.0  # the ladder's step, in R (after any widening)


def scalp_plan(
    setup: ScalpSetup,
    config: ScalperConfig,
    costs: LadderCosts,
    *,
    plan_id: str,
    session: str | None,
    bar: dt.timedelta,
) -> ScalpPlan:
    """The market order for ``setup``, entered at the next bar's open."""
    sign = setup.direction.sign
    atr = setup.atr
    buffer = config.sl_buffer_atr * atr
    lowest, highest = config.sl_min_atr * atr, config.sl_max_atr * atr
    stop: float | None = None
    tried: list[str] = []
    for anchor in (setup.extreme, setup.trail_line):
        if anchor != anchor:  # NaN: the line is not ready
            continue
        level = (
            anchor - buffer if setup.direction is Direction.LONG else anchor + buffer + costs.spread
        )
        distance = (setup.reference - level) * sign
        if distance <= 0:
            tried.append("on the wrong side")
            continue
        if lowest - _EPSILON <= distance <= highest + _EPSILON:
            stop = level
            break
        tried.append(f"{distance / atr:.2f} ATR")
    if stop is None:
        found = ", ".join(tried) or "no anchor"
        return ScalpPlan(
            None,
            f"no stop within {config.sl_min_atr:g}-{config.sl_max_atr:g} ATR ({found})",
        )
    risk = abs(setup.reference - stop)
    step_r = config.tp_step_r
    needed = config.risk.cost_multiple * costs.round_trip
    if step_r * risk < needed - _EPSILON:
        step_r = needed / risk
        if step_r > config.tp_step_r_max + _EPSILON:
            return ScalpPlan(
                None,
                f"TP1 cannot clear {config.risk.cost_multiple:g}x the round-trip cost "
                f"({costs.round_trip:.2f}) within {config.tp_step_r_max:g} R steps",
            )
    targets: list[float | None] = [
        setup.reference + sign * (n + 1) * step_r * risk for n in range(len(config.split))
    ]
    targets += [None] * (3 - len(targets))
    first = targets[0]
    assert first is not None
    plan = TradePlan(
        id=plan_id,
        time=setup.time,
        direction=setup.direction,
        session=session,
        entry_mode="market",
        entry=setup.reference,
        stop_loss=stop,
        take_profit_1=first,
        take_profit_2=targets[1],
        take_profit_3=targets[2],
        expires_at=setup.time + bar,
        score=setup.score,
        risk_reward_1=step_r,
    )
    return ScalpPlan(plan, "", step_r)


def ladder(
    entry: float, stop: float, direction: Direction, step_r: float, count: int = 3
) -> list[float]:
    """The TP ladder alone: ``count`` targets ``step_r`` R apart (for display and tests)."""
    risk = abs(entry - stop)
    return [entry + direction.sign * (n + 1) * step_r * risk for n in range(count)]

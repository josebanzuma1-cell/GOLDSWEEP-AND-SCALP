"""The scalper's capped recovery features (S10, D116): the owner's decision of 2026-11-25.

The owner asked for martingale, grid, averaging down and hedging "while keeping the risks
reasonable", and chose capped versions on paper and the demo account only. None of them
turns a losing edge into a winning one: they change how wins and losses fall. Pure
functions, used by the runner (paper and demo) and by ``mode_backtest`` alike:

* **martingale** (:func:`martingale_risk`): after a losing scalp the mode's next scalp
  risks more, at most ``max_steps`` steps and ``max_risk_pct``; back to the base after a
  win or at a new trading day;
* **grid / averaging down** (:func:`add_due`, :func:`add_plan`): while a scalp still has
  its first stop, a bar closing ``add_at_r`` R against its entry adds a position with the
  same stop and one take-profit at the scalp's TP1; a scalp and its adds never risk more
  than ``max_idea_risk_pct`` together;
* **HFT stacking** (:func:`stack_refusal`): another HFT scalp beside open ones only when
  every one of them is risk-free (its stop at or past its entry);
* **the daily budget** (:func:`budget_pct`): a new scalp never risks more than what is
  left of the day's loss limit once the open scalps' risk is counted.

Hedging between modes is a switch in the runner's shared rules; the main strategy is
never hedged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import (
    AverageDownConfig,
    MartingaleConfig,
    RecoveryConfig,
    StackConfig,
)
from goldsweep.execution.book import TradePlan

_EPSILON = 1e-9
_ADD = re.compile(r"-A(\d+)$")


def recovery_line(config: RecoveryConfig) -> str:
    """One line for /status: which of the owner's recovery features are on (D116)."""
    parts = []
    if config.martingale.enabled:
        parts.append(f"martingale up to {config.martingale.max_risk_pct:g}% after a loss")
    if config.average_down.enabled:
        levels = "/".join(f"{level:g}" for level in config.average_down.add_at_r)
        parts.append(f"adds at {levels} R against (grid / averaging down)")
    if config.hft_stack.enabled:
        parts.append(f"HFT stacking up to {config.hft_stack.max_open} while risk-free")
    if config.hedge_modes:
        parts.append("modes may hedge each other")
    return (
        f"Scalper recovery (paper and demo only): {'; '.join(parts) or 'off'}; a scalp and "
        f"its adds at most {config.max_idea_risk_pct:g}%"
    )


def is_add(plan_id: str) -> bool:
    """``2026-11-25-HFT-004-A1`` is an add to ``2026-11-25-HFT-004``."""
    return _ADD.search(plan_id) is not None


def primary_of(plan_id: str) -> str:
    """The scalp an add belongs to (the id itself for a scalp)."""
    return _ADD.sub("", plan_id)


def martingale_risk(
    base_pct: float, results: Sequence[float], config: MartingaleConfig
) -> tuple[float, int]:
    """The next scalp's risk in % and its step, from the mode's results today (oldest
    first; adds left out by the caller): the base times ``multiplier`` per loss in a row
    since the last win, at most ``max_steps`` steps and ``max_risk_pct``."""
    if not config.enabled:
        return base_pct, 0
    losses = 0
    for result in reversed(results):
        if result >= 0:
            break
        losses += 1
    step = min(losses, config.max_steps)
    return min(base_pct * config.multiplier**step, config.max_risk_pct), step


def open_risk(position: Mapping[str, Any], value_per_price_unit: float) -> float:
    """Money an open position still loses at its current stop (0 once it is risk-free)."""
    fill, stop = float(position["fill_price"]), float(position["stop"])
    sign = 1.0 if position["direction"] == Direction.LONG.value else -1.0
    lots = float(position.get("lots", 0.0))
    return max(0.0, (fill - stop) * sign) * lots * value_per_price_unit


def budget_pct(limit_pct: float, today_net: float, open_risk_money: float, equity: float) -> float:
    """What the day's loss limit still allows a new scalp to risk, in % of equity."""
    if equity <= 0:
        return 0.0
    return limit_pct + (today_net - open_risk_money) / equity * 100.0


def stack_refusal(
    positions: Sequence[Mapping[str, Any]],
    waiting: int,
    direction: Direction,
    config: StackConfig,
) -> str | None:
    """None when another HFT scalp may open beside the open ones; otherwise why not."""
    if not positions and not waiting:
        return None
    if not config.enabled:
        return "a scalp of this mode is already open"
    if waiting:
        return "an HFT scalp is still waiting for its fill (no stacking)"
    if len(positions) >= config.max_open:
        return f"{config.max_open} HFT scalps are already open (the stacking cap)"
    for position in positions:
        way = Direction(position["direction"])
        if way is not direction:
            return "an HFT scalp is open the other way"
        if not _risk_free(position, way):
            return "the open HFT scalp is not risk-free yet (stacking waits for its TP1)"
    return None


def _risk_free(position: Mapping[str, Any], direction: Direction) -> bool:
    stop, fill = float(position["stop"]), float(position["fill_price"])
    if direction is Direction.LONG:
        return stop >= fill - _EPSILON
    return stop <= fill + _EPSILON


@dataclass(frozen=True, slots=True)
class OpenScalp:
    """What :func:`add_due` needs from an open primary scalp."""

    plan: TradePlan
    fill: float
    stop: float  # where its stop is now

    @property
    def long(self) -> bool:
        return self.plan.direction is Direction.LONG

    @property
    def risk(self) -> float:
        return abs(self.fill - self.plan.stop_loss)


def add_due(scalp: OpenScalp, close: float, adds_done: int, config: AverageDownConfig) -> bool:
    """Is the next grid / averaging-down add due at this bar's close?

    Only while the scalp still has its first stop and the close is still inside it, and
    only once the close is ``add_at_r[adds_done]`` R against the entry.
    """
    if not config.enabled or adds_done >= len(config.add_at_r):
        return False
    first = scalp.plan.stop_loss
    if abs(scalp.stop - first) > _EPSILON or scalp.risk <= 0:
        return False  # the stop has moved (breakeven+ or a trail): nothing to rescue
    sign = 1.0 if scalp.long else -1.0
    level = scalp.fill - sign * config.add_at_r[adds_done] * scalp.risk
    inside = (close - first) * sign > 0
    return inside and (close - level) * sign <= _EPSILON


def add_plan(
    scalp: OpenScalp,
    number: int,
    *,
    close: float,
    time: Any,
    expires_at: Any,
    risk_pct: float,
    level_r: float,
) -> TradePlan | None:
    """The add: a market order with the scalp's first stop and one take-profit at its TP1.

    It is never larger than it would be at its own level (``level_r`` R against): when the
    close is nearer the stop, its risk shrinks in proportion, so a close just inside the
    stop cannot buy a large position on a tiny distance. None when nothing is left.
    """
    plan = scalp.plan
    distance = abs(close - plan.stop_loss)
    nominal = (1.0 - level_r) * scalp.risk
    if distance <= 0:
        return None
    pct = round(risk_pct * min(1.0, distance / nominal), 4) if nominal > 0 else risk_pct
    if pct <= 0:
        return None
    return TradePlan(
        id=f"{primary_of(plan.id)}-A{number}",
        time=time,
        direction=plan.direction,
        session=plan.session,
        entry_mode="market",
        entry=close,
        stop_loss=plan.stop_loss,
        take_profit_1=plan.take_profit_1,
        take_profit_2=None,
        expires_at=expires_at,
        score=plan.score,
        risk_reward_1=abs(plan.take_profit_1 - close) / abs(close - plan.stop_loss),
        close_by=plan.close_by,
        risk_pct=pct,
    )


def idea_room(
    primary_risk_pct: float, add_risks_pct: Sequence[float], max_idea_risk_pct: float
) -> float:
    """What a scalp and its adds may still add in risk, in % (never negative)."""
    return max(0.0, max_idea_risk_pct - primary_risk_pct - sum(add_risks_pct))

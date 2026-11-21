"""The trade book: entry orders, open positions and their management, bar by bar.

One implementation serves the backtester and the paper executor, so a paper trade
is filled, stopped, taken profit on and trailed exactly as a backtested one.

Prices
------
Bars are **bid** prices, as MetaTrader 5 charts them; the ask is the bid plus
the spread. A long is bought at the ask and sold at the bid; a short is sold at
the bid and bought back at the ask.

When the order of events inside a bar is unknown, the worse case is assumed:

* a stop and a target touched in the same bar: the stop was hit first;
* the bar that fills an entry can stop it out but cannot reach its targets;
* a breakeven or trailing move takes effect from the next bar;
* a limit order fills at its own price, never at a better gap price;
* stop exits (and market entries) suffer the configured slippage; a gap through
  the stop exits at the open.

Management (spec sections 6 and 7b): a position is split into up to three
parts with the same entry and stop and their own targets: TP1 (nearest pool,
``partial_close_at_tp1_pct``), TP2 (next pool, ``partial_close_at_tp2_pct``) and
TP3 (``tp3_r`` R from the entry, the rest). The stop moves to breakeven at
+``breakeven_at_r`` R or when TP1 is hit, and after TP1 it can trail behind
confirmed swing points. The same rules (:func:`plan_legs`, :func:`breakeven_due`,
:func:`trailed_stop`) drive the live executor, so a live trade is managed as a
backtested one.

Every :class:`TradePlan` must carry a stop-loss and a take-profit on the right
sides of the entry; a plan without them cannot even be created (ground rule 2).

A waiting entry order is a trade that has not happened yet, so it must be gone
before any time at which a new trade is forbidden: a news blackout, the rollover
block, the Friday cutoff or the weekend. :func:`entry_deadline` cuts a plan's
expiry to the first such moment when the plan is placed, in every mode, and the
normal expiry path then withdraws the order (outcome ``withdrawn``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import SessionCalculator, ensure_utc
from goldsweep.core.settings import DayBoundaryConfig, NewsConfig, RiskConfig
from goldsweep.data.broker import SymbolSpec
from goldsweep.risk.risk_manager import HIGH_IMPACT, NewsEvent, RiskManager

if TYPE_CHECKING:
    from goldsweep.analysis.refine import RefineResult, RefineSetup

TARGET_NAMES = ("tp1", "tp2", "tp3")

_EPSILON = 1e-9
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MINUTE = dt.timedelta(minutes=1)
#: How the clock's hard stops are named when an entry is withdrawn before one.
_BLOCK_NAMES = {
    "weekend": "the weekend close",
    "rollover block": "the rollover block",
    "Friday cutoff": "the Friday cutoff",
}


def to_ns(moment: dt.datetime) -> int:
    delta = ensure_utc(moment) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def from_ns(nanoseconds: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=nanoseconds // 1000)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Bar:
    """One bid bar and the spread (in price) to apply to it."""

    open_ns: int
    close_ns: int
    open: float
    high: float
    low: float
    close: float
    spread: float

    @property
    def open_time(self) -> dt.datetime:
        return from_ns(self.open_ns)

    @property
    def close_time(self) -> dt.datetime:
        return from_ns(self.close_ns)


class TrailSource(Protocol):
    def at(self, close_ns: int) -> tuple[float, float]:
        """(latest confirmed swing low, swing high) at ``close_ns``; NaN when none."""
        ...


class InvalidPlan(ValueError):
    """A plan without a usable stop-loss and take-profit."""


@dataclass(frozen=True, slots=True)
class TradePlan:
    """What the book needs from a signal to trade it."""

    id: str
    time: dt.datetime  # the signal's bar close; the order exists from the next bar
    direction: Direction
    session: str | None
    entry_mode: str
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    expires_at: dt.datetime
    score: float = 0.0
    risk_reward_1: float = 0.0
    take_profit_3: float | None = None
    #: Why ``expires_at`` was brought forward (e.g. "the rollover block"); empty
    #: when the order simply lives for its configured number of bars.
    expiry_note: str = ""

    def __post_init__(self) -> None:
        numbers = [self.entry, self.stop_loss, self.take_profit_1]
        numbers += [t for t in (self.take_profit_2, self.take_profit_3) if t is not None]
        if not all(isinstance(n, int | float) and math.isfinite(n) for n in numbers):
            raise InvalidPlan(f"{self.id}: every order needs a finite entry, stop-loss and target")
        sign = self.direction.sign
        if (self.entry - self.stop_loss) * sign <= 0:
            raise InvalidPlan(f"{self.id}: the stop-loss is not on the losing side of the entry")
        if (self.take_profit_1 - self.entry) * sign <= 0:
            raise InvalidPlan(f"{self.id}: TP1 is not on the winning side of the entry")
        if self.take_profit_2 is not None and (self.take_profit_2 - self.entry) * sign <= 0:
            raise InvalidPlan(f"{self.id}: TP2 is not on the winning side of the entry")
        if self.take_profit_3 is not None and (self.take_profit_3 - self.entry) * sign <= 0:
            raise InvalidPlan(f"{self.id}: TP3 is not on the winning side of the entry")

    @property
    def targets(self) -> tuple[float | None, float | None, float | None]:
        return (self.take_profit_1, self.take_profit_2, self.take_profit_3)

    @property
    def risk_distance(self) -> float:
        """Entry to stop, in price: one R."""
        return abs(self.entry - self.stop_loss)

    @property
    def withdrawal_detail(self) -> str:
        """What to say when the entry order is removed at ``expires_at``."""
        if self.expiry_note:
            return f"withdrawn before {self.expiry_note}"
        return "the entry was not reached"

    @property
    def expiry_outcome(self) -> Outcome:
        return Outcome.WITHDRAWN if self.expiry_note else Outcome.EXPIRED

    def with_exit_targets(self, risk: RiskConfig) -> TradePlan:
        """This plan with the targets ``risk.target_mode`` asks for (D84).

        ``pools`` keeps the liquidity targets the plan was made with; ``fixed_r`` puts
        TP1..TP3 at ``risk.fixed_targets_r`` R from the entry instead.
        """
        if risk.target_mode == "pools":
            return self
        sign = self.direction.sign
        r = self.risk_distance
        levels: list[float | None] = [self.entry + sign * k * r for k in risk.fixed_targets_r]
        levels += [None] * (3 - len(levels))
        first = levels[0]
        assert first is not None
        return dataclasses.replace(
            self,
            take_profit_1=first,
            take_profit_2=levels[1],
            take_profit_3=levels[2],
            risk_reward_1=risk.fixed_targets_r[0],
        )

    def with_deadline(
        self,
        *,
        sessions: SessionCalculator,
        events: Sequence[NewsEvent] = (),
        blackout_before_min: int = 0,
    ) -> TradePlan:
        """This plan with its expiry cut by :func:`entry_deadline` (unchanged when not cut)."""
        deadline, note = entry_deadline(
            self.time,
            self.expires_at,
            sessions=sessions,
            events=events,
            blackout_before_min=blackout_before_min,
        )
        if not note:
            return self
        return dataclasses.replace(self, expires_at=deadline, expiry_note=note)

    @classmethod
    def from_signal(cls, signal: Any, *, tp3_r: float = 0.0) -> TradePlan:
        """The plan for ``signal``; TP3 sits ``tp3_r`` R out when that is beyond TP1/TP2."""
        return cls(
            id=signal.id,
            time=signal.time,
            direction=signal.direction,
            session=signal.session,
            entry_mode=signal.entry_mode,
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            expires_at=signal.expires_at,
            score=signal.score,
            risk_reward_1=signal.risk_reward_1,
            take_profit_3=third_target(
                signal.direction,
                signal.entry,
                signal.stop_loss,
                (signal.take_profit_1, signal.take_profit_2),
                tp3_r,
            ),
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "time": self.time.isoformat(),
            "direction": self.direction.value,
            "session": self.session,
            "entry_mode": self.entry_mode,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "expires_at": self.expires_at.isoformat(),
            "score": self.score,
            "risk_reward_1": self.risk_reward_1,
            "take_profit_3": self.take_profit_3,
            "expiry_note": self.expiry_note,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> TradePlan:
        values = dict(data)
        values["time"] = dt.datetime.fromisoformat(values["time"])
        values["expires_at"] = dt.datetime.fromisoformat(values["expires_at"])
        values["direction"] = Direction(values["direction"])
        return cls(**values)


# --------------------------------------------------------------------------- #
# The management rules, shared by the book and the live executor
# --------------------------------------------------------------------------- #


def entry_deadline(
    placed: dt.datetime,
    expires_at: dt.datetime,
    *,
    sessions: SessionCalculator,
    events: Sequence[NewsEvent] = (),
    blackout_before_min: int = 0,
) -> tuple[dt.datetime, str]:
    """When a waiting entry order must be gone, and why ("" when it is its own expiry).

    The earliest of: the order's own expiry, the start of any known high-impact
    news blackout, and the first minute the clock forbids new trades (rollover
    block, Friday cutoff, weekend). Leaving a kill zone does not count: the setup
    formed inside it. Every window is defined in whole minutes, so the clock is
    checked minute by minute.
    """
    start, deadline = ensure_utc(placed), ensure_utc(expires_at)
    note = ""
    before = dt.timedelta(minutes=blackout_before_min)
    for event in events:
        if event.impact != HIGH_IMPACT:
            continue
        begins = ensure_utc(event.time) - before
        if start <= begins < deadline:
            deadline, note = begins, f"the news blackout for {event.title}"
    moment = start.replace(second=0, microsecond=0)
    while moment < deadline:
        status = sessions.status(moment)
        if status.blocked:
            label = status.reasons[0].split(":")[0]
            return max(moment, start), _BLOCK_NAMES.get(label, label)
        moment += _MINUTE
    return deadline, note


def refined_plan(setup: RefineSetup, result: RefineResult, *, tp3_r: float = 0.0) -> TradePlan:
    """The order for an M5 setup refined on M1 (D83): the setup's targets, the
    refined entry and stop, placed when the M1 turn closed."""
    if result.time is None or result.entry is None or result.stop_loss is None:
        raise InvalidPlan(f"{setup.id}: the refinement has not produced an entry")
    sign = setup.direction.sign
    risk = (result.entry - result.stop_loss) * sign
    return TradePlan(
        id=setup.id,
        time=result.time,
        direction=setup.direction,
        session=setup.session,
        entry_mode=result.entry_mode,
        entry=result.entry,
        stop_loss=result.stop_loss,
        take_profit_1=setup.take_profit_1,
        take_profit_2=setup.take_profit_2,
        expires_at=result.order_expires_at or setup.expires_at,
        score=setup.score,
        risk_reward_1=(setup.take_profit_1 - result.entry) * sign / risk if risk > 0 else 0.0,
        take_profit_3=third_target(
            setup.direction,
            result.entry,
            result.stop_loss,
            (setup.take_profit_1, setup.take_profit_2),
            tp3_r,
        ),
    )


def third_target(
    direction: Direction,
    entry: float,
    stop: float,
    targets: Sequence[float | None],
    r_multiple: float,
) -> float | None:
    """TP3 ``r_multiple`` R from the entry, or ``None`` when it is not beyond the other targets."""
    if r_multiple <= 0:
        return None
    sign = direction.sign
    level = entry + sign * r_multiple * abs(entry - stop)
    farthest = max((t - entry) * sign for t in targets if t is not None)
    if (level - entry) * sign <= farthest + _EPSILON:
        return None
    return level


@dataclass(frozen=True, slots=True)
class LegPlan:
    """One part of a position: its size and its own target."""

    lots: float
    target: float
    reason: ExitReason


def plan_legs(plan: TradePlan, lots: float, spec: SymbolSpec, risk: RiskConfig) -> list[LegPlan]:
    """Split ``lots`` over the plan's targets (spec 7b: 50 / 30 / 20 by default).

    A missing target gives its share to the nearest earlier one, so without a
    TP3 the rest goes to TP2 and without a TP2 to TP1. Every part but the last is
    rounded down to the volume step; the last takes the remainder. When a part
    would fall below the broker's minimum volume, the farthest part is folded
    into the one before it until every part can be traded.
    """
    reasons = (ExitReason.TP1, ExitReason.TP2, ExitReason.TP3)
    first = risk.partial_close_at_tp1_pct
    second = min(risk.partial_close_at_tp2_pct, 100.0 - first)
    shares = [first, second, max(0.0, 100.0 - first - second)]
    present = [t is not None for t in plan.targets]
    for k in (2, 1):
        if not present[k]:
            earlier = max(i for i in range(k) if present[i])
            shares[earlier] += shares[k]
            shares[k] = 0.0
    chosen = [
        (share, target, reasons[i])
        for i, (share, target) in enumerate(zip(shares, plan.targets, strict=True))
        if target is not None and share > 0
    ]
    step = spec.volume_step
    parts: list[float] = []
    for i, (share, _target, _reason) in enumerate(chosen):
        if i == len(chosen) - 1:
            parts.append(round(lots - sum(parts), 8))
        else:
            parts.append(round(math.floor(lots * share / 100.0 / step + _EPSILON) * step, 8))
    while len(parts) > 1 and min(parts) < spec.volume_min - _EPSILON:
        extra = parts.pop()
        chosen.pop()
        parts[-1] = round(parts[-1] + extra, 8)
    return [
        LegPlan(part, float(target), reason)
        for part, (_share, target, reason) in zip(parts, chosen, strict=True)
    ]


def breakeven_due(
    *,
    long: bool,
    fill_price: float,
    initial_stop: float,
    best_price: float,
    tp1_done: bool,
    at_r: float,
) -> bool:
    """True once TP1 has been hit, or price has gone ``at_r`` R in favour (that part
    never applies when ``at_r`` is 0)."""
    if tp1_done:
        return True
    if at_r <= 0:
        return False
    sign = 1.0 if long else -1.0
    r = abs(fill_price - initial_stop)
    return (best_price - fill_price) * sign >= at_r * r - _EPSILON


def trailed_stop(
    *,
    long: bool,
    stop: float,
    swing_low: float,
    swing_high: float,
    close: float,
    spread: float,
) -> float | None:
    """The stop behind the latest confirmed swing, when that tightens it (else ``None``).

    A long's stop goes under the last swing low; a short's above the last swing
    high plus the spread (a short is stopped on the ask). The new stop must stay
    on the losing side of the current price.
    """
    if long:
        if not math.isnan(swing_low) and stop < swing_low < close:
            return swing_low
        return None
    if math.isnan(swing_high):
        return None
    level = swing_high + spread
    if close + spread < level < stop:
        return level
    return None


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


class ExitReason(StrEnum):
    TP1 = "tp1"
    TP2 = "tp2"
    TP3 = "tp3"
    STOP = "stop"
    BREAKEVEN = "breakeven"
    TRAIL = "trail"
    END_OF_DATA = "end_of_data"
    MANUAL = "manual"  # closed by the kill switch (/stop)


class Outcome(StrEnum):
    TRADED = "traded"
    NOT_SELECTED = "not_selected"  # below the score threshold or the R:R filter
    RISK_BLOCKED = "risk_blocked"
    NO_SIZE = "no_size"
    EXPIRED = "expired"  # the entry order was not filled in time
    TP1_FIRST = "tp1_first"  # price reached TP1 before the entry filled
    CANCELLED = "cancelled"  # withdrawn (kill switch)
    #: withdrawn unfilled before a news blackout, the rollover block, the Friday
    #: cutoff or the weekend (see :func:`entry_deadline`)
    WITHDRAWN = "withdrawn"
    #: M1 refinement (D83): the turn inside the gap never came, or the setup failed
    NO_TRIGGER = "no_trigger"
    REFUSED = "refused"  # the broker refused the order (live trading)


@dataclass(frozen=True, slots=True)
class Leg:
    """One closed part of a position."""

    lots: float
    exit_price: float
    exit_time: dt.datetime
    reason: ExitReason
    pnl: float  # after commission
    commission: float


@dataclass(frozen=True, slots=True)
class Trade:
    """A filled plan, from entry to the last exit."""

    plan: TradePlan
    payload: Any  # what the caller attached (the backtester's SignalRecord), or None
    fill_time: dt.datetime
    fill_price: float
    stop_loss: float  # initial stop
    take_profit_1: float
    take_profit_2: float | None
    lots: float
    risk_amount: float  # account currency at risk when the entry filled
    legs: tuple[Leg, ...]
    take_profit_3: float | None = None

    @property
    def record(self) -> Any:
        return self.payload

    @property
    def signal(self) -> Any:
        """The full signal, when the caller attached one (backtests do)."""
        return getattr(self.payload, "signal", None)

    @property
    def direction(self) -> Direction:
        return self.plan.direction

    @property
    def exit_time(self) -> dt.datetime:
        return self.legs[-1].exit_time

    @property
    def exit_reason(self) -> ExitReason:
        return self.legs[-1].reason

    @property
    def net_pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs)

    @property
    def commission(self) -> float:
        return sum(leg.commission for leg in self.legs)

    @property
    def r_multiple(self) -> float:
        """Net result in units of the amount risked."""
        return self.net_pnl / self.risk_amount if self.risk_amount > 0 else 0.0


class EventKind(StrEnum):
    PLACED = "placed"  # an entry order is waiting
    REJECTED = "rejected"  # risk rules or sizing refused the plan
    FILLED = "filled"
    EXPIRED = "expired"
    TP1_FIRST = "tp1_first"
    CANCELLED = "cancelled"
    LEG_CLOSED = "leg_closed"  # part of a position closed (TP1 while the rest runs)
    STOP_MOVED = "stop_moved"  # breakeven or trailing
    CLOSED = "closed"  # the whole position is flat


@dataclass(frozen=True, slots=True)
class BookEvent:
    kind: EventKind
    time: dt.datetime
    plan: TradePlan
    payload: Any = None
    outcome: Outcome | None = None
    detail: str = ""
    price: float | None = None
    lots: float | None = None
    leg: Leg | None = None
    trade: Trade | None = None
    equity: float | None = None  # realised equity right after a position closed
    note: str = ""  # extra context for messages (e.g. "before US CPI")


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #


@dataclass
class _Order:
    plan: TradePlan
    payload: Any
    lots: float
    risk_amount: float

    @property
    def market(self) -> bool:
        return self.plan.entry_mode == "market"


@dataclass
class _OpenLeg:
    lots: float
    target: float | None
    reason: ExitReason


@dataclass
class _Position:
    plan: TradePlan
    payload: Any
    fill_time: dt.datetime
    fill_price: float
    initial_stop: float
    stop: float
    stop_reason: ExitReason
    lots: float
    risk_amount: float
    legs: list[_OpenLeg]
    closed: list[Leg] = field(default_factory=list)
    tp1_done: bool = False
    breakeven_done: bool = False

    @property
    def long(self) -> bool:
        return self.plan.direction is Direction.LONG


class TradeBook:
    """Holds entry orders and positions, and applies each new bar to them."""

    def __init__(
        self,
        spec: SymbolSpec,
        risk: RiskConfig,
        news: NewsConfig,
        boundary: DayBoundaryConfig,
        *,
        equity: float,
        slippage_points: float,
        commission_per_lot: float,
        trail_after_tp1: bool,
    ) -> None:
        self._spec = spec
        self._risk_cfg = risk
        self._trail_after_tp1 = trail_after_tp1
        self._slippage = slippage_points * spec.point
        self._commission = commission_per_lot
        self._value = spec.value_per_price_unit
        self.manager = RiskManager(risk, news, boundary, equity=equity)
        self._orders: list[_Order] = []
        self._positions: list[_Position] = []

    # -- what is open -------------------------------------------------------- #

    @property
    def idle(self) -> bool:
        return not self._orders and not self._positions

    @property
    def orders(self) -> list[TradePlan]:
        return [order.plan for order in self._orders]

    @property
    def positions(self) -> list[dict[str, Any]]:
        """Open positions, summarised for status messages."""
        return [
            {
                "id": p.plan.id,
                "direction": p.plan.direction.value,
                "lots": sum(leg.lots for leg in p.legs),
                "fill_price": p.fill_price,
                "stop": p.stop,
                "targets": [leg.target for leg in p.legs],
                "risk_amount": p.risk_amount,
            }
            for p in self._positions
        ]

    def holds(self, plan_id: str) -> bool:
        return any(o.plan.id == plan_id for o in self._orders) or any(
            p.plan.id == plan_id for p in self._positions
        )

    # -- new plans ----------------------------------------------------------- #

    def submit(
        self,
        plan: TradePlan,
        payload: Any = None,
        *,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> BookEvent:
        """Risk-check and size a plan; on success an entry order waits for the next bar."""
        manager = self.manager
        decision = manager.check(
            plan.time,
            session=plan.session,
            spread_points=spread_points,
            median_spread_points=median_spread_points,
            events=events,
        )
        if not decision.allowed:
            return BookEvent(
                EventKind.REJECTED,
                plan.time,
                plan,
                payload,
                Outcome.RISK_BLOCKED,
                "; ".join(decision.reasons),
            )
        size = manager.size(plan.entry, plan.stop_loss, self._spec)
        if not size.ok:
            return BookEvent(
                EventKind.REJECTED, plan.time, plan, payload, Outcome.NO_SIZE, size.reason or ""
            )
        manager.order_placed()
        self._orders.append(_Order(plan, payload, size.lots, size.risk_amount))
        return BookEvent(
            EventKind.PLACED,
            plan.time,
            plan,
            payload,
            None,
            f"{size.lots:g} lots, {size.risk_amount:.2f} at risk",
            plan.entry,
            size.lots,
        )

    # -- each bar ------------------------------------------------------------ #

    def on_bar(self, bar: Bar, trail: TrailSource | None = None) -> list[BookEvent]:
        """Apply one closed bar: fills, expiries, stops, targets, stop moves."""
        events: list[BookEvent] = []
        self._fill_orders(bar, events)
        self._manage(bar, trail, events)
        return events

    def _fill_orders(self, bar: Bar, events: list[BookEvent]) -> None:
        for order in list(self._orders):
            plan = order.plan
            if bar.open_ns < to_ns(plan.time):
                continue  # the order exists from the next bar on
            long = plan.direction is Direction.LONG
            fill: float | None = None
            if order.market:
                fill = bar.open + bar.spread + self._slippage if long else bar.open - self._slippage
            elif bar.open_ns >= to_ns(plan.expires_at):
                self._drop(
                    order,
                    events,
                    EventKind.EXPIRED,
                    plan.expiry_outcome,
                    plan.withdrawal_detail,
                    bar.open_time,
                )
                continue
            elif long and bar.low + bar.spread <= plan.entry + _EPSILON:
                fill = plan.entry
            elif not long and bar.high >= plan.entry - _EPSILON:
                fill = plan.entry
            if fill is None:
                reached_tp1 = (
                    bar.high >= plan.take_profit_1 - _EPSILON
                    if long
                    else bar.low + bar.spread <= plan.take_profit_1 + _EPSILON
                )
                if reached_tp1:
                    self._drop(
                        order,
                        events,
                        EventKind.TP1_FIRST,
                        Outcome.TP1_FIRST,
                        "price reached TP1 before the entry",
                        bar.close_time,
                    )
                continue
            self._orders.remove(order)
            position = self._open_position(order, float(fill), bar)
            self.manager.position_opened(position.fill_time, plan.session)
            self._positions.append(position)
            events.append(
                BookEvent(
                    EventKind.FILLED,
                    position.fill_time,
                    plan,
                    order.payload,
                    Outcome.TRADED,
                    "",
                    position.fill_price,
                    position.lots,
                )
            )
            # The fill bar can stop the trade out, but cannot reach its targets.
            self._check_stop(position, bar, events)

    def _drop(
        self,
        order: _Order,
        events: list[BookEvent],
        kind: EventKind,
        outcome: Outcome,
        detail: str,
        when: dt.datetime,
    ) -> None:
        self._orders.remove(order)
        self.manager.order_cancelled()
        events.append(BookEvent(kind, when, order.plan, order.payload, outcome, detail))

    def _open_position(self, order: _Order, fill: float, bar: Bar) -> _Position:
        plan = order.plan
        lots = order.lots
        risk_amount = abs(fill - plan.stop_loss) * lots * self._value
        legs = [
            _OpenLeg(leg.lots, leg.target, leg.reason)
            for leg in plan_legs(plan, lots, self._spec, self._risk_cfg)
        ]
        return _Position(
            plan=plan,
            payload=order.payload,
            fill_time=bar.open_time,
            fill_price=fill,
            initial_stop=plan.stop_loss,
            stop=plan.stop_loss,
            stop_reason=ExitReason.STOP,
            lots=lots,
            risk_amount=risk_amount,
            legs=legs,
        )

    def _manage(self, bar: Bar, trail: TrailSource | None, events: list[BookEvent]) -> None:
        for position in list(self._positions):
            if position.fill_time == bar.open_time:
                continue  # filled on this bar: already checked by _fill_orders
            if self._check_stop(position, bar, events):
                continue
            for leg in list(position.legs):
                if leg.target is None:
                    continue
                reached = (
                    bar.high >= leg.target - _EPSILON
                    if position.long
                    else bar.low + bar.spread <= leg.target + _EPSILON
                )
                if reached:
                    self._close_leg(position, leg, leg.target, bar.close_time, leg.reason, events)
                    if leg.reason is ExitReason.TP1:
                        position.tp1_done = True
            if not position.legs:
                self._finish(position, events)
                continue
            self._move_stop(position, bar, trail, events)

    def _check_stop(self, position: _Position, bar: Bar, events: list[BookEvent]) -> bool:
        """Close the whole position if this bar reached its stop. True when it did."""
        if position.long:
            if bar.open <= position.stop + _EPSILON:
                price = bar.open - self._slippage  # gapped through the stop
            elif bar.low <= position.stop + _EPSILON:
                price = position.stop - self._slippage
            else:
                return False
        else:
            if bar.open + bar.spread >= position.stop - _EPSILON:
                price = bar.open + bar.spread + self._slippage
            elif bar.high + bar.spread >= position.stop - _EPSILON:
                price = position.stop + self._slippage
            else:
                return False
        self._close_all(position, float(price), bar.close_time, position.stop_reason, events)
        return True

    def _move_stop(
        self, position: _Position, bar: Bar, trail: TrailSource | None, events: list[BookEvent]
    ) -> None:
        """Breakeven and trailing, effective from the next bar."""
        long = position.long
        sign = 1.0 if long else -1.0
        best = bar.high if long else bar.low + bar.spread  # the most favourable price reached
        before = position.stop
        if not position.breakeven_done and breakeven_due(
            long=long,
            fill_price=position.fill_price,
            initial_stop=position.initial_stop,
            best_price=best,
            tp1_done=position.tp1_done,
            at_r=self._risk_cfg.breakeven_at_r,
        ):
            position.breakeven_done = True
            if (position.fill_price - position.stop) * sign > 0:
                position.stop = position.fill_price
                position.stop_reason = ExitReason.BREAKEVEN
        if position.tp1_done and self._trail_after_tp1 and trail is not None:
            swing_low, swing_high = trail.at(bar.close_ns)
            level = trailed_stop(
                long=long,
                stop=position.stop,
                swing_low=swing_low,
                swing_high=swing_high,
                close=bar.close,
                spread=bar.spread,
            )
            if level is not None:
                position.stop, position.stop_reason = level, ExitReason.TRAIL
        if position.stop != before:
            events.append(
                BookEvent(
                    EventKind.STOP_MOVED,
                    bar.close_time,
                    position.plan,
                    position.payload,
                    None,
                    position.stop_reason.value,
                    position.stop,
                )
            )

    # -- closing -------------------------------------------------------------- #

    def _close_leg(
        self,
        position: _Position,
        leg: _OpenLeg,
        price: float,
        when: dt.datetime,
        reason: ExitReason,
        events: list[BookEvent],
    ) -> None:
        sign = 1.0 if position.long else -1.0
        commission = self._commission * leg.lots
        pnl = (price - position.fill_price) * sign * leg.lots * self._value - commission
        closed = Leg(leg.lots, price, when, reason, pnl, commission)
        position.closed.append(closed)
        position.legs.remove(leg)
        self.manager.book(when, pnl)
        if position.legs:  # part of the position is still open
            events.append(
                BookEvent(
                    EventKind.LEG_CLOSED,
                    when,
                    position.plan,
                    position.payload,
                    None,
                    reason.value,
                    price,
                    leg.lots,
                    closed,
                )
            )

    def _close_all(
        self,
        position: _Position,
        price: float,
        when: dt.datetime,
        reason: ExitReason,
        events: list[BookEvent],
    ) -> None:
        for leg in list(position.legs):
            self._close_leg(position, leg, price, when, reason, events)
        self._finish(position, events)

    def _finish(self, position: _Position, events: list[BookEvent]) -> None:
        self._positions.remove(position)
        trade = Trade(
            plan=position.plan,
            payload=position.payload,
            fill_time=position.fill_time,
            fill_price=position.fill_price,
            stop_loss=position.initial_stop,
            take_profit_1=position.plan.take_profit_1,
            take_profit_2=position.plan.take_profit_2,
            lots=position.lots,
            risk_amount=position.risk_amount,
            legs=tuple(position.closed),
            take_profit_3=position.plan.take_profit_3,
        )
        self.manager.position_closed(trade.exit_time, trade.net_pnl)
        events.append(
            BookEvent(
                EventKind.CLOSED,
                trade.exit_time,
                position.plan,
                position.payload,
                None,
                trade.exit_reason.value,
                trade.legs[-1].exit_price,
                trade.lots,
                trade.legs[-1],
                trade,
                self.manager.equity,
            )
        )

    def close_everything(
        self,
        when: dt.datetime,
        bid: float,
        ask: float,
        reason: ExitReason,
        detail: str,
        *,
        order_outcome: Outcome = Outcome.CANCELLED,
    ) -> list[BookEvent]:
        """Cancel every entry order and close every position at the given quote."""
        events: list[BookEvent] = []
        instant = ensure_utc(when)
        for order in list(self._orders):
            self._drop(order, events, EventKind.CANCELLED, order_outcome, detail, instant)
        for position in list(self._positions):
            price = bid if position.long else ask
            self._close_all(position, price, instant, reason, events)
        return events

    def withdraw_orders(self, when: dt.datetime, detail: str) -> list[BookEvent]:
        """Remove every waiting entry order (a release appeared after it was placed).

        Open positions are not touched.
        """
        events: list[BookEvent] = []
        instant = ensure_utc(when)
        for order in list(self._orders):
            if order.market:
                continue  # fills at the next open, before anything else can happen
            self._drop(order, events, EventKind.EXPIRED, Outcome.WITHDRAWN, detail, instant)
        return events

    def protect_before_news(
        self, when: dt.datetime, bid: float, ask: float, min_r: float, detail: str
    ) -> list[BookEvent]:
        """Spec 5.6: before a release, move a stop to breakeven when the trade is ``min_r`` up.

        Applies to positions whose stop is still on the losing side of the entry
        and that are at least ``min_r`` R in profit at the given quote.
        """
        events: list[BookEvent] = []
        instant = ensure_utc(when)
        for position in self._positions:
            sign = 1.0 if position.long else -1.0
            price = bid if position.long else ask
            r = abs(position.fill_price - position.initial_stop)
            if (position.fill_price - position.stop) * sign <= 0 or r <= 0:
                continue
            if (price - position.fill_price) * sign < min_r * r - _EPSILON:
                continue
            position.stop = position.fill_price
            position.stop_reason = ExitReason.BREAKEVEN
            position.breakeven_done = True
            events.append(
                BookEvent(
                    EventKind.STOP_MOVED,
                    instant,
                    position.plan,
                    position.payload,
                    None,
                    ExitReason.BREAKEVEN.value,
                    position.stop,
                    note=detail,
                )
            )
        return events

    # -- saving and restoring (paper trading survives restarts) ---------------- #

    def to_state(self) -> dict[str, Any]:
        return {
            "risk": self.manager.to_state(),
            "orders": [
                {"plan": o.plan.to_state(), "lots": o.lots, "risk_amount": o.risk_amount}
                for o in self._orders
            ],
            "positions": [
                {
                    "plan": p.plan.to_state(),
                    "fill_time": p.fill_time.isoformat(),
                    "fill_price": p.fill_price,
                    "initial_stop": p.initial_stop,
                    "stop": p.stop,
                    "stop_reason": p.stop_reason.value,
                    "lots": p.lots,
                    "risk_amount": p.risk_amount,
                    "legs": [[leg.lots, leg.target, leg.reason.value] for leg in p.legs],
                    "closed": [
                        [
                            c.lots,
                            c.exit_price,
                            c.exit_time.isoformat(),
                            c.reason.value,
                            c.pnl,
                            c.commission,
                        ]
                        for c in p.closed
                    ],
                    "tp1_done": p.tp1_done,
                    "breakeven_done": p.breakeven_done,
                }
                for p in self._positions
            ],
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.manager.restore(state["risk"])
        self._orders = [
            _Order(TradePlan.from_state(o["plan"]), None, o["lots"], o["risk_amount"])
            for o in state.get("orders", [])
        ]
        self._positions = [
            _Position(
                plan=TradePlan.from_state(p["plan"]),
                payload=None,
                fill_time=dt.datetime.fromisoformat(p["fill_time"]),
                fill_price=p["fill_price"],
                initial_stop=p["initial_stop"],
                stop=p["stop"],
                stop_reason=ExitReason(p["stop_reason"]),
                lots=p["lots"],
                risk_amount=p["risk_amount"],
                legs=[
                    _OpenLeg(lots, target, ExitReason(reason)) for lots, target, reason in p["legs"]
                ],
                closed=[
                    Leg(c[0], c[1], dt.datetime.fromisoformat(c[2]), ExitReason(c[3]), c[4], c[5])
                    for c in p["closed"]
                ],
                tp1_done=p["tp1_done"],
                breakeven_done=p["breakeven_done"],
            )
            for p in state.get("positions", [])
        ]

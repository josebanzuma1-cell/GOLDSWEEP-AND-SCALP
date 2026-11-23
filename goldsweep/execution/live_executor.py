"""Live execution on MetaTrader 5 (spec sections 7 and 7b, Phase 6: demo account).

:class:`MT5Executor` turns a :class:`~goldsweep.execution.book.TradePlan` into
real orders and manages them with the rules the backtester uses:

* **The split** (7b.3): up to three positions with the same entry and stop and
  their own targets (:func:`~goldsweep.execution.book.plan_legs`). Every order
  carries its stop-loss and take-profit when it is sent; the broker closes each
  part at its own target or at the stop, even while GoldSweep is not running.
* **Risk first**: the risk manager's limits and sizing (on the broker's balance),
  then ``max_total_lots``, then a margin check (no order that takes the margin
  level below ``min_margin_level_pct``). Total risk over the parts never exceeds
  ``risk_per_trade_pct``: market orders are sized from the price they will get.
* **Management**: TP1 hit, or +``breakeven_at_r`` R reached on a closed bar,
  moves the other parts' stops to breakeven; after TP1 the stops can trail
  behind confirmed M5 swings. A limit entry is withdrawn when it expires or when
  price reaches TP1 first. The wanted stop is kept in the saved state and pushed
  to the broker until it is accepted.
* **Reconciliation**: the state is saved in SQLite after every change. At
  startup, positions and orders are matched with the broker again; what
  happened while the bot was off (fills, stops, targets) is read from the deal
  history and reported. Positions with GoldSweep's magic number that are not in
  the saved state keep their own stop and target and are reported, never
  silently managed; one without a stop or target is closed for safety, and
  unknown pending orders are cancelled.

The broker is the record of what happened: fills, exits and results come from
its deals, never from simulated prices.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import Clock, SystemClock, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import RiskConfig, Settings
from goldsweep.core.types import Mode
from goldsweep.data.broker import BrokerError
from goldsweep.execution.book import (
    Bar,
    BookEvent,
    EventKind,
    ExitReason,
    Leg,
    Management,
    Outcome,
    StepLineSource,
    Trade,
    TradePlan,
    TrailSource,
    breakeven_due,
    breakeven_plus_stop,
    close_by_due,
    ladder_stop,
    plan_legs,
    previous_target,
    step_trailed_stop,
    target_number,
    time_stop_due,
    to_ns,
    trailed_stop,
)
from goldsweep.execution.executor import (
    LIVE_TABLE,
    Executor,
    TradeRow,
    store_trade,
    trades_between,
)
from goldsweep.execution.mt5_gateway import (
    DEAL_REASON_SL,
    DEAL_REASON_SO,
    DEAL_REASON_TP,
    BrokerPosition,
    MT5Gateway,
    OrderResult,
)
from goldsweep.risk.risk_manager import NewsEvent, RiskManager
from goldsweep.storage.db import Database

log = get_logger(__name__)

STATE_KEY = "live.book"
PENDING, OPEN = "pending", "open"
#: How long a vanished position may wait for its deals to appear in the history.
HISTORY_GRACE = dt.timedelta(seconds=60)
_EPSILON = 1e-9


def _iso(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _time(text: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(text) if text else None


@dataclass
class _Part:
    """One of the positions a trade is split into."""

    index: int
    lots: float
    target: float
    reason: ExitReason
    order: int  # the ticket of the order that opens it (the position's identifier)
    position: int = 0  # the position's ticket once it is open
    open_price: float | None = None
    open_time: dt.datetime | None = None
    broker_sl: float = 0.0
    last_profit: float = 0.0
    missing_since: dt.datetime | None = None
    closed: Leg | None = None
    cancelled: bool = False

    @property
    def done(self) -> bool:
        return self.closed is not None or self.cancelled

    @property
    def live(self) -> bool:
        return bool(self.position) and not self.done

    def to_state(self) -> dict[str, Any]:
        leg = self.closed
        return {
            "index": self.index,
            "lots": self.lots,
            "target": self.target,
            "reason": self.reason.value,
            "order": self.order,
            "position": self.position,
            "open_price": self.open_price,
            "open_time": _iso(self.open_time),
            "broker_sl": self.broker_sl,
            "last_profit": self.last_profit,
            "missing_since": _iso(self.missing_since),
            "closed": None
            if leg is None
            else [
                leg.lots,
                leg.exit_price,
                leg.exit_time.isoformat(),
                leg.reason.value,
                leg.pnl,
                leg.commission,
            ],
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> _Part:
        closed = data.get("closed")
        return cls(
            index=data["index"],
            lots=data["lots"],
            target=data["target"],
            reason=ExitReason(data["reason"]),
            order=data["order"],
            position=data["position"],
            open_price=data["open_price"],
            open_time=_time(data["open_time"]),
            broker_sl=data["broker_sl"],
            last_profit=data["last_profit"],
            missing_since=_time(data["missing_since"]),
            closed=None
            if closed is None
            else Leg(
                closed[0],
                closed[1],
                dt.datetime.fromisoformat(closed[2]),
                ExitReason(closed[3]),
                closed[4],
                closed[5],
            ),
            cancelled=data["cancelled"],
        )


@dataclass
class _Group:
    """All the parts of one signal's trade."""

    plan: TradePlan
    lots: float
    risk_amount: float
    placed_at: dt.datetime
    stop: float
    parts: list[_Part] = field(default_factory=list)
    status: str = PENDING
    limit: bool = False
    fill_time: dt.datetime | None = None
    fill_price: float | None = None
    stop_reason: ExitReason = ExitReason.STOP
    tp1_done: bool = False
    breakeven_done: bool = False
    # The scalper's management (D111): TP2 reached, the time stop's one look, the
    # breakeven+ offset applied to each part's own fill, and why GoldSweep closed it.
    tp2_done: bool = False
    time_checked: bool = False
    stop_offset: float = 0.0
    close_reason: ExitReason | None = None
    reached: int = 0  # the highest target reached (1..5; D115)

    def reach(self, reason: ExitReason) -> None:
        """Note a take-profit exit (the ladder rule follows the highest one)."""
        self.reached = max(self.reached, target_number(reason))
        self.tp1_done = self.tp1_done or self.reached >= 1
        self.tp2_done = self.tp2_done or self.reached >= 2

    @property
    def long(self) -> bool:
        return self.plan.direction is Direction.LONG

    @property
    def sign(self) -> float:
        return 1.0 if self.long else -1.0

    @property
    def finished(self) -> bool:
        return bool(self.parts) and all(part.done for part in self.parts)

    def to_state(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_state(),
            "lots": self.lots,
            "risk_amount": self.risk_amount,
            "placed_at": self.placed_at.isoformat(),
            "stop": self.stop,
            "parts": [part.to_state() for part in self.parts],
            "status": self.status,
            "limit": self.limit,
            "fill_time": _iso(self.fill_time),
            "fill_price": self.fill_price,
            "stop_reason": self.stop_reason.value,
            "tp1_done": self.tp1_done,
            "breakeven_done": self.breakeven_done,
            "tp2_done": self.tp2_done,
            "time_checked": self.time_checked,
            "stop_offset": self.stop_offset,
            "close_reason": self.close_reason.value if self.close_reason else None,
            "reached": self.reached,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> _Group:
        return cls(
            plan=TradePlan.from_state(data["plan"]),
            lots=data["lots"],
            risk_amount=data["risk_amount"],
            placed_at=dt.datetime.fromisoformat(data["placed_at"]),
            stop=data["stop"],
            parts=[_Part.from_state(p) for p in data["parts"]],
            status=data["status"],
            limit=data["limit"],
            fill_time=_time(data["fill_time"]),
            fill_price=data["fill_price"],
            stop_reason=ExitReason(data["stop_reason"]),
            tp1_done=data["tp1_done"],
            breakeven_done=data["breakeven_done"],
            tp2_done=data.get("tp2_done", False),
            time_checked=data.get("time_checked", False),
            stop_offset=data.get("stop_offset", 0.0),
            close_reason=ExitReason(data["close_reason"]) if data.get("close_reason") else None,
            reached=data.get("reached", 2 if data.get("tp2_done") else int(bool(data["tp1_done"]))),
        )


def order_logger(db: Database, clock: Clock) -> Any:
    """An ``OrderLog`` that writes every request and answer to the ``order_log`` table."""

    def write(
        action: str, signal_id: str | None, request: dict[str, Any], result: OrderResult
    ) -> None:
        db.execute(
            "INSERT INTO order_log (time, signal_id, action, ok, retcode, ticket, message, "
            "request, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(ensure_utc(clock.now()).timestamp()),
                signal_id,
                action,
                int(result.ok),
                result.retcode,
                result.ticket or None,
                result.message,
                json.dumps(request, default=str),
                result.elapsed_ms,
            ),
        )

    return write


class MT5Executor(Executor):
    """Real orders on a MetaTrader 5 account, managed like the backtester's trades."""

    mode = Mode.LIVE

    def __init__(
        self,
        settings: Settings,
        gateway: MT5Gateway,
        db: Database,
        *,
        clock: Clock | None = None,
        demo: bool = True,
        armed: bool = True,
        state_key: str = STATE_KEY,
        table: str = LIVE_TABLE,
        risk: RiskConfig | None = None,
        management: Management | None = None,
        comment_prefix: str = "GS",
    ) -> None:
        """``state_key``, ``table``, ``risk`` and ``management`` default to the main
        strategy's; the scalper passes its own, with a gateway on its own magic (D111)."""
        config = settings.config
        self.armed = armed
        self._risk_cfg = risk or config.risk
        self._exec_cfg = config.execution
        self._rules = management
        self._trail_after_tp1 = config.backtest.trail_after_tp1 if management is None else False
        self._state_key = state_key
        self._table = table
        self._comment = comment_prefix
        self._gateway = gateway
        self._spec = gateway.spec
        self._db = db
        self._clock = clock or SystemClock()
        self.demo = demo
        self.label = "LIVE DEMO" if demo else "LIVE"
        account = gateway.account()
        self.login = account.login
        self.manager = RiskManager(
            self._risk_cfg,
            config.news,
            config.strategy.liquidity.day_boundary,
            equity=account.balance,
        )
        self._groups: list[_Group] = []
        self._outbox: list[BookEvent] = []
        self._positions: dict[int, BrokerPosition] = {}
        saved = db.get_state(state_key)
        self.restored = saved is not None
        if saved is not None:
            self.manager.restore(saved["risk"])
            self._groups = [_Group.from_state(g) for g in saved.get("groups", [])]
        self.manager.rebase(account.balance)

    # -- state ------------------------------------------------------------------ #

    def _now(self) -> dt.datetime:
        return ensure_utc(self._clock.now())

    def _save(self) -> None:
        self._db.set_state(
            self._state_key,
            {"risk": self.manager.to_state(), "groups": [g.to_state() for g in self._groups]},
        )

    @property
    def equity(self) -> float:
        try:
            return self._gateway.account().equity
        except BrokerError:
            return self.manager.equity

    @property
    def open_positions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": g.plan.id,
                "direction": g.plan.direction.value,
                "lots": round(sum(p.lots for p in g.parts if p.live), 8),
                "fill_price": g.fill_price or g.plan.entry,
                "stop": g.stop,
                "targets": [p.target for p in g.parts if p.live],
                "risk_amount": g.risk_amount,
                # For the chart panel (S7): the plan as it was placed.
                "stop_loss": g.plan.stop_loss,
                "take_profits": [t for t in g.plan.targets if t is not None],
                "fill_time": _iso(g.fill_time),
            }
            for g in self._groups
            if g.status == OPEN
        ]

    @property
    def pending_orders(self) -> list[TradePlan]:
        return [g.plan for g in self._groups if g.status == PENDING]

    def trades_between(self, start: dt.datetime, end: dt.datetime) -> list[TradeRow]:
        return trades_between(self._db, self._table, start, end)

    # -- new trades ----------------------------------------------------------------- #

    def _rejected(
        self, plan: TradePlan, when: dt.datetime, outcome: Outcome, detail: str
    ) -> BookEvent:
        log.warning("live order not placed", signal=plan.id, outcome=outcome.value, detail=detail)
        return BookEvent(EventKind.REJECTED, when, plan, None, outcome, detail)

    def submit(
        self,
        plan: TradePlan,
        *,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> BookEvent:
        """Check, size and send the orders for ``plan``: one per target, each with SL and TP."""
        if any(g.plan.id == plan.id for g in self._groups):
            raise ValueError(f"{plan.id} is already being traded")
        now = self._now()
        if not self.armed:
            return self._rejected(
                plan,
                now,
                Outcome.RISK_BLOCKED,
                f"live orders are not confirmed yet: send /golive {self.login} on Telegram",
            )
        gateway, spec = self._gateway, self._spec
        self.manager.rebase(gateway.account().balance)
        decision = self.manager.check(
            now,
            session=plan.session,
            spread_points=spread_points,
            median_spread_points=median_spread_points,
            events=events,
        )
        if not decision.allowed:
            return self._rejected(plan, now, Outcome.RISK_BLOCKED, "; ".join(decision.reasons))

        long = plan.direction is Direction.LONG
        sign = 1.0 if long else -1.0
        tick = gateway.tick()
        market_price = tick.ask if long else tick.bid
        notes: list[str] = []
        limit: float | None = None
        if plan.entry_mode != "market":
            if (plan.entry - market_price) * sign >= 0:
                notes.append("price was already at the entry: market order")
            else:
                limit = plan.entry
        entry = limit if limit is not None else market_price
        if (entry - plan.stop_loss) * sign <= 0 or (plan.take_profit_1 - entry) * sign <= 0:
            return self._rejected(
                plan,
                now,
                Outcome.RISK_BLOCKED,
                f"price {market_price:.2f} is no longer between the stop and TP1",
            )

        size = self.manager.size(entry, plan.stop_loss, spec)
        if not size.ok:
            return self._rejected(plan, now, Outcome.NO_SIZE, size.reason or "no size")
        lots = size.lots
        positions, orders = gateway.positions(), gateway.orders()
        open_lots = sum(p.volume for p in positions) + sum(o.volume for o in orders)
        cap = self._exec_cfg.max_total_lots
        step = spec.volume_step
        room = round(math.floor((cap - open_lots) / step + _EPSILON) * step, 8)
        if room < spec.volume_min - _EPSILON:
            return self._rejected(
                plan,
                now,
                Outcome.RISK_BLOCKED,
                f"max_total_lots {cap:g} reached ({open_lots:g} lots already open)",
            )
        if lots > room:
            lots = room
            notes.append(f"cut to {lots:g} lots by max_total_lots")
        level = gateway.margin_level_after(long, lots, market_price)
        floor_pct = self._exec_cfg.min_margin_level_pct
        if level is not None and level < floor_pct:
            return self._rejected(
                plan,
                now,
                Outcome.RISK_BLOCKED,
                f"the margin level would fall to {level:.0f}% (minimum {floor_pct:g}%)",
            )

        split = self._rules.split if self._rules is not None else None
        legs = plan_legs(plan, lots, spec, self._risk_cfg, split)
        group = _Group(
            plan=plan,
            lots=0.0,
            risk_amount=0.0,
            placed_at=now,
            stop=plan.stop_loss,
            limit=limit is not None,
        )
        for index, leg in enumerate(legs, 1):
            result = gateway.open(
                long=long,
                volume=leg.lots,
                stop_loss=plan.stop_loss,
                take_profit=leg.target,
                comment=(
                    f"GS {plan.id} {index}/{len(legs)}"
                    if self._comment == "GS"
                    else f"{self._comment}|{plan.id}|{index}"  # e.g. GS-SC|<id>|1 (D111)
                ),
                signal_id=plan.id,
                limit_price=limit,
            )
            if not result.ok:
                if not group.parts:
                    return self._rejected(
                        plan, now, Outcome.REFUSED, f"the broker refused it: {result.message}"
                    )
                notes.append(f"part {index}/{len(legs)} refused: {result.message}")
                break
            group.parts.append(_Part(index, leg.lots, leg.target, leg.reason, result.ticket))
        group.lots = round(sum(p.lots for p in group.parts), 8)
        group.risk_amount = abs(entry - plan.stop_loss) * group.lots * spec.value_per_price_unit
        self.manager.order_placed()
        self._groups.append(group)
        self._save()
        how = "market" if limit is None else f"limit {limit:.2f}"
        detail = (
            f"{group.lots:g} lots in {len(group.parts)} position(s) ({how}), "
            f"{group.risk_amount:.2f} at risk"
        )
        if notes:
            detail += "; " + "; ".join(notes)
        return BookEvent(EventKind.PLACED, now, plan, None, None, detail, entry, group.lots)

    # -- following the broker ------------------------------------------------------- #

    def sync(self, now: dt.datetime | None = None) -> list[BookEvent]:
        """Read positions, orders and deals; report fills, exits and finished trades."""
        instant = ensure_utc(now or self._now())
        events = list(self._outbox)
        self._outbox.clear()
        if not self._groups:
            return events
        positions = self._gateway.positions()
        pending = {o.ticket for o in self._gateway.orders()}
        self._positions = {p.ticket: p for p in positions}
        by_identifier = {p.identifier: p for p in positions}
        changed = False
        for group in list(self._groups):
            closed_now: list[_Part] = []
            for part in group.parts:
                if part.done:
                    continue
                if not part.position:
                    seen = by_identifier.get(part.order) or self._positions.get(part.order)
                    if seen is not None:
                        part.position, part.open_price = seen.ticket, seen.price_open
                        part.open_time, part.broker_sl = seen.time, seen.sl
                        part.last_profit = seen.profit
                        changed = True
                    elif part.order in pending:
                        continue
                    else:
                        changed |= self._vanished_order(part, instant)
                        if not part.position:
                            continue
                seen = self._positions.get(part.position)
                if seen is not None:
                    part.broker_sl, part.last_profit = seen.sl, seen.profit
                    part.missing_since = None
                    continue
                leg = self._exit_leg(group, part, instant)
                if leg is None:
                    changed = True  # missing_since was noted
                    continue
                part.closed = leg
                self.manager.book(leg.exit_time, leg.pnl)
                group.reach(leg.reason)
                closed_now.append(part)
                changed = True
            events += self._group_events(group, closed_now)
        events += self._apply_rules_after_tp1()
        events += self._enforce_stops()
        if changed or events:
            try:
                self.manager.rebase(self._gateway.account().balance)
            except BrokerError as exc:
                log.warning("balance not read", error=str(exc))
            self._save()
        return events

    def _vanished_order(self, part: _Part, now: dt.datetime) -> bool:
        """An entry order is gone and no position carries its ticket: filled and closed, or
        deleted. The deal history tells which; it may lag a little behind."""
        deals = self._gateway.deals(part.order)
        if deals:
            first = deals[0]
            part.position, part.open_price, part.open_time = part.order, first.price, first.time
            return True
        if part.missing_since is None:
            part.missing_since = now
            return True
        if now - part.missing_since >= HISTORY_GRACE:
            part.cancelled = True
            log.warning("entry order deleted outside GoldSweep", order=part.order)
            return True
        return False

    def _exit_leg(self, group: _Group, part: _Part, now: dt.datetime) -> Leg | None:
        deals = self._gateway.deals(part.position)
        exits = [d for d in deals if d.is_exit]
        if not exits:
            if part.missing_since is None:
                part.missing_since = now
            if now - part.missing_since < HISTORY_GRACE:
                return None
            log.error(
                "position closed but no exit deal found; using the last known result",
                position=part.position,
            )
            price = part.broker_sl or group.plan.stop_loss
            return Leg(part.lots, price, now, ExitReason.STOP, part.last_profit, 0.0)
        volume = sum(d.volume for d in exits) or part.lots
        price = sum(d.price * d.volume for d in exits) / volume
        pnl = sum(d.net for d in deals)
        costs = -sum(d.commission + d.fee for d in deals)
        last = exits[-1]
        return Leg(
            part.lots, price, last.time, self._exit_reason(group, part, last.reason), pnl, costs
        )

    def _exit_reason(self, group: _Group, part: _Part, deal_reason: int) -> ExitReason:
        if deal_reason == DEAL_REASON_TP:
            return part.reason
        if deal_reason in (DEAL_REASON_SL, DEAL_REASON_SO):
            moved = abs(part.broker_sl - group.plan.stop_loss) > self._spec.point / 2
            return group.stop_reason if moved and part.broker_sl else ExitReason.STOP
        if group.close_reason is not None:
            return group.close_reason  # GoldSweep closed it: time stop or flat-by (D111)
        return ExitReason.MANUAL  # the kill switch, or closed by hand in MT5

    def _group_events(self, group: _Group, closed_now: list[_Part]) -> list[BookEvent]:
        events: list[BookEvent] = []
        plan = group.plan
        filled = [p for p in group.parts if p.position and p.open_price is not None]
        if group.status == PENDING and filled:
            lots = sum(p.lots for p in filled)
            group.status = OPEN
            group.fill_time = min(p.open_time or self._now() for p in filled)
            group.fill_price = sum(p.open_price * p.lots for p in filled) / lots  # type: ignore[operator]
            group.risk_amount = (
                abs(group.fill_price - plan.stop_loss)
                * group.lots
                * self._spec.value_per_price_unit
            )
            self.manager.position_opened(group.fill_time, plan.session)
            events.append(
                BookEvent(
                    EventKind.FILLED,
                    group.fill_time,
                    plan,
                    None,
                    Outcome.TRADED,
                    f"{len(filled)} position(s)",
                    group.fill_price,
                    lots,
                )
            )
        if not group.finished:
            for part in closed_now:
                leg = part.closed
                assert leg is not None
                events.append(
                    BookEvent(
                        EventKind.LEG_CLOSED,
                        leg.exit_time,
                        plan,
                        None,
                        None,
                        leg.reason.value,
                        leg.exit_price,
                        leg.lots,
                        leg,
                    )
                )
            return events
        self._groups.remove(group)
        legs = sorted((p.closed for p in group.parts if p.closed), key=lambda leg: leg.exit_time)
        if group.status == PENDING or not legs:
            self.manager.order_cancelled()
            events.append(
                BookEvent(
                    EventKind.CANCELLED,
                    self._now(),
                    plan,
                    None,
                    Outcome.CANCELLED,
                    "entry orders deleted outside GoldSweep",
                )
            )
            return events
        trade = Trade(
            plan=plan,
            payload=None,
            fill_time=group.fill_time or legs[0].exit_time,
            fill_price=group.fill_price or plan.entry,
            stop_loss=plan.stop_loss,
            take_profit_1=plan.take_profit_1,
            take_profit_2=plan.take_profit_2,
            lots=round(sum(leg.lots for leg in legs), 8),
            risk_amount=group.risk_amount,
            legs=tuple(legs),
            take_profit_3=plan.take_profit_3,
        )
        self.manager.position_closed(trade.exit_time, trade.net_pnl)
        store_trade(self._db, self._table, trade)
        events.append(
            BookEvent(
                EventKind.CLOSED,
                trade.exit_time,
                plan,
                None,
                None,
                trade.exit_reason.value,
                trade.legs[-1].exit_price,
                trade.lots,
                trade.legs[-1],
                trade,
                self._balance_or(self.manager.equity),
            )
        )
        return events

    def _balance_or(self, fallback: float) -> float:
        try:
            return self._gateway.account().balance
        except BrokerError:
            return fallback

    # -- the management rules ----------------------------------------------------------- #

    def _to_breakeven(self, group: _Group) -> None:
        group.breakeven_done = True
        assert group.fill_price is not None
        if (group.fill_price - group.stop) * group.sign > 0:
            group.stop = group.fill_price
            group.stop_reason = ExitReason.BREAKEVEN

    def _apply_rules_after_tp1(self) -> list[BookEvent]:
        """TP1 hit: the rest of the trade goes to breakeven at once (spec 7b.3)."""
        for group in self._groups:
            if group.status == OPEN and group.tp1_done and not group.breakeven_done:
                self._to_breakeven(group)
        return []

    def _wanted_stop(self, group: _Group, part: _Part) -> float:
        if group.stop_reason is ExitReason.BREAKEVEN and part.open_price is not None:
            return part.open_price + group.sign * group.stop_offset  # breakeven(+)
        return group.stop

    def _enforce_stops(self, note: str = "") -> list[BookEvent]:
        """Push each part's wanted stop to the broker when it is tighter than the current one."""
        events: list[BookEvent] = []
        if not self.armed:
            return events  # the wanted stops are kept and sent once confirmed
        for group in self._groups:
            if group.status != OPEN:
                continue
            moved = False
            for part in group.parts:
                position = self._positions.get(part.position) if part.live else None
                if position is None:
                    continue
                wanted = self._gateway.price(self._wanted_stop(group, part))
                current = part.broker_sl
                tighter = (
                    wanted > current + _EPSILON
                    if group.long
                    else (current <= 0 or wanted < current - _EPSILON)
                )
                if not tighter:
                    continue
                result = self._gateway.modify(position, stop_loss=wanted, signal_id=group.plan.id)
                if result.ok:
                    part.broker_sl = wanted
                    moved = True
            if moved:
                events.append(
                    BookEvent(
                        EventKind.STOP_MOVED,
                        self._now(),
                        group.plan,
                        None,
                        None,
                        group.stop_reason.value,
                        group.stop,
                        note=note,
                    )
                )
        if events:
            self._save()
        return events

    def on_bar(
        self,
        bar: Bar,
        trail: TrailSource | None = None,
        line: StepLineSource | None = None,
    ) -> list[BookEvent]:
        """A closed bar: expiries, TP1-first, breakeven, trailing after TP1; with the
        scalper's ``Management`` also breakeven+, the stop to TP1 after TP2, the step
        trail, the time stop and the flat-by time (D111), by the shared rules."""
        events = self.sync()
        for group in list(self._groups):
            plan = group.plan
            if group.status == PENDING and group.limit:
                if bar.close_ns >= to_ns(plan.expires_at):
                    events += self._withdraw(
                        group, EventKind.EXPIRED, plan.expiry_outcome, plan.withdrawal_detail
                    )
                    continue
                reached_tp1 = (
                    bar.high >= plan.take_profit_1 - _EPSILON
                    if group.long
                    else bar.low + bar.spread <= plan.take_profit_1 + _EPSILON
                )
                if reached_tp1:
                    events += self._withdraw(
                        group,
                        EventKind.TP1_FIRST,
                        Outcome.TP1_FIRST,
                        "price reached TP1 before the entry",
                    )
                continue
            if group.status != OPEN or group.fill_time is None or bar.open_time < group.fill_time:
                continue
            assert group.fill_price is not None
            best = bar.high if group.long else bar.low + bar.spread
            if not group.breakeven_done and breakeven_due(
                long=group.long,
                fill_price=group.fill_price,
                initial_stop=plan.stop_loss,
                best_price=best,
                tp1_done=group.tp1_done,
                at_r=self._risk_cfg.breakeven_at_r,
            ):
                self._to_breakeven(group)
            if group.tp1_done and self._trail_after_tp1 and trail is not None:
                swing_low, swing_high = trail.at(bar.close_ns)
                level = trailed_stop(
                    long=group.long,
                    stop=group.stop,
                    swing_low=swing_low,
                    swing_high=swing_high,
                    close=bar.close,
                    spread=bar.spread,
                )
                if level is not None:
                    group.stop, group.stop_reason = level, ExitReason.TRAIL
            if self._rules is not None:
                self._manage(group, bar, line)
        events += self._enforce_stops()
        self._save()
        return events

    def _manage(self, group: _Group, bar: Bar, line: StepLineSource | None) -> None:
        """The scalper's rules on one open trade, after a closed bar (D111)."""
        rules = self._rules
        assert rules is not None and group.fill_price is not None and group.fill_time is not None
        plan, long = group.plan, group.long
        if close_by_due(plan, bar.close_time):
            self._close_group(group, ExitReason.SESSION_END, "flat-by time")
            return
        if rules.time_stop is not None and not group.tp1_done and not group.time_checked:
            held = bar.close_time - group.fill_time
            if held >= rules.time_stop:
                group.time_checked = True
                if time_stop_due(
                    long=long,
                    fill_price=group.fill_price,
                    initial_stop=plan.stop_loss,
                    close=bar.close,
                    spread=bar.spread,
                    held=held,
                    after=rules.time_stop,
                    min_r=rules.time_stop_min_r,
                ):
                    self._close_group(group, ExitReason.TIME, "time stop")
                    return
        if group.breakeven_done and rules.breakeven_offset > 0 and not group.stop_offset:
            plus = breakeven_plus_stop(
                long=long,
                stop=group.stop,
                fill_price=group.fill_price,
                offset=rules.breakeven_offset,
                close=bar.close,
                spread=bar.spread,
            )
            if plus is not None:  # each part's stop is its own fill plus the offset
                group.stop_offset = rules.breakeven_offset
                group.stop = plus
        # After TP k (k >= 2) the stop goes to TP k-1 (the ladder, D115).
        previous = previous_target(plan, max(group.reached, 2 * group.tp2_done))
        if rules.stop_to_tp1_after_tp2 and previous is not None:
            level = ladder_stop(
                long=long,
                stop=group.stop,
                take_profit_1=previous,
                close=bar.close,
                spread=bar.spread,
            )
            if level is not None:
                group.stop, group.stop_reason = level, ExitReason.TRAIL
        if rules.step_trail and line is not None:
            sided = getattr(line, "line_for", None)
            value, atr = sided(bar.close_ns, long) if sided else line.line_at(bar.close_ns)
            buffer = rules.trail_buffer_atr * atr if not math.isnan(atr) else 0.0
            level = step_trailed_stop(
                long=long,
                stop=group.stop,
                line=value,
                buffer=buffer,
                close=bar.close,
                spread=bar.spread,
            )
            if level is not None:
                group.stop, group.stop_reason = level, ExitReason.TRAIL

    def _close_group(self, group: _Group, reason: ExitReason, why: str) -> None:
        """Close every open part at market (the time stop, the flat-by time); the next
        sync reads the exits. Waits while unarmed, like every order (D78)."""
        if not self.armed:
            return
        group.close_reason = reason
        for part in group.parts:
            position = self._positions.get(part.position) if part.live else None
            if position is not None:
                self._gateway.close(
                    position, comment=f"{self._comment} {why}"[:31], signal_id=group.plan.id
                )

    def _withdraw(
        self, group: _Group, kind: EventKind, outcome: Outcome, detail: str
    ) -> list[BookEvent]:
        """Cancel a trade's entry orders that have not filled."""
        if not self.armed and kind is not EventKind.CANCELLED:
            return []  # expiries wait for the confirmation (the orders keep their SL/TP)
        for part in group.parts:
            if part.position or part.done:
                continue
            result = self._gateway.cancel(part.order, signal_id=group.plan.id)
            if result.ok:
                part.cancelled = True
        if not all(part.cancelled for part in group.parts):
            return []  # something filled meanwhile: the next sync reports it
        self._groups.remove(group)
        self.manager.order_cancelled()
        self._save()
        return [BookEvent(kind, self._now(), group.plan, None, outcome, detail)]

    def withdraw_pending(self, when: dt.datetime, detail: str) -> list[BookEvent]:
        """Withdraw unfilled limit entries. Like expiries, this waits while unarmed."""
        events = self.sync(when)
        for group in list(self._groups):
            if group.status == PENDING and group.limit:
                events += self._withdraw(group, EventKind.EXPIRED, Outcome.WITHDRAWN, detail)
        return events

    def protect_before_news(
        self, when: dt.datetime, bid: float, ask: float, min_r: float, detail: str
    ) -> list[BookEvent]:
        self.sync(when)
        for group in self._groups:
            if group.status != OPEN or group.fill_price is None:
                continue
            price = bid if group.long else ask
            r = abs(group.fill_price - group.plan.stop_loss)
            if (group.fill_price - group.stop) * group.sign <= 0 or r <= 0:
                continue
            if (price - group.fill_price) * group.sign >= min_r * r - _EPSILON:
                self._to_breakeven(group)
        return self._enforce_stops(note=detail)

    # -- the kill switch and the restart ------------------------------------------------- #

    def close_everything(
        self, when: dt.datetime, bid: float, ask: float, detail: str
    ) -> list[BookEvent]:
        """Cancel every entry order and close every position with GoldSweep's magic number."""
        events = self.sync(when)
        for group in list(self._groups):
            if group.status == PENDING:
                events += self._withdraw(group, EventKind.CANCELLED, Outcome.CANCELLED, detail)
        owners = {part.position: g.plan.id for g in self._groups for part in g.parts}
        for position in self._gateway.positions():
            self._gateway.close(
                position, comment="GS kill switch", signal_id=owners.get(position.ticket)
            )
        for order in self._gateway.orders():
            self._gateway.cancel(order.ticket, signal_id=None)
        events += self.sync(when)
        return events

    def arm(self) -> list[str]:
        """The owner confirmed live orders (/golive): catch up on what was held back."""
        self.armed = True
        notes = self.reconcile()
        self._outbox.extend(self._enforce_stops(note="confirmed"))
        return notes

    def reconcile(self) -> list[str]:
        """Match the saved state with the broker after a (re)start. Returns notes to report."""
        notes: list[str] = []
        self._outbox.extend(self.sync())
        known_positions = {p.position for g in self._groups for p in g.parts if p.position}
        known_orders = {p.order for g in self._groups for p in g.parts}
        for position in self._gateway.positions():
            if position.ticket in known_positions or position.identifier in known_orders:
                continue
            if not self.armed:
                notes.append(
                    f"position {position.ticket} ({position.comment}) is not in the saved state "
                    "(it is handled once live orders are confirmed)"
                )
                continue
            if position.sl <= 0 or position.tp <= 0:
                self._gateway.close(position, comment="GS no stop: closed", signal_id=None)
                notes.append(
                    f"position {position.ticket} ({position.comment}) had no stop-loss or "
                    "take-profit: closed for safety"
                )
            else:
                notes.append(
                    f"position {position.ticket} ({position.comment}) is not in the saved "
                    f"state: it keeps its own SL {position.sl:g} and TP {position.tp:g} and is "
                    "not managed"
                )
        for order in self._gateway.orders():
            if order.ticket not in known_orders and self.armed:
                self._gateway.cancel(order.ticket, signal_id=None)
                notes.append(f"entry order {order.ticket} ({order.comment}) was unknown: cancelled")
        if self._groups:
            notes.append(
                f"resumed {len(self.open_positions)} open trade(s) and "
                f"{len(self.pending_orders)} waiting entry order(s)"
            )
        self._save()
        return notes

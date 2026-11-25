"""Executors (spec section 7): one interface for paper and MetaTrader 5.

:class:`PaperExecutor` trades on paper with the backtester's own
:class:`~goldsweep.execution.book.TradeBook`, so a paper trade is filled,
managed and costed exactly as a backtested one. It has no code that can send an
order to a broker. Its whole state (entry orders, positions, the risk counters)
is saved in SQLite after every change, so a restart resumes where it stopped,
and every closed trade is kept in the ``paper_trades`` table. The MetaTrader 5
executor is :class:`goldsweep.execution.live_executor.MT5Executor`.
"""

from __future__ import annotations

import datetime as dt
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from goldsweep.core.clock import ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import RiskConfig, Settings
from goldsweep.core.types import Mode
from goldsweep.data.broker import SymbolSpec
from goldsweep.execution.book import (
    Bar,
    BookEvent,
    EventKind,
    ExitReason,
    Management,
    StepLineSource,
    Trade,
    TradeBook,
    TradePlan,
    TrailSource,
)
from goldsweep.risk.risk_manager import NewsEvent, RiskManager
from goldsweep.storage.db import Database

log = get_logger(__name__)

STATE_KEY = "paper.book"


class Executor(ABC):
    """What the live loop needs from an executor, paper or real."""

    mode: Mode
    #: The risk limits and counters behind this executor.
    manager: RiskManager
    #: How messages name the mode: PAPER, LIVE DEMO or LIVE.
    label: str = "PAPER"
    #: True when saved state was found at startup.
    restored: bool = False
    #: False after an unattended live start until the owner confirms (/golive):
    #: no order of any kind is sent then, except the kill switch.
    armed: bool = True
    #: The broker account number (live), for the confirmation phrase.
    login: int | None = None

    def arm(self) -> list[str]:
        """Allow orders from now on; returns notes on what was done at once."""
        self.armed = True
        return []

    def sync(self, now: dt.datetime | None = None) -> list[BookEvent]:
        """Compare with the broker and report what changed (nothing to do on paper)."""
        return []

    @abstractmethod
    def protect_before_news(
        self, when: dt.datetime, bid: float, ask: float, min_r: float, detail: str
    ) -> list[BookEvent]:
        """Spec 5.6: move stops to breakeven before a release when far enough in profit."""

    @abstractmethod
    def trades_between(self, start: dt.datetime, end: dt.datetime) -> list[TradeRow]:
        """Closed trades whose last exit lies in ``[start, end)``."""

    @abstractmethod
    def submit(
        self,
        plan: TradePlan,
        *,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> BookEvent:
        """Risk-check, size and place an entry order for ``plan``."""

    @abstractmethod
    def on_bar(
        self,
        bar: Bar,
        trail: TrailSource | None = None,
        line: StepLineSource | None = None,
    ) -> list[BookEvent]:
        """Apply a newly closed bar to orders and positions (``line``: the scalper's
        step-trail line, used with ``Management.step_trail``)."""

    @abstractmethod
    def close_everything(
        self, when: dt.datetime, bid: float, ask: float, detail: str
    ) -> list[BookEvent]:
        """The kill switch: cancel every order and close every position."""

    def close_plan(
        self, when: dt.datetime, bid: float, ask: float, plan_id: str, detail: str
    ) -> list[BookEvent]:
        """Close one trade (its setup is done, S10): cancel its entry or close it."""
        raise NotImplementedError

    def plan_for(self, plan_id: str) -> TradePlan | None:
        """The plan of an open trade or waiting order (S10's adds need it)."""
        return None

    @abstractmethod
    def withdraw_pending(self, when: dt.datetime, detail: str) -> list[BookEvent]:
        """Withdraw every unfilled entry order (a news blackout is about to start).

        Open positions are not touched.
        """

    @property
    @abstractmethod
    def equity(self) -> float: ...

    @property
    @abstractmethod
    def open_positions(self) -> list[dict[str, Any]]: ...

    @property
    @abstractmethod
    def pending_orders(self) -> list[TradePlan]: ...


@dataclass(frozen=True, slots=True)
class TradeRow:
    """A closed trade, as stored."""

    id: str
    direction: str
    session: str | None
    exit_time: dt.datetime
    exit_reason: str
    net_pnl: float
    r_multiple: float
    lots: float


class PaperExecutor(Executor):
    """Simulated execution with persistent state. Never contacts a broker."""

    mode = Mode.PAPER

    def __init__(
        self,
        settings: Settings,
        spec: SymbolSpec,
        db: Database,
        *,
        starting_equity: float,
        state_key: str = STATE_KEY,
        table: str | None = None,
        risk: RiskConfig | None = None,
        management: Management | None = None,
    ) -> None:
        """``state_key``, ``table``, ``risk`` and ``management`` default to the main
        strategy's; the scalper passes its own (D111)."""
        config = settings.config
        self._db = db
        self._state_key = state_key
        self._table = table or PAPER_TABLE
        self._book = TradeBook(
            spec,
            risk or config.risk,
            config.news,
            config.strategy.liquidity.day_boundary,
            equity=starting_equity,
            slippage_points=config.backtest.costs.slippage_points,
            commission_per_lot=config.backtest.costs.commission_per_lot,
            trail_after_tp1=config.backtest.trail_after_tp1 if management is None else False,
            management=management,
        )
        saved = db.get_state(state_key)
        self.restored = saved is not None
        if saved is not None:
            self._book.restore(saved)
            log.info(
                "paper book restored",
                orders=len(self._book.orders),
                positions=len(self._book.positions),
                equity=round(self._book.manager.equity, 2),
            )

    # -- state ---------------------------------------------------------------- #

    @property
    def book(self) -> TradeBook:
        return self._book

    @property
    def manager(self) -> RiskManager:
        return self._book.manager

    @property
    def equity(self) -> float:
        return self._book.manager.equity

    @property
    def open_positions(self) -> list[dict[str, Any]]:
        return self._book.positions

    @property
    def pending_orders(self) -> list[TradePlan]:
        return self._book.orders

    def _save(self, events: Sequence[BookEvent]) -> None:
        self._db.set_state(self._state_key, self._book.to_state())
        for event in events:
            if event.kind is EventKind.CLOSED and event.trade is not None:
                store_trade(self._db, self._table, event.trade)

    # -- trading -------------------------------------------------------------- #

    def submit(
        self,
        plan: TradePlan,
        *,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> BookEvent:
        if self._book.holds(plan.id):
            raise ValueError(f"{plan.id} is already in the book")
        event = self._book.submit(
            plan,
            None,
            spread_points=spread_points,
            median_spread_points=median_spread_points,
            events=events,
        )
        self._save([event])
        return event

    def on_bar(
        self,
        bar: Bar,
        trail: TrailSource | None = None,
        line: StepLineSource | None = None,
    ) -> list[BookEvent]:
        if self._book.idle:
            return []
        events = self._book.on_bar(bar, trail, line)
        self._save(events)
        return events

    def close_everything(
        self, when: dt.datetime, bid: float, ask: float, detail: str
    ) -> list[BookEvent]:
        events = self._book.close_everything(when, bid, ask, ExitReason.MANUAL, detail)
        self._save(events)
        return events

    def close_plan(
        self, when: dt.datetime, bid: float, ask: float, plan_id: str, detail: str
    ) -> list[BookEvent]:
        events = self._book.close_plan(when, bid, ask, plan_id, ExitReason.SETUP_DONE, detail)
        if events:
            self._save(events)
        return events

    def plan_for(self, plan_id: str) -> TradePlan | None:
        return self._book.plan_of(plan_id)

    def withdraw_pending(self, when: dt.datetime, detail: str) -> list[BookEvent]:
        events = self._book.withdraw_orders(when, detail)
        if events:
            self._save(events)
        return events

    def protect_before_news(
        self, when: dt.datetime, bid: float, ask: float, min_r: float, detail: str
    ) -> list[BookEvent]:
        events = self._book.protect_before_news(when, bid, ask, min_r, detail)
        if events:
            self._save(events)
        return events

    # -- history -------------------------------------------------------------- #

    def trades_between(self, start: dt.datetime, end: dt.datetime) -> list[TradeRow]:
        return trades_between(self._db, self._table, start, end)


# --------------------------------------------------------------------------- #
# Closed trades in SQLite (every trades table shares one layout)
# --------------------------------------------------------------------------- #

PAPER_TABLE = "paper_trades"
LIVE_TABLE = "live_trades"
#: The scalper's closed trades (D111): on paper, and on the demo account.
SCALP_PAPER_TABLE = "scalp_trades"
SCALP_LIVE_TABLE = "scalp_live_trades"
_TABLES = frozenset({PAPER_TABLE, LIVE_TABLE, SCALP_PAPER_TABLE, SCALP_LIVE_TABLE})


def store_trade(db: Database, table: str, trade: Trade) -> None:
    """Keep one closed trade, with each of its exits."""
    if table not in _TABLES:
        raise ValueError(f"unknown trade table {table!r}")
    plan = trade.plan
    legs = [
        {
            "lots": leg.lots,
            "price": leg.exit_price,
            "time": leg.exit_time.isoformat(),
            "reason": leg.reason.value,
            "pnl": round(leg.pnl, 2),
        }
        for leg in trade.legs
    ]
    db.execute(
        f"INSERT OR REPLACE INTO {table} (id, direction, session, score, signal_time, "
        "fill_time, exit_time, fill_price, stop_loss, take_profit_1, take_profit_2, lots, "
        "risk_amount, exit_reason, net_pnl, r_multiple, legs) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            plan.id,
            plan.direction.value,
            plan.session,
            plan.score,
            int(plan.time.timestamp()),
            int(trade.fill_time.timestamp()),
            int(trade.exit_time.timestamp()),
            trade.fill_price,
            trade.stop_loss,
            trade.take_profit_1,
            trade.take_profit_2,
            trade.lots,
            trade.risk_amount,
            trade.exit_reason.value,
            trade.net_pnl,
            trade.r_multiple,
            json.dumps(legs),
        ),
    )


def trades_between(
    db: Database, table: str, start: dt.datetime, end: dt.datetime
) -> list[TradeRow]:
    if table not in _TABLES:
        raise ValueError(f"unknown trade table {table!r}")
    rows = db.query(
        "SELECT id, direction, session, exit_time, exit_reason, net_pnl, r_multiple, lots "
        f"FROM {table} WHERE exit_time >= ? AND exit_time < ? ORDER BY exit_time",
        (int(ensure_utc(start).timestamp()), int(ensure_utc(end).timestamp())),
    )
    return [
        TradeRow(
            r[0], r[1], r[2], dt.datetime.fromtimestamp(r[3], tz=dt.UTC), r[4], r[5], r[6], r[7]
        )
        for r in rows
    ]

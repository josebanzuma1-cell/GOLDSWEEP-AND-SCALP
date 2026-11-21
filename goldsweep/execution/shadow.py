"""The shadow book: monitored A+ setups traded on paper (Phase 10, D87).

A session's candidate playbook must prove itself on new data before it may trade.
Every setup that matches it is traded here, in paper *and* live mode, by the same
:class:`~goldsweep.execution.book.TradeBook` that runs paper trading and the
backtests: the same fills, costs, deadline rule and management. Nothing is ever
sent to MetaTrader 5.

Each finished trade is kept in ``shadow_trades`` with its session's playbook and
the fingerprint of the candidate rule it was monitored for, so editing a candidate
starts its count again. The book's state is saved after every change.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from goldsweep.analysis.grades import playbook_session
from goldsweep.execution.book import Bar, BookEvent, EventKind, TradeBook, TradePlan, TrailSource

if TYPE_CHECKING:
    from goldsweep.core.settings import PlaybookRule, Settings
    from goldsweep.data.broker import SymbolSpec
    from goldsweep.risk.risk_manager import NewsEvent
    from goldsweep.storage.db import Database

STATE_KEY = "shadow.book"
RULES_KEY = "shadow.fingerprints"  # signal id -> the candidate it is monitored for
#: The shadow book's own notional balance: results are counted in R, not money.
SHADOW_EQUITY = 10_000.0


def fingerprint(rule: PlaybookRule) -> str:
    """A short, stable identity of a candidate rule."""
    return hashlib.sha256(rule.model_dump_json().encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Tally:
    """How a candidate has done in monitoring."""

    finished: int
    won: int
    total_r: float

    @property
    def average_r(self) -> float | None:
        return self.total_r / self.finished if self.finished else None

    def confirmed(self, needed: int) -> bool:
        """Enough finished trades, with a positive average."""
        return self.finished >= needed and (self.average_r or 0.0) > 0


class ShadowBook:
    """Paper-trades monitored setups and keeps their results per candidate."""

    def __init__(self, settings: Settings, spec: SymbolSpec, db: Database) -> None:
        self._settings = settings
        self._spec = spec
        self._db = db
        self._book = self._new_book()
        saved = db.get_state(STATE_KEY)
        if saved is not None:
            self._book.restore(saved)

    def _new_book(self) -> TradeBook:
        config = self._settings.config
        return TradeBook(
            self._spec,
            config.risk,
            config.news,
            config.strategy.liquidity.day_boundary,
            equity=SHADOW_EQUITY,
            slippage_points=config.backtest.costs.slippage_points,
            commission_per_lot=config.backtest.costs.commission_per_lot,
            trail_after_tp1=config.backtest.trail_after_tp1,
        )

    @property
    def orders(self) -> list[TradePlan]:
        return self._book.orders

    @property
    def positions(self) -> list[dict[str, Any]]:
        return self._book.positions

    def _rules(self) -> dict[str, str]:
        return dict(self._db.get_state(RULES_KEY, {}))

    def _save(self) -> None:
        self._db.set_state(STATE_KEY, self._book.to_state())

    def submit(
        self,
        plan: TradePlan,
        candidate: PlaybookRule,
        *,
        spread_points: float,
        median_spread_points: float | None,
        events: Sequence[NewsEvent] = (),
    ) -> BookEvent:
        """Place a monitored setup on the shadow book (its own risk limits apply)."""
        if self._book.holds(plan.id):
            raise ValueError(f"{plan.id} is already on the shadow book")
        event = self._book.submit(
            plan,
            None,
            spread_points=spread_points,
            median_spread_points=median_spread_points,
            events=events,
        )
        if event.kind is EventKind.PLACED:
            rules = self._rules()
            rules[plan.id] = fingerprint(candidate)
            self._db.set_state(RULES_KEY, rules)
        self._save()
        return event

    def on_bar(self, bar: Bar, trail: TrailSource | None = None) -> list[BookEvent]:
        """Apply a closed bar; finished trades are stored. Returns the CLOSED events."""
        if self._book.idle:
            return []
        events = self._book.on_bar(bar, trail)
        closed = [e for e in events if e.kind is EventKind.CLOSED and e.trade is not None]
        if closed or events:
            rules = self._rules()
            for event in closed:
                self._store(event, rules.pop(event.plan.id, ""))
            for event in events:
                if event.kind in (EventKind.EXPIRED, EventKind.TP1_FIRST, EventKind.CANCELLED):
                    rules.pop(event.plan.id, None)
            self._db.set_state(RULES_KEY, rules)
            self._save()
        return closed

    def _store(self, event: BookEvent, rule: str) -> None:
        trade = event.trade
        assert trade is not None
        plan = trade.plan
        self._db.execute(
            "INSERT OR REPLACE INTO shadow_trades (id, playbook, fingerprint, direction, "
            "session, score, signal_time, fill_time, exit_time, fill_price, stop_loss, "
            "exit_reason, net_pnl, r_multiple) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.id,
                playbook_session(plan.session) or "",
                rule,
                plan.direction.value,
                plan.session,
                plan.score,
                int(plan.time.timestamp()),
                int(trade.fill_time.timestamp()),
                int(trade.exit_time.timestamp()),
                trade.fill_price,
                trade.stop_loss,
                trade.exit_reason.value,
                trade.net_pnl,
                trade.r_multiple,
            ),
        )

    def tally(self, playbook: str, candidate: PlaybookRule) -> Tally:
        """Finished monitored trades of ``candidate`` in ``playbook``."""
        rows = self._db.query(
            "SELECT COUNT(*), SUM(CASE WHEN r_multiple > 0 THEN 1 ELSE 0 END), "
            "SUM(r_multiple) FROM shadow_trades WHERE playbook = ? AND fingerprint = ?",
            (playbook, fingerprint(candidate)),
        )
        count, won, total = rows[0]
        return Tally(int(count or 0), int(won or 0), float(total or 0.0))

    def clear(self, when: dt.datetime) -> None:
        """The kill switch: drop every monitored order and position (nothing is counted)."""
        del when
        self._book = self._new_book()
        self._db.set_state(RULES_KEY, {})
        self._save()

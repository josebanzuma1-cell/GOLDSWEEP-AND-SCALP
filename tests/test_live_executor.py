"""Live execution on MT5 (Phase 6) against a fake trade server.

The account holds 10,000 USD; gold moves 100 USD per 1.00 on one lot. A long
entered at 2650.21 with its stop at 2645.21 risks 5.00 per unit, so 0.5% of
the account (50 USD) buys 0.10 lots: 0.05 to TP1, 0.03 to TP2, 0.02 to TP3.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import Settings
from goldsweep.data.mt5_client import MT5Client
from goldsweep.execution.book import Bar, EventKind, ExitReason, Outcome, TradePlan, to_ns
from goldsweep.execution.live_executor import STATE_KEY, MT5Executor, order_logger
from goldsweep.execution.mt5_gateway import MT5Gateway
from goldsweep.storage.db import Database
from tests.fake_mt5 import gold_symbol
from tests.fake_mt5_trading import FakeTradingMT5
from tests.helpers import utc

NOW = utc(2026, 11, 18, 8, 0, 3)
FIVE = dt.timedelta(minutes=5)


def plan(**changes: Any) -> TradePlan:
    values: dict[str, Any] = {
        "id": "2026-11-18-LDN-01",
        "time": utc(2026, 11, 18, 8, 0),
        "direction": Direction.LONG,
        "session": "london",
        "entry_mode": "market",
        "entry": 2650.21,
        "stop_loss": 2645.21,
        "take_profit_1": 2660.0,
        "take_profit_2": 2665.0,
        "take_profit_3": 2670.0,
        "expires_at": utc(2026, 11, 18, 8, 30),
        "score": 82.0,
        "risk_reward_1": 1.96,
    }
    values.update(changes)
    return TradePlan(**values)


class Rig:
    """A fake MT5 terminal, the gateway, a database and the executor."""

    def __init__(self, settings: Settings, db: Database, **execution: Any) -> None:
        config = settings.config
        if execution:
            config = config.model_copy(
                update={"execution": config.execution.model_copy(update=execution)}
            )
        self.settings = dataclasses.replace(settings, config=config)
        self.db = db
        self.clock = ManualClock(NOW)
        self.fake = FakeTradingMT5(self.clock, symbols=[gold_symbol()])
        self.client = MT5Client(
            mt5=self.fake, clock=self.clock, sleep=lambda _s: None, server_timezone="+02:00"
        )
        self.client.connect()
        self.fake.quote(2650.00)  # ask 2650.21
        spec = self.client.resolve_symbol(["XAUUSD"])
        self.gateway = MT5Gateway(
            self.client,
            spec,
            magic=config.execution.magic_number,
            deviation_points=config.execution.deviation_points,
            order_log=order_logger(db, self.clock),
            sleep=lambda _s: None,
        )
        self.executor = self.make()

    def make(self) -> MT5Executor:
        return MT5Executor(self.settings, self.gateway, self.db, clock=self.clock, demo=True)

    def submit(self, the_plan: TradePlan | None = None) -> Any:
        return self.executor.submit(
            the_plan or plan(), spread_points=21.0, median_spread_points=20.0
        )

    def positions(self) -> list[Any]:
        return sorted(self.fake.open_positions.values(), key=lambda p: p.ticket)

    def move(self, bid: float, minutes: float = 1.0) -> list[Any]:
        self.clock.advance(dt.timedelta(minutes=minutes))
        self.fake.quote(bid)
        return self.executor.sync()

    def bar(self, opened: dt.datetime, o: float, h: float, low: float, c: float) -> Bar:
        start = to_ns(opened)
        return Bar(start, start + to_ns(opened + FIVE) - to_ns(opened), o, h, low, c, 0.21)


def kinds(events: list[Any]) -> list[str]:
    return [e.kind.value for e in events]


def test_market_trade_is_split_and_sent_with_stops(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    placed = rig.submit()
    assert placed.kind is EventKind.PLACED
    assert placed.detail == "0.1 lots in 3 position(s) (market), 50.00 at risk"
    positions = rig.positions()
    assert [(p.volume, p.sl, p.tp) for p in positions] == [
        (0.05, 2645.21, 2660.0),
        (0.03, 2645.21, 2665.0),
        (0.02, 2645.21, 2670.0),
    ]
    assert [p.comment for p in positions] == [
        "GS 2026-11-18-LDN-01 1/3",
        "GS 2026-11-18-LDN-01 2/3",
        "GS 2026-11-18-LDN-01 3/3",
    ]
    assert all(r["sl"] and r["tp"] for r in rig.fake.sent)  # never an order without both
    (filled,) = rig.executor.sync()
    assert (filled.kind, filled.price, filled.lots) == (EventKind.FILLED, 2650.21, 0.1)
    rows = db.query("SELECT action, ok, retcode FROM order_log ORDER BY id")
    assert rows == [("market", 1, 10009)] * 3


def test_tp1_moves_the_rest_to_breakeven_then_the_trade_closes(
    settings: Settings, db: Database
) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    events = rig.move(2660.10)  # TP1 2660.00 is hit by the broker
    assert kinds(events) == ["leg_closed", "stop_moved"]
    assert events[0].leg.reason is ExitReason.TP1 and events[0].leg.pnl == pytest.approx(48.95)
    assert events[1].detail == "breakeven"
    assert [p.sl for p in rig.positions()] == [2650.21, 2650.21]  # TP2 and TP3 parts
    events = rig.move(2650.00)  # back to the entry: both stopped at breakeven
    assert kinds(events) == ["closed"]
    trade = events[0].trade
    assert [leg.reason for leg in trade.legs] == [
        ExitReason.TP1,
        ExitReason.BREAKEVEN,
        ExitReason.BREAKEVEN,
    ]
    assert trade.net_pnl == pytest.approx(48.95) and trade.r_multiple == pytest.approx(0.979)
    assert rig.executor.open_positions == [] and rig.executor.manager.open_positions == 0
    (row,) = rig.executor.trades_between(NOW, NOW + dt.timedelta(hours=1))
    assert (row.id, row.exit_reason) == ("2026-11-18-LDN-01", "breakeven")


def test_stop_loss_closes_every_part(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    (closed,) = rig.move(2645.00)
    assert closed.kind is EventKind.CLOSED and closed.trade.exit_reason is ExitReason.STOP
    assert closed.trade.net_pnl == pytest.approx(-50.0) and closed.trade.r_multiple == -1.0


def test_breakeven_at_one_r_on_a_closed_bar(settings: Settings, db: Database) -> None:
    def run(breakeven_at_r: float) -> tuple[list[Any], Rig]:
        risk = settings.config.risk.model_copy(update={"breakeven_at_r": breakeven_at_r})
        config = settings.config.model_copy(update={"risk": risk})
        rig = Rig(dataclasses.replace(settings, config=config), Database(":memory:"))
        rig.submit()
        rig.executor.sync()
        rig.move(2655.50, minutes=2)
        bar = rig.bar(utc(2026, 11, 18, 8, 5), 2652.0, 2655.60, 2651.0, 2655.5)  # +1R reached
        rig.clock.set(utc(2026, 11, 18, 8, 10, 2))
        return rig.executor.on_bar(bar), rig

    events, rig = run(1.0)  # the spec's rule
    assert kinds(events) == ["stop_moved"] and events[0].price == pytest.approx(2650.21)
    assert {p.sl for p in rig.positions()} == {2650.21}
    rig.db.close()
    events, rig = run(0.0)  # the shipped rule (D84): only reaching TP1 moves it
    assert events == [] and {p.sl for p in rig.positions()} == {2645.21}
    rig.db.close()


def test_limit_entry_fills_or_expires(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    placed = rig.submit(plan(entry_mode="fvg_mid", entry=2648.0, stop_loss=2643.0))
    assert "(limit 2648.00)" in placed.detail and len(rig.fake.pending_orders) == 3
    assert rig.executor.pending_orders[0].entry == 2648.0
    (filled,) = rig.move(2647.70)  # ask 2647.91 reaches the limit
    assert filled.kind is EventKind.FILLED and filled.price == 2648.0

    rig = Rig(settings, Database(":memory:"))
    rig.submit(plan(entry_mode="fvg_mid", entry=2648.0, stop_loss=2643.0))
    bar = rig.bar(utc(2026, 11, 18, 8, 25), 2650.0, 2651.0, 2649.0, 2650.5)  # closes 08:30
    (expired,) = rig.executor.on_bar(bar)
    assert (expired.kind, expired.outcome) == (EventKind.EXPIRED, Outcome.EXPIRED)
    assert rig.fake.pending_orders == {} and rig.executor.manager.open_positions == 0
    rig.db.close()


def test_limit_entry_withdrawn_at_its_cut_deadline(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit(
        plan(
            entry_mode="fvg_mid",
            entry=2648.0,
            stop_loss=2643.0,
            expires_at=utc(2026, 11, 18, 8, 10),
            expiry_note="the news blackout for US CPI",
        )
    )
    bar = rig.bar(utc(2026, 11, 18, 8, 5), 2650.0, 2651.0, 2649.0, 2650.5)  # closes 08:10
    (gone,) = rig.executor.on_bar(bar)
    assert (gone.kind, gone.outcome, gone.detail) == (
        EventKind.EXPIRED,
        Outcome.WITHDRAWN,
        "withdrawn before the news blackout for US CPI",
    )
    assert rig.fake.pending_orders == {} and rig.executor.manager.open_positions == 0


def test_withdraw_pending_cancels_only_unfilled_entries(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit(plan(entry_mode="fvg_mid", entry=2648.0, stop_loss=2643.0))
    rig.executor.armed = False  # an unattended start before /golive: nothing is sent
    assert rig.executor.withdraw_pending(NOW, "withdrawn before the news blackout") == []
    assert len(rig.fake.pending_orders) == 3
    rig.executor.armed = True
    (gone,) = rig.executor.withdraw_pending(NOW, "withdrawn before the news blackout")
    assert (gone.kind, gone.outcome) == (EventKind.EXPIRED, Outcome.WITHDRAWN)
    assert rig.fake.pending_orders == {} and rig.executor.pending_orders == []

    rig = Rig(settings, Database(":memory:"))
    rig.submit()  # a market trade: open positions are never touched
    rig.executor.sync()
    assert rig.executor.withdraw_pending(NOW, "withdrawn before the news blackout") == []
    assert len(rig.positions()) == 3
    rig.db.close()


def test_limit_entry_withdrawn_when_tp1_comes_first(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit(plan(entry_mode="fvg_mid", entry=2648.0, stop_loss=2643.0))
    bar = rig.bar(utc(2026, 11, 18, 8, 5), 2650.0, 2660.5, 2649.0, 2659.0)
    (gone,) = rig.executor.on_bar(bar)
    assert gone.kind is EventKind.TP1_FIRST and rig.fake.pending_orders == {}


def test_limit_already_passed_becomes_a_market_order(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    placed = rig.submit(plan(entry_mode="fvg_mid", entry=2651.0))
    assert "(market)" in placed.detail and "already at the entry" in placed.detail
    assert len(rig.fake.open_positions) == 3


def test_caps_and_margin_check(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db, max_total_lots=0.04)
    placed = rig.submit()
    assert "cut to 0.04 lots by max_total_lots" in placed.detail
    assert [p.volume for p in rig.positions()] == [0.02, 0.01, 0.01]
    rig = Rig(settings, Database(":memory:"), min_margin_level_pct=1_000_000)
    blocked = rig.submit()
    assert blocked.outcome is Outcome.RISK_BLOCKED and "margin level would fall" in blocked.detail
    assert rig.fake.open_positions == {}
    rig.db.close()


def test_risk_rules_still_apply(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    second = rig.submit(plan(id="2026-11-18-LDN-02"))
    assert second.outcome is Outcome.RISK_BLOCKED and "already open" in second.detail
    assert len(rig.fake.open_positions) == 3


def test_a_broker_refusal_leaves_nothing_behind(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.fake.send_codes = [10019]
    refused = rig.submit()
    assert refused.outcome is Outcome.REFUSED and "10019 not enough money" in refused.detail
    assert rig.executor.manager.open_positions == 0
    assert rig.submit(plan(id="2026-11-18-LDN-02")).kind is EventKind.PLACED


def test_kill_switch_closes_real_positions(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    rig.fake.quote(2652.00)
    events = rig.executor.close_everything(NOW, 2652.0, 2652.21, "kill switch (/stop)")
    assert kinds(events) == ["closed"] and events[0].trade.exit_reason is ExitReason.MANUAL
    assert rig.fake.open_positions == {}
    assert events[0].trade.net_pnl == pytest.approx(1.79 * 0.10 * 100)


def test_closed_by_hand_is_reported(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    first = rig.positions()[0].ticket
    rig.fake.close_by_hand(first)
    (event,) = rig.executor.sync()
    assert event.kind is EventKind.LEG_CLOSED and event.leg.reason is ExitReason.MANUAL


def test_restart_resumes_and_catches_up(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    assert db.get_state(STATE_KEY)["groups"][0]["status"] == "open"
    rig.clock.advance(minutes=30)
    rig.fake.quote(2645.00)  # stopped out while GoldSweep was not running
    restarted = rig.make()
    assert restarted.restored and restarted.manager.open_positions == 1
    notes = restarted.reconcile()
    assert notes == []
    (closed,) = restarted.sync()
    assert closed.kind is EventKind.CLOSED and closed.trade.exit_reason is ExitReason.STOP


def test_restart_reports_positions_it_does_not_know(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    stranger = rig.gateway.open(
        long=True,
        volume=0.01,
        stop_loss=2640.0,
        take_profit=2670.0,
        comment="GS old",
        signal_id="old",
    )
    unprotected = rig.gateway.open(
        long=True,
        volume=0.01,
        stop_loss=2640.0,
        take_profit=2670.0,
        comment="GS bare",
        signal_id="bare",
    )
    rig.fake.open_positions[unprotected.ticket].sl = 0.0  # its stop was removed by hand
    rig.gateway.open(
        long=True,
        volume=0.01,
        stop_loss=2640.0,
        take_profit=2670.0,
        comment="GS waiting",
        signal_id="w",
        limit_price=2649.0,
    )
    notes = rig.executor.reconcile()
    assert any(f"position {stranger.ticket} (GS old) is not in the saved state" in n for n in notes)
    assert any("had no stop-loss or take-profit: closed for safety" in n for n in notes)
    assert any("(GS waiting) was unknown: cancelled" in n for n in notes)
    assert list(rig.fake.open_positions) == [stranger.ticket] and rig.fake.pending_orders == {}


def test_trailing_after_tp1(settings: Settings, db: Database) -> None:
    class Swings:
        def at(self, close_ns: int) -> tuple[float, float]:
            return (2656.0, float("nan"))

    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    rig.move(2660.10)  # TP1: the rest goes to breakeven
    bar = rig.bar(utc(2026, 11, 18, 8, 5), 2659.0, 2660.5, 2657.0, 2659.5)
    rig.clock.set(utc(2026, 11, 18, 8, 10, 2))
    (moved,) = rig.executor.on_bar(bar, Swings())
    assert (moved.detail, moved.price) == ("trail", 2656.0)
    assert {p.sl for p in rig.positions()} == {2656.0}


def test_protect_before_news(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    rig.fake.quote(2655.50)  # +1.06 R
    (moved,) = rig.executor.protect_before_news(NOW, 2655.50, 2655.71, 1.0, "before US CPI")
    assert (moved.detail, moved.note) == ("breakeven", "before US CPI")
    assert {p.sl for p in rig.positions()} == {2650.21}

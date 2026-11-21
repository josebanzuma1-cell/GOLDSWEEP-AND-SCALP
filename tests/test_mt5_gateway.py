"""The MT5 order gateway against a fake trade server."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from goldsweep.core.clock import ManualClock
from goldsweep.data.mt5_client import MT5Client
from goldsweep.execution.mt5_gateway import MT5Gateway, OrderResult
from tests.fake_mt5 import gold_symbol
from tests.fake_mt5_trading import REASON_EXPERT, REASON_SL, FakeTradingMT5
from tests.helpers import utc

NOW = utc(2026, 11, 18, 8, 0)
MAGIC = 7341926


class Rig:
    def __init__(self, filling_mode: int = 3) -> None:
        self.clock = ManualClock(NOW)
        self.fake = FakeTradingMT5(
            self.clock,
            symbols=[gold_symbol(filling_mode=filling_mode)],
            server_offset=dt.timedelta(hours=2),
        )
        self.sleeps: list[float] = []
        self.client = MT5Client(
            mt5=self.fake, clock=self.clock, sleep=lambda _s: None, server_timezone="+02:00"
        )
        self.client.connect()
        spec = self.client.resolve_symbol(["XAUUSD"])
        self.logged: list[tuple[str, str | None, dict[str, Any], OrderResult]] = []
        self.gateway = MT5Gateway(
            self.client,
            spec,
            magic=MAGIC,
            deviation_points=30,
            order_log=lambda *row: self.logged.append(row),
            sleep=self.sleeps.append,
        )
        self.fake.quote(2650.00)

    def buy(self, **kwargs: Any) -> OrderResult:
        values: dict[str, Any] = {
            "long": True,
            "volume": 0.05,
            "stop_loss": 2645.0,
            "take_profit": 2660.0,
            "comment": "GS 2026-11-18-LDN-01 1/3",
            "signal_id": "2026-11-18-LDN-01",
        }
        values.update(kwargs)
        return self.gateway.open(**values)


def test_market_order_carries_its_stops() -> None:
    rig = Rig()
    result = rig.buy()
    assert result.ok and result.retcode == 10009 and result.price == pytest.approx(2650.21)
    request = rig.fake.sent[-1]
    assert request["sl"] == 2645.0 and request["tp"] == 2660.0
    assert (request["magic"], request["deviation"], request["comment"]) == (
        MAGIC,
        30,
        "GS 2026-11-18-LDN-01 1/3",
    )
    assert request["type_filling"] == 0  # FOK, the first mode the symbol allows
    assert rig.fake.count("order_check") == 1  # checked before it was sent
    (position,) = rig.gateway.positions()
    assert (position.long, position.volume, position.sl, position.tp) == (
        True,
        0.05,
        2645.0,
        2660.0,
    )
    assert position.time == NOW  # server time converted back to UTC
    assert [row[0] for row in rig.logged] == ["market"]


def test_every_send_is_timed(tmp_path: Any) -> None:
    """S2: how long each order_send took reaches the order log (the tick mode needs it)."""
    from goldsweep.execution.live_executor import order_logger
    from goldsweep.storage.db import Database

    rig = Rig()
    ticks = iter([10.000, 10.087, 20.000, 20.042])  # seconds: 87 ms, then 42 ms
    with Database(tmp_path / "orders.sqlite3") as db:
        gateway = MT5Gateway(
            rig.client,
            rig.gateway.spec,
            magic=MAGIC,
            deviation_points=30,
            order_log=order_logger(db, rig.clock),
            timer=lambda: next(ticks),
        )
        result = gateway.open(
            long=True,
            volume=0.05,
            stop_loss=2645.0,
            take_profit=2660.0,
            comment="GS t 1/1",
            signal_id="t",
        )
        assert result.ok and result.elapsed_ms == pytest.approx(87.0)
        assert db.query("SELECT elapsed_ms FROM order_log") == [(pytest.approx(87.0),)]


def test_no_order_without_a_stop_and_a_target() -> None:
    rig = Rig()
    with pytest.raises(ValueError, match="without a stop-loss"):
        rig.buy(stop_loss=0.0)
    with pytest.raises(ValueError, match="without a stop-loss"):
        rig.buy(take_profit=0.0)
    assert rig.fake.count("order_send") == 0


def test_filling_mode_is_found_with_order_check() -> None:
    rig = Rig()
    rig.fake.allowed_fillings = {1}  # this server takes IOC only
    assert rig.buy().ok
    assert [c[1][0]["type_filling"] for c in rig.fake.calls_named("order_check")] == [0, 1]
    assert rig.fake.sent[-1]["type_filling"] == 1


def test_requotes_are_retried_with_backoff() -> None:
    rig = Rig()
    rig.fake.send_codes = [10004, 10020]
    result = rig.buy()
    assert result.ok and rig.fake.count("order_send") == 3
    assert rig.sleeps == [0.5, 1.0]
    assert [row[3].retcode for row in rig.logged] == [10004, 10020, 10009]


def test_a_final_refusal_is_not_retried() -> None:
    rig = Rig()
    rig.fake.send_codes = [10019]
    result = rig.buy()
    assert not result.ok and result.message == "10019 not enough money"
    assert rig.fake.count("order_send") == 1 and rig.gateway.positions() == []


def test_order_check_refusal_stops_the_order() -> None:
    rig = Rig()
    result = rig.buy(stop_loss=2655.0)  # above the price for a buy
    assert not result.ok and "10016 invalid stops" in result.message
    assert rig.fake.count("order_send") == 0
    assert rig.logged[-1][0] == "market_check"


def test_limit_order_fills_then_hits_its_stop() -> None:
    rig = Rig()
    result = rig.buy(limit_price=2648.0)
    assert result.ok and result.retcode == 10008
    (order,) = rig.gateway.orders()
    assert (order.ticket, order.long, order.price_open) == (result.ticket, True, 2648.0)
    assert rig.fake.sent[-1]["type_filling"] == 2  # RETURN first for a resting order
    rig.fake.quote(2647.70)  # ask 2647.91 reaches the limit
    (position,) = rig.gateway.positions()
    assert position.identifier == result.ticket and position.price_open == 2648.0
    rig.fake.quote(2644.90)
    assert rig.gateway.positions() == []
    deals = rig.gateway.deals(position.ticket)
    assert [d.entry for d in deals] == [0, 1]
    assert deals[-1].reason == REASON_SL and deals[-1].price == 2645.0
    assert deals[-1].profit == pytest.approx(-15.0)  # 3.00 x 0.05 lots x 100


def test_modify_close_and_cancel() -> None:
    rig = Rig()
    rig.buy()
    (position,) = rig.gateway.positions()
    rig.fake.quote(2655.00)
    assert rig.gateway.modify(position, stop_loss=2650.21, signal_id="x").ok
    (moved,) = rig.gateway.positions()
    assert (moved.sl, moved.tp) == (2650.21, 2660.0)  # the target is kept
    assert rig.gateway.close(moved, comment="GS kill switch", signal_id="x").ok
    assert rig.gateway.positions() == []
    assert rig.gateway.deals(moved.ticket)[-1].reason == REASON_EXPERT
    pending = rig.buy(limit_price=2650.0)
    assert rig.gateway.cancel(pending.ticket, signal_id="x").ok
    assert rig.gateway.orders() == []


def test_only_this_bots_positions_are_seen() -> None:
    rig = Rig()
    rig.buy()
    other = MT5Gateway(rig.client, rig.gateway.spec, magic=1, deviation_points=30)
    assert other.positions() == [] and len(rig.gateway.positions()) == 1


def test_margin_level_after_an_order() -> None:
    rig = Rig()
    level = rig.gateway.margin_level_after(True, 1.0, 2650.0)
    assert level == pytest.approx(10_000 / (100 * 2650 / 100) * 100)  # leverage 1:100

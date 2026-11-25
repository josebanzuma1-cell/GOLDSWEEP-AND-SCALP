"""The scalper running in the bot (S5, D111): the HFT mode on paper and on the demo account.

The M1 market is hand-made: quiet bars at 2030 from 06:00 UTC on Tuesday 17 November
2026, then a rise through 08:01-08:03 that reaches each target of a long scalp. The
setups come from a scripted engine (the real engine's setups are tested in
tests/test_scalp_engine.py), so these tests follow what the runner does with a
setup: the gates, the order, the management, the messages and the stored trade.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from goldsweep.alerts.telegram import Command, ConsoleNotifier
from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import Settings
from goldsweep.data.mock_broker import MockBroker
from goldsweep.data.mt5_client import MT5Client
from goldsweep.execution.book import Bar, ExitReason, TradePlan, to_ns
from goldsweep.execution.live_executor import MT5Executor, order_logger
from goldsweep.execution.mt5_gateway import MT5Gateway
from goldsweep.live import Trader
from goldsweep.risk.account_guard import AccountGuard, Exposure
from goldsweep.scalping.engine import SWEEP_RECLAIM, ScalpSetup
from goldsweep.scalping.runner import built_modes, demo_executor
from goldsweep.storage.db import Database
from tests.fake_mt5 import gold_symbol
from tests.fake_mt5_trading import FakeTradingMT5
from tests.helpers import bars, only_spec_model, quiet, utc, without_grade_filter

MINUTE = dt.timedelta(minutes=1)
DAY = utc(2026, 11, 17)  # a Tuesday; London runs 07:00-10:00 UTC in November
SIGNAL_BAR = utc(2026, 11, 17, 7, 59)  # closes at 08:00 inside London
START = utc(2026, 11, 17, 7, 30, 2)  # two seconds after the 07:29 M1 bar closes
RISE = [
    (2030.0, 2030.4, 2029.9, 2030.2),  # 08:00: the scalp fills at this open
    (2030.2, 2031.0, 2030.1, 2030.9),  # 08:01: TP1 2030.60
    (2030.9, 2032.0, 2030.8, 2031.9),  # 08:02: TP2 2031.20, TP3 2031.80
    (2031.9, 2033.0, 2031.8, 2032.9),  # 08:03: TP4 2032.40, TP5 2033.00 (S9's ladder)
]
M1 = bars(quiet(120, 2030.0) + RISE + quiet(60, 2032.9), utc(2026, 11, 17, 6, 0), minutes=1)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return without_grade_filter(only_spec_model(settings))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "scalper.sqlite3")
    yield database
    database.close()


def setup_at(
    opened: dt.datetime,
    direction: Direction = Direction.LONG,
    *,
    reference: float = 2030.0,
    extreme: float = 2028.3,
) -> ScalpSetup:
    """A sweep-and-reclaim setup on the bar opened at ``opened`` (ATR 2.00)."""
    return ScalpSetup(
        index=0,
        time=opened + MINUTE,
        direction=direction,
        mode=SWEEP_RECLAIM,
        reference=reference,
        extreme=extreme,
        trail_line=math.nan,
        atr=2.0,
        level_price=2028.5 if direction is Direction.LONG else 2031.5,
        level_kind="asia",
        flipped=False,
        sweep_time=opened,
    )


class ScriptedEngine:
    """Stands in for the scalp engine: the given setups at the given bars."""

    def __init__(
        self, setups: Sequence[ScalpSetup], *, quiet_from: dt.datetime | None = None
    ) -> None:
        self._setups = {s.time - MINUTE: s for s in setups}
        self._quiet_from = quiet_from
        self.atr: float | None = 2.0
        self.line = math.nan
        self.fed: list[dt.datetime] = []

    def on_bar(
        self, opened: dt.datetime, open_: float, high: float, low: float, close: float
    ) -> list[ScalpSetup]:
        self.fed.append(opened)
        self.atr = 1.0 if self._quiet_from is not None and opened >= self._quiet_from else 2.0
        setup = self._setups.get(opened)
        return [setup] if setup is not None else []


class Harness:
    """A paper bot on the M1 market; the scalper's job is run each minute."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        setups: Sequence[ScalpSetup] = (),
        *,
        start: dt.datetime = START,
        clock: ManualClock | None = None,
        executor: Any = None,
        scalp_demos: Any = None,
        market: pd.DataFrame = M1,
        **engine: Any,
    ) -> None:
        self.db = db
        self.clock = clock or ManualClock(start)
        self.market = MockBroker(clock=self.clock, candles_m1=market)
        self.market.connect()
        self.notes = ConsoleNotifier(io.StringIO())
        self.trader = Trader(
            settings,
            self.market,
            db,
            self.notes,
            clock=self.clock,
            starting_equity=10_000.0,
            executor=executor,
            scalp_demos=scalp_demos,
        )
        self.engine = ScriptedEngine(setups, **engine)
        self.hft = self.trader.scalper.slots["hft"]
        self.hft.logic.engine = self.engine  # type: ignore[attr-defined]

    def say(self, name: str, argument: str = "") -> str:
        return self.trader.handle(Command(name, argument, 1))

    def run_until(self, moment: dt.datetime) -> None:
        """The scalper job at every minute up to ``moment`` (2 s after each close)."""
        while self.clock.now() <= moment + dt.timedelta(seconds=2):
            self.trader.scalper_tick()
            self.clock.advance(MINUTE)

    def scalper_on(self) -> None:
        self.say("mode", "both")
        self.say("scalper", "hft")


# --------------------------------------------------------------------------- #
# On paper
# --------------------------------------------------------------------------- #


def test_a_paper_scalp_is_opened_managed_closed_and_stored(
    settings: Settings, db: Database
) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))
    placed, closed = h.notes.sent
    # The stop 0.10 ATR beyond the sweep's X (2028.30). Five targets (S9): HFT's 0.25 R
    # step (0.475) is widened to 0.60 so TP1 clears twice the round-trip cost (0.30).
    assert placed == (
        "⚡ Scalp HFT LONG 2026-11-17-HFT-001 (paper, 0.13 lots, 0.25% risk): market | "
        "SL 2028.10 | "
        "TP 2030.60 / 2031.20 / 2031.80 / 2032.40 / 2033.00\n"
        "asia low 2028.50 swept to 2028.30, closed back inside, structure shift"
    )
    # Filled at the 08:00 open, out at TP5 on the 08:03 bar (closing 08:04).
    assert closed == (
        "🏁 Scalp HFT 2026-11-17-HFT-001 closed (tp5, paper) after 4m: +$17.70 (+0.62 R)"
    )
    (row,) = db.query("SELECT id, direction, session, exit_reason, lots, legs FROM scalp_trades")
    assert row[:5] == ("2026-11-17-HFT-001", "LONG", "london", "tp5", 0.13)
    # 0.13 lots in five parts, whole steps, the largest remainders first: 3/3/3/2/2.
    assert [(leg["lots"], leg["reason"]) for leg in json.loads(row[5])] == [
        (0.03, "tp1"),
        (0.03, "tp2"),
        (0.03, "tp3"),
        (0.02, "tp4"),
        (0.02, "tp5"),
    ]
    assert db.query("SELECT COUNT(*) FROM paper_trades") == [(0,)]  # the main book: untouched
    paper = h.hft.paper
    assert paper.equity > 10_000.0 and paper.open_positions == []
    status = h.trader.status()
    assert "scalper: HFT (M1, seconds to 15 minutes): on paper" in status
    assert "Scalps closed in the last 24 h: 1, +" in status
    # The warm-up read bars it does not trade; then one bar a minute.
    assert h.engine.fed[0] == utc(2026, 11, 17, 6, 0) and h.engine.fed[-1] == utc(
        2026, 11, 17, 8, 9
    )
    assert len(h.engine.fed) == len(set(h.engine.fed))  # no bar twice


def test_a_restart_resumes_the_open_scalp(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 1))  # filled, nothing reached yet
    assert len(h.hft.paper.open_positions) == 1
    restarted = Harness(settings, db, start=h.clock.now())
    assert len(restarted.hft.paper.open_positions) == 1
    restarted.run_until(utc(2026, 11, 17, 8, 10))
    (closed,) = restarted.notes.sent
    assert "closed (tp5, paper)" in closed


@pytest.mark.parametrize(
    ("strategies", "mode"),
    [("main", "hft"), ("both", "off"), ("scalper", "swing")],  # swing is not built yet
)
def test_no_scalp_unless_the_switch_allows_it(
    settings: Settings, db: Database, strategies: str, mode: str
) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.say("mode", strategies)
    h.say("scalper", mode)
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert h.notes.sent == []
    assert db.query("SELECT COUNT(*) FROM scalp_trades") == [(0,)]


def test_no_scalp_while_paused(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.say("pause")
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert h.notes.sent == []


def test_only_in_the_active_sessions(settings: Settings, db: Database) -> None:
    h = Harness(
        settings, db, [setup_at(utc(2026, 11, 17, 6, 40))], start=utc(2026, 11, 17, 6, 30, 2)
    )
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 6, 50))  # 06:41 UTC: no session is open
    assert h.notes.sent == []


def test_a_quiet_market_is_not_scalped(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)], quiet_from=utc(2026, 11, 17, 7, 50))
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))  # the M1 ATR fell to half its median
    assert h.notes.sent == []


def test_a_late_setup_is_not_traded(settings: Settings, db: Database) -> None:
    market = bars(quiet(120, 2030.0), utc(2026, 11, 17, 6, 0), minutes=1)  # no bar after 07:59
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)], market=market)
    h.scalper_on()
    h.trader.scalper_tick()  # warm-up at 07:30
    h.clock.advance(dt.timedelta(minutes=33))  # 08:03: the 08:00 setup is three minutes old
    h.trader.scalper_tick()
    assert h.notes.sent == []


def test_one_scalp_at_a_time(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR), setup_at(SIGNAL_BAR + MINUTE)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert [n[:30] for n in h.notes.sent] == [
        "⚡ Scalp HFT LONG 2026-11-17-HF",
        "🏁 Scalp HFT 2026-11-17-HFT-001",
    ]


def test_cooldown_after_a_stop_out(settings: Settings, db: Database) -> None:
    falls = [
        (2030.0, 2030.1, 2029.5, 2029.6),  # 08:00: filled
        (2029.6, 2029.7, 2027.9, 2028.0),  # 08:01: stopped at 2028.10
        *quiet(20, 2028.0),
    ]
    market = bars(quiet(120, 2030.0) + falls, utc(2026, 11, 17, 6, 0), minutes=1)
    again = setup_at(utc(2026, 11, 17, 8, 3), reference=2028.0, extreme=2026.3)
    later = setup_at(utc(2026, 11, 17, 8, 8), reference=2028.0, extreme=2026.3)
    h = Harness(settings, db, [setup_at(SIGNAL_BAR), again, later], market=market)
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))
    _placed, stopped, *rest = h.notes.sent
    assert "closed (stop, paper)" in stopped
    # 08:04 is inside the five-minute cooldown after the stop-out; 08:09 is not.
    assert [n[:35] for n in rest] == ["⚡ Scalp HFT LONG 2026-11-17-HFT-002"]
    assert db.query("SELECT exit_reason FROM scalp_trades") == [("stop",)]


def test_the_kill_switch_closes_paper_scalps(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 1))
    reply = h.say("stop")
    assert reply.startswith("🛑 Kill switch: 0 position(s) closed, 1 scalp(s) closed, 0 order(s)")
    assert "closed (manual, paper)" in h.notes.sent[-1]
    assert db.query("SELECT exit_reason FROM scalp_trades") == [("manual",)]


def test_the_dashboard_snapshot_shows_the_scalper(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 0))  # placed: it fills at the next open
    picture = db.get_state("dashboard.snapshot")["scalper"]
    assert (picture["mode"], picture["where"]) == ("hft", "paper")
    assert (picture["open"], picture["waiting"]) == ([], ["2026-11-17-HFT-001"])
    h.run_until(utc(2026, 11, 17, 8, 10))  # closed: written again
    picture = db.get_state("dashboard.snapshot")["scalper"]
    assert (picture["open"], picture["today"]["scalps"]) == ([], 1)


# --------------------------------------------------------------------------- #
# On the demo account (a fake MT5 trade server)
# --------------------------------------------------------------------------- #

SCALP_MAGIC = 7_341_927


class Demo:
    """The main strategy's and the scalper's MT5 executors on one fake account."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        armed: bool = True,
        start: dt.datetime = START,
    ) -> None:
        self.clock = ManualClock(start)
        self.fake = FakeTradingMT5(self.clock, symbols=[gold_symbol()])
        self.client = MT5Client(
            mt5=self.fake, clock=self.clock, sleep=lambda _s: None, server_timezone="+02:00"
        )
        self.client.connect()
        self.fake.quote(2030.00)  # ask 2030.21
        spec = self.client.resolve_symbol(["XAUUSD"])
        config = settings.config

        def gateway(magic: int) -> MT5Gateway:
            return MT5Gateway(
                self.client,
                spec,
                magic=magic,
                deviation_points=config.execution.deviation_points,
                order_log=order_logger(db, self.clock),
                sleep=lambda _s: None,
            )

        self.main = MT5Executor(
            settings, gateway(config.execution.magic_number), db, clock=self.clock, armed=armed
        )
        # One executor per mode, as run_live makes them (D112).
        self.scalps = {
            mode: demo_executor(
                mode, settings, self.client, spec, db, self.clock, demo=True, armed=armed
            )
            for mode in built_modes()
        }
        self.scalp = self.scalps["hft"]


def demo_harness(settings: Settings, db: Database, **options: Any) -> tuple[Demo, Harness]:
    """The bot on the demo account: market data from the M1 market, orders to the fake."""
    demo = Demo(settings, db, **options)
    h = Harness(
        settings,
        db,
        [setup_at(SIGNAL_BAR)],
        clock=demo.clock,
        executor=demo.main,
        scalp_demos=demo.scalps,
    )
    return demo, h


def test_an_approved_mode_sends_scalps_to_the_demo_account(
    settings: Settings, db: Database
) -> None:
    demo, h = demo_harness(settings, db)
    h.scalper_on()
    assert h.say("approve", "scalper hft").startswith(
        "✅ The scalper's hft mode sends its orders to the demo account"
    )
    assert "HFT (M1, seconds to 15 minutes): on the demo account" in h.trader.status()
    h.run_until(utc(2026, 11, 17, 8, 0))
    (placed,) = h.notes.sent
    # Sized at the ask (2030.21): 0.25% of 10,000 over 2.11 a unit is 0.11 lots.
    assert placed.startswith("⚡ Scalp HFT LONG 2026-11-17-HFT-001 (demo account, 0.11 lots, ")
    positions = sorted(demo.fake.open_positions.values(), key=lambda p: p.ticket)
    # Five parts (S9), each its own position with the stop and its own target.
    assert [(p.magic, p.volume, p.sl, p.tp) for p in positions] == [
        (SCALP_MAGIC, 0.03, 2028.1, 2030.6),
        (SCALP_MAGIC, 0.02, 2028.1, 2031.2),
        (SCALP_MAGIC, 0.02, 2028.1, 2031.8),
        (SCALP_MAGIC, 0.02, 2028.1, 2032.4),
        (SCALP_MAGIC, 0.02, 2028.1, 2033.0),
    ]
    assert [p.comment for p in positions] == [
        f"GS-SC|2026-11-17-HFT-001|{part}" for part in range(1, 6)
    ]
    assert all(r["sl"] and r["tp"] for r in demo.fake.sent)  # never an order without both
    assert demo.main.open_positions == []  # the main strategy's book is not involved
    # TP1 to TP5 at the broker; the scalp is stored in its own table.
    for bid in (2031.00, 2031.95, 2033.05):
        demo.fake.quote(bid)
        h.clock.advance(MINUTE)
        h.trader.scalper_tick()
    closed = h.notes.sent[-1]
    assert closed.startswith("🏁 Scalp HFT 2026-11-17-HFT-001 closed (tp5, demo account)")
    assert db.query("SELECT id, exit_reason FROM scalp_live_trades") == [
        ("2026-11-17-HFT-001", "tp5")
    ]
    assert db.query("SELECT COUNT(*) FROM live_trades") == [(0,)]


def test_the_main_strategy_closes_an_opposite_scalp_first(settings: Settings, db: Database) -> None:
    demo, h = demo_harness(settings, db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    h.run_until(utc(2026, 11, 17, 8, 0))
    assert len(demo.fake.open_positions) == 5
    short = TradePlan(
        id="2026-11-17-LDN-01",
        time=utc(2026, 11, 17, 8, 5),
        direction=Direction.SHORT,
        session="london",
        entry_mode="market",
        entry=2030.0,
        stop_loss=2033.0,
        take_profit_1=2025.0,
        take_profit_2=2020.0,
        take_profit_3=None,
        expires_at=utc(2026, 11, 17, 8, 30),
        score=80.0,
        risk_reward_1=1.7,
    )
    h.trader._yield_scalps(short)  # noqa: SLF001 - the rule _handle_signal applies first
    assert demo.fake.open_positions == {}
    assert "closed (manual, demo account)" in h.notes.sent[-1]
    long = dataclasses.replace(
        short,
        direction=Direction.LONG,
        stop_loss=2027.0,
        take_profit_1=2035.0,
        take_profit_2=2040.0,
    )
    h.trader._yield_scalps(long)  # noqa: SLF001 - nothing open: nothing to do


def test_the_account_guard_refuses_an_opposite_scalp(settings: Settings, db: Database) -> None:
    demo, h = demo_harness(settings, db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    context = h.trader._scalp_context(h.clock.now())  # noqa: SLF001
    assert context.demo and context.main_exposures == []
    # A main short already open: the long scalp would hedge it.
    guard = AccountGuard(
        db,
        settings.config.strategy.liquidity.day_boundary,
        tables=("live_trades", "scalp_live_trades"),
        daily_loss_pct=2.0,
        weekly_loss_pct=5.0,
        max_open_risk_pct=0.75,
    )
    decision = guard.check(
        h.clock.now(),
        equity=10_000.0,
        direction=Direction.LONG,
        new_risk=25.0,
        exposures=[Exposure(Direction.SHORT, 50.0)],
    )
    assert decision.reasons == ("the other strategy holds the opposite side (no hedging)",)
    # Wired: with the main short open, the runner places no scalp.
    blocked = dataclasses.replace(
        context, may_open=True, main_exposures=[Exposure(Direction.SHORT, 50.0)]
    )
    h.trader.scalper.step(blocked)  # warm-up
    h.clock.advance(dt.timedelta(minutes=30))
    assert h.trader.scalper.step(blocked) == []
    assert demo.fake.open_positions == {}


def test_unarmed_the_demo_scalper_sends_nothing_until_golive(
    settings: Settings, db: Database
) -> None:
    demo, h = demo_harness(settings, db, armed=False)
    h.scalper_on()
    h.say("approve", "scalper hft")
    h.run_until(utc(2026, 11, 17, 8, 0))
    assert demo.fake.open_positions == {} and demo.fake.sent == []
    assert h.notes.sent == []
    login = demo.main.login
    assert h.say("golive", str(login)).startswith("✅ Live orders confirmed")
    assert demo.scalp.armed


def test_the_kill_switch_closes_demo_scalps(settings: Settings, db: Database) -> None:
    demo, h = demo_harness(settings, db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    h.run_until(utc(2026, 11, 17, 8, 0))
    reply = h.say("stop")
    assert "1 scalp(s) closed" in reply
    assert demo.fake.open_positions == {}


def test_a_real_account_keeps_scalps_on_paper(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo, h = demo_harness(settings, db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    monkeypatch.setattr(h.trader, "label", "LIVE")  # as on a real account
    assert not h.trader._scalp_context(h.clock.now()).demo  # noqa: SLF001
    assert h.say("approve", "scalper hft").startswith("Not approved: this is a real account")


# --------------------------------------------------------------------------- #
# The demo executor's scalp rules (the same functions as the paper book)
# --------------------------------------------------------------------------- #


def scalp_plan_at(ask: float = 2030.21) -> TradePlan:
    return TradePlan(
        id="2026-11-17-HFT-001",
        time=utc(2026, 11, 17, 8, 0),
        direction=Direction.LONG,
        session="london",
        entry_mode="market",
        entry=ask,
        stop_loss=ask - 2.0,
        take_profit_1=ask + 1.0,
        take_profit_2=ask + 2.0,
        take_profit_3=ask + 3.0,
        expires_at=utc(2026, 11, 17, 8, 1),
        score=70.0,
        risk_reward_1=0.5,
    )


class LineAt:
    def __init__(self, value: float, atr: float = 2.0) -> None:
        self.value, self.atr = value, atr

    def line_at(self, close_ns: int) -> tuple[float, float]:
        return self.value, self.atr


def bar(opened: dt.datetime, o: float, h: float, low: float, c: float) -> Bar:
    start = to_ns(opened)
    return Bar(start, start + 60 * 10**9, o, h, low, c, 0.21)


FILLED = utc(2026, 11, 17, 8, 0, 2)
T = utc(2026, 11, 17, 8, 1)  # the first bar after the fill


def submitted(settings: Settings, db: Database, plan: TradePlan | None = None) -> Demo:
    demo = Demo(settings, db, start=FILLED)
    placed = demo.scalp.submit(
        plan or scalp_plan_at(), spread_points=21.0, median_spread_points=20.0
    )
    assert placed.lots
    demo.scalp.sync()
    return demo


def test_breakeven_plus_stop_to_tp1_and_the_step_trail_on_the_demo_account(
    settings: Settings, db: Database
) -> None:
    demo = submitted(settings, db)
    scalp = demo.scalp
    t = T
    demo.fake.quote(2031.30)  # TP1 2031.21 at the broker: the rest to breakeven
    demo.clock.advance(MINUTE)
    scalp.sync()
    assert {p.sl for p in demo.fake.open_positions.values()} == {2030.21}
    scalp.on_bar(bar(t, 2030.2, 2031.3, 2030.1, 2031.3))  # breakeven+: entry + slippage
    assert {p.sl for p in demo.fake.open_positions.values()} == {2030.31}
    demo.fake.quote(2032.30)  # TP2: the last part's stop goes to TP1 on the next close
    demo.clock.advance(MINUTE)
    scalp.on_bar(bar(t + MINUTE, 2031.3, 2032.3, 2031.2, 2032.3))
    (last,) = demo.fake.open_positions.values()
    assert last.sl == 2031.21
    scalp.on_bar(bar(t + 2 * MINUTE, 2032.3, 2032.6, 2032.2, 2032.5), line=LineAt(2032.0))
    (last,) = demo.fake.open_positions.values()
    assert last.sl == pytest.approx(2031.8)  # the line less 0.10 ATR


def slow_bars(demo: Demo, count: int) -> list[Any]:
    """``count`` M1 bars that stay under 0.3 R; returns the executor's events."""
    events: list[Any] = []
    for _ in range(count):
        opened = demo.clock.now().replace(second=0) + MINUTE  # the next bar after the clock
        demo.clock.advance(MINUTE)
        events += demo.scalp.on_bar(bar(opened, 2030.0, 2030.3, 2029.9, 2030.1))
    return events


def test_the_time_stop_closes_a_slow_scalp_on_the_demo_account(
    settings: Settings, db: Database
) -> None:
    demo = submitted(settings, db)
    assert slow_bars(demo, 14) == [] and len(demo.fake.open_positions) == 3  # 14m58s held
    slow_bars(demo, 1)  # 15 minutes without 0.3 R: closed at market
    assert demo.fake.open_positions == {}
    (closed,) = [e for e in demo.scalp.sync() if e.trade is not None]
    assert closed.trade.exit_reason is ExitReason.TIME


def test_the_flat_by_time_closes_a_scalp_on_the_demo_account(
    settings: Settings, db: Database
) -> None:
    plan = dataclasses.replace(scalp_plan_at(), close_by=utc(2026, 11, 17, 8, 3))
    demo = submitted(settings, db, plan)
    slow_bars(demo, 1)  # closes at 08:02
    assert len(demo.fake.open_positions) == 3
    slow_bars(demo, 1)  # closes at 08:03: flat
    assert demo.fake.open_positions == {}
    (closed,) = [e for e in demo.scalp.sync() if e.trade is not None]
    assert closed.trade.exit_reason is ExitReason.SESSION_END


def test_unarmed_the_demo_executor_leaves_positions_to_their_stops(
    settings: Settings, db: Database
) -> None:
    demo = submitted(settings, db)
    demo.scalp.armed = False  # as after an unattended restart
    slow_bars(demo, 20)
    assert len(demo.fake.open_positions) == 3  # no close is sent while unarmed


def test_scalp_context_on_paper(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    context = h.trader._scalp_context(h.clock.now())  # noqa: SLF001
    assert (context.mode, context.may_open, context.demo, context.events) == (
        "off",
        False,
        False,
        (),
    )
    assert (context.modes, context.demo_modes, context.setups, context.graded) == (
        frozenset(),
        frozenset(),
        {},
        {},
    )
    assert context.analysis is not None and context.analysis.bias is None  # no M5 bar yet

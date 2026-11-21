"""Paper trading live: the trader loop, its saved state, commands, news jobs.

The market is the hand-made Phase 2 one (tests/test_signals.py) with the
follow-through of tests/test_backtest_run.py: the London raid of the Asian low
gives one long at 2028.90 that fills, reaches TP1 and then TP2.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from goldsweep import cli
from goldsweep.alerts.telegram import Command, ConsoleNotifier
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import (
    DEFAULT_CONFIG_PATH,
    CalendarSourceConfig,
    Settings,
    load_settings,
)
from goldsweep.data.calendar import (
    CalendarService,
    ForexFactoryProvider,
)
from goldsweep.data.mock_broker import MockBroker
from goldsweep.execution.book import InvalidPlan, TradeBook, TradePlan
from goldsweep.execution.executor import STATE_KEY
from goldsweep.live import ACTIVE, PAUSED, STOPPED, NewsDesk, PaperTrader, run_paper
from goldsweep.storage.db import Database
from tests.helpers import bars, utc
from tests.test_backtest_run import FOLLOW_UP
from tests.test_news_sources import FakeWeb
from tests.test_signals import MONDAY, with_raid

START = utc(2026, 11, 17, 7, 30, 2)  # two seconds after the 07:25 bar closes
FIVE = dt.timedelta(minutes=5)
MARKET = bars(with_raid() + FOLLOW_UP, MONDAY)


class Harness:
    """A paper trader on the hand-made market, with a clock the test moves."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        start: dt.datetime = START,
        news: NewsDesk | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.clock = ManualClock(start)
        self.broker = MockBroker(clock=self.clock, candles_m1=MARKET)
        self.broker.connect()
        self.notes = ConsoleNotifier(io.StringIO())
        self.news = news
        self.trader = self.make()

    def make(self) -> PaperTrader:
        return PaperTrader(
            self.settings,
            self.broker,
            self.db,
            self.notes,
            clock=self.clock,
            news=self.news,
            starting_equity=10_000.0,
        )

    def run_until(self, moment: dt.datetime) -> None:
        """Run the bar job at every close up to ``moment`` (2 s after each close)."""
        while self.clock.now() <= moment + dt.timedelta(seconds=2):
            assert self.trader.on_bar_close()
            self.clock.advance(FIVE)

    def text(self) -> str:
        return "\n".join(self.notes.sent)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "paper.sqlite3")
    yield database
    database.close()


def test_the_textbook_setup_is_traded_on_paper(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 8, 40))
    alert, filled, breakeven, tp1, closed = h.notes.sent
    assert alert.startswith("🟢 XAUUSD LONG setup (score 81.1) — London kill zone")
    assert "Entry: 2028.90 (FVG mid, limit) | SL: 2026.09 | TP1: 2036.00 | TP2: 2048.00" in alert
    assert "Risk 0.5% (0.17 lots)" in alert
    assert "Action: paper limit order placed, valid until 08:25 UTC" in alert
    assert "Mode: PAPER  |  id: 2026-11-17-LDN-01" in alert
    assert "Pressure: Buyers" in alert
    assert filled.startswith("✅ PAPER LONG 2026-11-17-LDN-01 filled at 2028.90, 0.17 lots")
    assert "stop moved to breakeven at 2028.90" in breakeven
    assert "TP1 hit, 0.08 lots closed at 2036.00 (+$56.24)" in tp1
    # The same result as the backtester: one shared trade book.
    assert closed.startswith("🏁 PAPER LONG 2026-11-17-LDN-01 closed (tp2) at 2048.00: +$227.51")
    assert db.query("SELECT id, action FROM signals") == [("2026-11-17-LDN-01", "traded")]
    (row,) = db.query("SELECT exit_reason, round(net_pnl, 2), lots FROM paper_trades")
    assert row == ("tp2", 227.51, 0.17)
    assert h.trader.executor.equity == pytest.approx(10_227.51)
    status = h.trader.status()
    assert "Equity 10,227.51 | today 1 trade(s), +$227.51" in status
    assert "No open positions or entry orders." in status


def test_a_restart_in_the_middle_of_a_trade_resumes_it(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 8, 5))  # filled, still open
    assert len(h.trader.executor.open_positions) == 1
    saved = db.get_state(STATE_KEY)
    assert saved["positions"][0]["plan"]["id"] == "2026-11-17-LDN-01"
    h.trader = h.make()  # the program restarts
    assert h.trader.executor.restored
    assert len(h.trader.executor.open_positions) == 1
    h.run_until(utc(2026, 11, 17, 8, 40))
    (row,) = db.query("SELECT exit_reason, round(net_pnl, 2) FROM paper_trades")
    assert row == ("tp2", 227.51)  # the same outcome as without the restart


def test_bars_missed_while_down_are_applied_in_order(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 7, 55))  # the order is placed
    h.clock.set(utc(2026, 11, 17, 8, 40, 2))  # 45 minutes without a call
    assert h.trader.on_bar_close()
    assert db.query("SELECT exit_reason FROM paper_trades") == [("tp2",)]


def test_waiting_for_a_late_bar(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.clock.set(utc(2026, 11, 17, 9, 40, 2))  # the 09:35 bar is past the end of the market
    assert not h.trader.on_bar_close()  # the newest closed bar is not there: retry


def test_kill_switch_closes_everything_and_stops_trading(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 8, 5))
    reply = h.trader.handle(Command("stop", "", 1))
    assert reply.startswith("🛑 Kill switch: 1 position(s) closed, 0 order(s) cancelled")
    assert h.trader.trading == STOPPED
    (reason,) = db.query("SELECT exit_reason FROM paper_trades")[0]
    assert reason == "manual"
    assert "closed (manual)" in h.text()
    assert h.trader.handle(Command("resume", "", 1)) == "▶️ Trading resumed."
    assert h.trader.trading == ACTIVE


def test_paused_trading_only_alerts(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert h.trader.handle(Command("pause", "", 1)).startswith("⏸ Paused")
    h.run_until(utc(2026, 11, 17, 8, 0))
    (alert,) = h.notes.sent
    assert alert.startswith("🟡") and "Action: alert only: trading is paused" in alert
    assert db.query("SELECT action FROM signals") == [("alert",)]
    assert db.query("SELECT COUNT(*) FROM paper_trades") == [(0,)]
    assert h.trader.trading == PAUSED
    assert h.trader.handle(Command("bogus", "", 1)).startswith("Commands:")
    assert "trading paused" in h.trader.handle(Command("status", "", 1))


def test_risk_blocked_setups_are_reported(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.trader.executor.book.manager.order_placed()  # an order already holds the only slot
    h.run_until(utc(2026, 11, 17, 7, 55))
    (alert,) = h.notes.sent
    assert alert.startswith("⛔") and "Action: blocked: 1 position(s) or entry order(s)" in alert
    quiet = settings.config.model_copy(
        update={"alerts": settings.config.alerts.model_copy(update={"send_blocked": False})}
    )
    import dataclasses

    other = Harness(dataclasses.replace(settings, config=quiet), Database(":memory:"))
    other.trader.executor.book.manager.order_placed()
    other.run_until(utc(2026, 11, 17, 7, 55))
    assert other.notes.sent == []  # stored, not sent
    other.db.close()


def test_auto_trade_off_and_high_threshold(settings: Settings, db: Database) -> None:
    import dataclasses

    config = settings.config
    off = config.model_copy(
        update={"execution": config.execution.model_copy(update={"auto_trade": False})}
    )
    h = Harness(dataclasses.replace(settings, config=off), db)
    h.run_until(utc(2026, 11, 17, 7, 55))
    assert "Action: alert only: auto_trade is off" in h.text()
    strict = config.model_copy(
        update={
            "strategy": config.strategy.model_copy(
                update={"alert_threshold": 95, "auto_trade_threshold": 98}
            )
        }
    )
    with Database(":memory:") as other_db:
        quiet = Harness(dataclasses.replace(settings, config=strict), other_db)
        quiet.run_until(utc(2026, 11, 17, 7, 55))
        assert quiet.notes.sent == []
        assert other_db.query("SELECT action FROM signals") == [("below_alert",)]


def test_a_restart_never_trades_the_same_raid_twice(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 7, 55))
    db.set_state("paper.last_bar", None)  # as if the bar state were lost
    db.set_state(STATE_KEY, None)
    h.clock.set(utc(2026, 11, 17, 7, 55, 2))
    h.trader = h.make()
    assert h.trader.on_bar_close()
    rows = db.query("SELECT id, action FROM signals ORDER BY id")
    assert rows == [("2026-11-17-LDN-01", "traded"), ("2026-11-17-LDN-02", "duplicate")]


def test_paper_trader_refuses_live_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").replace("mode: PAPER", "mode: LIVE"),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODE", "LIVE")
    live = load_settings(config, read_env_file=False, data_dir=tmp_path, log_dir=tmp_path)
    out = io.StringIO()
    assert run_paper(live, use_mock=True, out=out) == 2
    assert "start live trading with  python main.py --live" in out.getvalue()
    with Database(":memory:") as memory, pytest.raises(RuntimeError, match="PAPER"):
        PaperTrader(live, MockBroker(clock=ManualClock(START)), memory, ConsoleNotifier())


# --------------------------------------------------------------------------- #
# News jobs and the daily summary
# --------------------------------------------------------------------------- #

FF_RELEASE = [
    {
        "title": "Core CPI m/m",
        "country": "USD",
        "date": "2026-11-17T09:00:00-05:00",
        "impact": "High",
        "forecast": "0.3%",
        "previous": "0.3%",
    },
]


def news_desk(settings: Settings, db: Database, clock: ManualClock) -> NewsDesk:
    from goldsweep.ai.news_analyzer import NewsAnalyzer
    from goldsweep.ai.scenarios import ScenarioBuilder
    from goldsweep.data.news import NewsService

    news_cfg = settings.config.news
    web = FakeWeb({ForexFactoryProvider.URL: json.dumps(FF_RELEASE).encode()})
    calendar = CalendarService(
        [(ForexFactoryProvider(fetch=web), CalendarSourceConfig(name="forexfactory"))],
        db,
        news_cfg,
        clock=clock,
    )
    return NewsDesk(
        settings,
        calendar,
        NewsService([], db, news_cfg, clock=clock),
        NewsAnalyzer(news_cfg, db, client=None, model="m", clock=clock),
        ScenarioBuilder(news_cfg, db, client=None, model="m", clock=clock),
    )


def test_pre_news_warning_is_sent_once(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.trader._news = news_desk(settings, db, h.clock)  # noqa: SLF001
    h.clock.set(utc(2026, 11, 17, 13, 50, 2))  # US CPI at 14:00 UTC
    h.trader.news_tick()
    (warning,) = [m for m in h.notes.sent if m.startswith("⚠️")]
    assert warning.startswith("⚠️ USD Core CPI m/m in 9m (14:00 UTC) (forecast 0.3%")
    assert "No new entries 13:30-14:15 UTC." in warning
    h.trader.news_tick()
    assert len([m for m in h.notes.sent if m.startswith("⚠️")]) == 1  # warned once
    h.clock.set(utc(2026, 11, 17, 13, 30, 2))  # too early for the next one: nothing


def test_scenario_brief_when_the_market_is_known(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.trader._news = news_desk(settings, db, h.clock)  # noqa: SLF001
    h.trader.news_tick()  # no market seen yet: no brief
    assert not [m for m in h.notes.sent if m.startswith("🧭")]
    h.run_until(utc(2026, 11, 17, 7, 55))
    h.clock.set(utc(2026, 11, 17, 12, 40, 2))  # 20 minutes before the New York kill zone
    h.trader.news_tick()
    briefs = [m for m in h.notes.sent if m.startswith("🧭")]
    assert len(briefs) == 1 and "New York kill zone opening 13:00 UTC (template)" in briefs[0]
    h.trader.news_tick()
    assert len([m for m in h.notes.sent if m.startswith("🧭")]) == 1  # built once


def test_daily_summary(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 8, 40))
    h.notes.sent.clear()
    h.trader.daily_summary()
    (summary,) = h.notes.sent
    assert summary.startswith("📊 Daily summary 2026-11-17 (PAPER)")
    assert "Trades: 1 (1 won, 0 lost) | net +$227.51 (+4.77 R) | equity 10,227.51" in summary
    assert "Signals: traded 1" in summary
    assert "losses are part of the plan" in summary


# --------------------------------------------------------------------------- #
# The trade plan guard (spec section 10: no order without SL and TP)
# --------------------------------------------------------------------------- #


def plan(**changes: Any) -> TradePlan:
    from goldsweep.analysis.structure import Direction

    values: dict[str, Any] = {
        "id": "P",
        "time": START,
        "direction": Direction.LONG,
        "session": "london",
        "entry_mode": "fvg_mid",
        "entry": 2029.0,
        "stop_loss": 2027.0,
        "take_profit_1": 2033.0,
        "take_profit_2": 2037.0,
        "expires_at": START + 6 * FIVE,
    }
    values.update(changes)
    return TradePlan(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_loss": float("nan")},
        {"take_profit_1": float("inf")},
        {"stop_loss": 2030.0},  # above a long's entry
        {"take_profit_1": 2028.0},  # below a long's entry
        {"take_profit_2": 2020.0},
        {"stop_loss": None},
    ],
)
def test_no_order_without_a_stop_loss_and_take_profit(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidPlan):
        plan(**changes)


def test_every_order_the_book_holds_has_both(settings: Settings) -> None:
    from goldsweep.data.mock_broker import default_gold_spec

    config = settings.config
    book = TradeBook(
        default_gold_spec(),
        config.risk,
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=10_000.0,
        slippage_points=10,
        commission_per_lot=7.0,
        trail_after_tp1=True,
    )
    book.submit(plan(), spread_points=20, median_spread_points=20)
    for order in book.orders:
        assert order.stop_loss < order.entry < order.take_profit_1
    restored = TradeBook(
        default_gold_spec(),
        config.risk,
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=1.0,
        slippage_points=10,
        commission_per_lot=7.0,
        trail_after_tp1=True,
    )
    restored.restore(book.to_state())
    assert restored.orders == book.orders and restored.manager.equity == 10_000.0
    assert restored.manager.open_positions == 1


def test_cli_routes_paper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("goldsweep.live.run_paper", lambda s, **kw: calls.append(kw) or 0)
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["--paper", "--mock", "--no-llm"]) == 0
    assert calls == [{"use_mock": True, "use_llm": False}]


def test_run_paper_starts_and_stops(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs: list[str] = []

    class FakeScheduler:
        def __init__(self, **_kw: Any) -> None:
            pass

        def add_job(self, func: Any, trigger: Any, **_kw: Any) -> None:
            jobs.append(func.__name__)

        def start(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr("apscheduler.schedulers.blocking.BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(
        "goldsweep.live.NewsDesk.from_settings",
        classmethod(lambda cls, s, db, clock, use_llm=True: None),
    )
    out = io.StringIO()
    assert run_paper(settings, use_mock=True, use_llm=False, out=out) == 0
    text = out.getvalue()
    assert "🚀 GoldSweep PAPER started: XAUUSD, equity 10,000.00, trading active." in text
    assert "No real orders are sent." in text and "Telegram not configured" in text
    assert "⏹ GoldSweep PAPER stopped." in text
    assert jobs == [
        "bar_job",
        "news_tick",
        "daily_summary",
        "dashboard_job",
        "heartbeat_job",
        "housekeeping_job",
    ]

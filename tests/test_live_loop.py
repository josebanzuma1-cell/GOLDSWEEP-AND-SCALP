"""The live loop (Phase 6): signals become MT5 orders, the startup checks, the panel file.

Market data comes from the hand-made Phase 2 market (the mock broker); orders go
to a fake MT5 trade server whose prices the test sets.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from goldsweep import cli
from goldsweep.alerts.telegram import Command, ConsoleNotifier
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from goldsweep.data.mock_broker import MockBroker
from goldsweep.data.mt5_client import MT5Client
from goldsweep.execution.live_executor import MT5Executor, order_logger
from goldsweep.execution.mt5_gateway import MT5Gateway
from goldsweep.execution.panel import PanelWriter
from goldsweep.live import Trader, confirm_live, run_live
from goldsweep.storage.db import Database
from tests.fake_mt5 import gold_symbol
from tests.fake_mt5_trading import FakeTradingMT5
from tests.helpers import utc
from tests.test_paper import FIVE, MARKET, START

LOGIN = 5012345


def fake_terminal(clock: ManualClock, **kwargs: Any) -> tuple[FakeTradingMT5, MT5Client]:
    fake = FakeTradingMT5(clock, symbols=[gold_symbol()], **kwargs)
    client = MT5Client(mt5=fake, clock=clock, sleep=lambda _s: None, server_timezone="+02:00")
    return fake, client


class LiveHarness:
    """The trader with the MT5 executor, on the hand-made market."""

    def __init__(self, settings: Settings, db: Database, tmp_path: Path) -> None:
        self.clock = ManualClock(START)
        self.market = MockBroker(clock=self.clock, candles_m1=MARKET)
        self.market.connect()
        self.fake, self.client = fake_terminal(self.clock)
        self.client.connect()
        self.fake.quote(2031.00)
        execution = settings.config.execution
        gateway = MT5Gateway(
            self.client,
            self.client.resolve_symbol(["XAUUSD"]),
            magic=execution.magic_number,
            deviation_points=execution.deviation_points,
            order_log=order_logger(db, self.clock),
            sleep=lambda _s: None,
        )
        self.executor = MT5Executor(settings, gateway, db, clock=self.clock, demo=True)
        self.notes = ConsoleNotifier(io.StringIO())
        self.panel = PanelWriter(tmp_path / "panel" / "signals.json", self.chart_time, 3.0, 10, 2)
        self.trader = Trader(
            settings,
            self.market,
            db,
            self.notes,
            clock=self.clock,
            executor=self.executor,
            panel=self.panel,
        )

    @staticmethod
    def chart_time(moment: dt.datetime) -> int:
        return int(moment.timestamp()) + 7200  # the fake server runs on UTC+2

    def run_until(self, moment: dt.datetime) -> None:
        while self.clock.now() <= moment + dt.timedelta(seconds=2):
            assert self.trader.on_bar_close()
            self.clock.advance(FIVE)


def test_a_signal_becomes_real_orders_with_stops(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h = LiveHarness(settings, db, tmp_path)
    assert h.trader.label == "LIVE DEMO" and h.trader.live
    h.run_until(utc(2026, 11, 17, 7, 55))
    (alert,) = h.notes.sent
    assert "Mode: LIVE DEMO  |  id: 2026-11-17-LDN-01" in alert
    assert (
        "Action: sent to MT5: 0.17 lots in 2 position(s) (limit 2028.90), 47.75 at risk; "
        "valid until 09:55 UTC" in alert  # entries wait 24 bars (D82)
    )
    orders = sorted(h.fake.pending_orders.values(), key=lambda o: o.ticket)
    assert [(o.volume_current, o.price_open, o.sl, o.tp) for o in orders] == [
        (0.08, 2028.9, 2026.09, 2036.0),
        (0.09, 2028.9, 2026.09, 2048.0),
    ]
    assert db.query("SELECT action FROM signals") == [("traded",)]

    panel = json.loads((tmp_path / "panel" / "signals.json").read_text(encoding="ascii"))
    (shown,) = panel["signals"]
    assert (panel["mode"], shown["id"], shown["dir"], shown["status"]) == (
        "LIVE DEMO",
        "2026-11-17-LDN-01",
        "BUY",
        "waiting",
    )
    assert (shown["entry"], shown["sl"], shown["tp1"], shown["tp2"]) == (
        2028.9,
        2026.09,
        2036.0,
        2048.0,
    )

    h.fake.quote(2028.60)  # the ask reaches the limit: the broker fills both parts
    h.trader.sync()
    filled = h.notes.sent[-1]
    assert filled.startswith("✅ LIVE DEMO LONG 2026-11-17-LDN-01 filled at 2028.90, 0.17 lots")
    assert "in 2 position(s)" in filled and "TP2 2048.00" in filled

    reply = h.trader.handle(Command("stop", "", 1))
    assert reply.startswith("🛑 Kill switch: 1 position(s) closed, 0 order(s) cancelled.")
    assert h.fake.open_positions == {} and h.trader.trading == "stopped"
    assert "closed (manual)" in h.notes.sent[-1]
    assert "GoldSweep LIVE DEMO | trading stopped" in h.trader.status()


# --------------------------------------------------------------------------- #
# Startup: two switches, a demo account, Algo Trading, the typed confirmation
# --------------------------------------------------------------------------- #


def live_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **execution: Any) -> Settings:
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").replace("mode: PAPER", "mode: LIVE")
    for key, value in execution.items():
        text = text.replace(f"  {key}: false", f"  {key}: {str(value).lower()}")
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    monkeypatch.setenv("MODE", "LIVE")
    monkeypatch.setattr(
        "goldsweep.live.NewsDesk.from_settings",
        classmethod(lambda cls, s, db, clock, use_llm=True: None),
    )
    return load_settings(config, read_env_file=False, data_dir=tmp_path / "data", log_dir=tmp_path)


def start(settings: Settings, answer: str, **fake_options: Any) -> tuple[int, str, FakeTradingMT5]:
    clock = ManualClock(utc(2026, 11, 18, 8, 0))
    fake, client = fake_terminal(clock, **fake_options)
    fake.quote(2650.0)
    out = io.StringIO()
    code = run_live(
        settings,
        use_llm=False,
        out=out,
        ask=lambda _prompt: answer,
        broker_factory=lambda s, c, d: client,
        run_scheduler=False,
    )
    return code, out.getvalue(), fake


def test_live_needs_both_switches(settings: Settings) -> None:
    out = io.StringIO()
    assert run_live(settings, out=out, run_scheduler=False) == 2
    assert "needs MODE=LIVE in .env AND mode: LIVE in config.yaml" in out.getvalue()


def test_the_typed_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = live_settings(tmp_path, monkeypatch)
    code, text, fake = start(settings, "yes")
    assert code == 3 and "Not confirmed: no orders were sent." in text
    assert f"Type  LIVE {LOGIN}  and press Enter" in text
    assert "Account   5012345 @ Demo-Server (Demo Broker Ltd) - DEMO" in text
    assert fake.count("order_send") == 0
    code, text, _fake = start(settings, f" LIVE {LOGIN} ")
    assert code == 0
    assert f"GoldSweep LIVE DEMO started: XAUUSD on account {LOGIN}" in text


def test_a_real_account_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = live_settings(tmp_path, monkeypatch)
    code, text, _fake = start(settings, f"LIVE {LOGIN}", account_trade_mode=2)
    assert code == 2 and "is a REAL account. Phase 6 trades demo accounts only" in text
    allowed = live_settings(tmp_path, monkeypatch, allow_real_account=True)
    code, text, _fake = start(allowed, f"LIVE {LOGIN}", account_trade_mode=2)
    assert code == 0 and "GoldSweep LIVE started" in text


def test_algo_trading_must_be_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = live_settings(tmp_path, monkeypatch)
    clock = ManualClock(utc(2026, 11, 18, 8, 0))
    fake, client = fake_terminal(clock)
    fake.terminal.trade_allowed = False
    out = io.StringIO()
    code = run_live(
        settings,
        out=out,
        ask=lambda _p: f"LIVE {LOGIN}",
        broker_factory=lambda s, c, d: client,
        run_scheduler=False,
    )
    assert code == 2 and "Algo Trading is off in MetaTrader 5" in out.getvalue()


def test_end_of_input_is_not_a_yes(settings: Settings) -> None:
    def closed(_prompt: str) -> str:
        raise EOFError

    account = type(
        "A",
        (),
        {
            "login": 1,
            "server": "s",
            "company": "c",
            "balance": 1.0,
            "currency": "USD",
            "leverage": 100,
            "account_type": type("T", (), {"value": "DEMO"})(),
        },
    )()
    spec = type("S", (), {"name": "XAUUSD"})()
    assert not confirm_live(settings, account, spec, ask=closed, out=io.StringIO())


def test_cli_routes_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_live(settings: Settings, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("goldsweep.live.run_live", fake_run_live)
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["--live", "--no-llm"]) == 0
    assert calls == [{"use_llm": False, "unattended": False}]
    assert cli.main(["--live", "--mock"]) == 2  # live trading never runs on the mock


# --------------------------------------------------------------------------- #
# The panel file
# --------------------------------------------------------------------------- #


def test_panel_file_layout(db: Database, tmp_path: Path) -> None:
    db.execute(
        "INSERT INTO signals (id, time, direction, session, score, entry, stop_loss, "
        "take_profit_1, take_profit_2, risk_reward, action, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-11-18-NY-01",
            int(utc(2026, 11, 18, 13, 20).timestamp()),
            "SHORT",
            "newyork",
            77.0,
            2650.0,
            2652.0,
            2646.0,
            2645.0,
            2.0,
            "alert",
            json.dumps({"pool": "Asian high", "pool_price": 2651.5}),
        ),
    )
    writer = PanelWriter(
        tmp_path / "GoldSweep" / "signals.json", LiveHarness.chart_time, 3.0, 10, 2
    )
    now = utc(2026, 11, 18, 13, 25)
    assert writer.write(
        db,
        now=now,
        mode="PAPER",
        state="active",
        pressure="Buyers 40% / Sellers 60%",
        news_bias=0.25,
    )
    text = writer.path.read_text(encoding="ascii")
    lines = text.splitlines()
    assert lines[0].startswith('{"version":1,"mode":"PAPER","state":"active"')
    assert lines[1].startswith('{"id":"2026-11-18-NY-01"')  # one signal per line
    data = json.loads(text)
    (signal,) = data["signals"]
    assert signal["t"] == int(utc(2026, 11, 18, 15, 20).timestamp())  # chart (server) time
    assert (signal["dir"], signal["tp3"], signal["pool"], signal["status"]) == (
        "SELL",
        2644.0,
        2651.5,
        "alert",
    )
    assert data["updated_utc"] == "2026-11-18 13:25 UTC" and data["news_bias"] == 0.25


def test_panel_file_uses_windows_line_ends(db: Database, tmp_path: Path) -> None:
    writer = PanelWriter(tmp_path / "signals.json", LiveHarness.chart_time, 3.0, 10, 2)
    assert writer.write(
        db, now=utc(2026, 11, 18, 8, 0), mode="PAPER", state="active", pressure=None, news_bias=None
    )
    raw = writer.path.read_bytes()
    assert raw.endswith(b"]}\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    assert json.loads(raw)["signals"] == []


def test_a_netting_account_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = live_settings(tmp_path, monkeypatch)
    clock = ManualClock(utc(2026, 11, 18, 8, 0))
    fake, client = fake_terminal(clock)
    fake.account.margin_mode = 0  # ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    out = io.StringIO()
    code = run_live(
        settings,
        out=out,
        ask=lambda _p: f"LIVE {LOGIN}",
        broker_factory=lambda s, c, d: client,
        run_scheduler=False,
    )
    assert code == 2 and "is a netting account" in out.getvalue()
    assert fake.count("order_send") == 0

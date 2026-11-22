"""The scalper on the chart, on Telegram and on the dashboard (S7, D99, D113).

``/stats`` and the dashboard's Scalper tab read the stored scalps per mode and account;
the bot writes ``scalp.json`` for the chart panel each scalper minute and carries out
the panel's buttons from ``scalp_commands.json``. The scalps come from the hand-made
M1 market of tests/test_scalper_runner.py.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from goldsweep.alerts.telegram import ConsoleNotifier
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import Settings
from goldsweep.dashboard import data
from goldsweep.data.mock_broker import MockBroker
from goldsweep.execution.panel import PanelWriter
from goldsweep.live import ACTIVE, PAUSED, Trader
from goldsweep.scalping import stats
from goldsweep.scalping.chart import PANEL_COMMANDS, ScalpPanel
from goldsweep.scalping.engine import ScalpEngine
from goldsweep.scalping.logic import ChartMarks
from goldsweep.storage.db import Database
from tests.helpers import utc
from tests.test_scalp_engine import SELL_ROWS, START, config
from tests.test_scalper_runner import M1, MINUTE, SIGNAL_BAR, Harness, ScriptedEngine, setup_at

EPOCH = utc(2026, 11, 17)


def chart_time(moment: dt.datetime) -> int:
    return int(moment.timestamp()) + 7200  # a broker clock two hours ahead


# --------------------------------------------------------------------------- #
# Results per mode
# --------------------------------------------------------------------------- #


def store(db: Database, table: str, scalp_id: str, r: float, pnl: float, *, hours: int = 1) -> None:
    closed = int((EPOCH + dt.timedelta(hours=hours)).timestamp())
    legs = [{"lots": 0.05, "price": 2031.0, "time": EPOCH.isoformat(), "reason": "tp1", "pnl": 5.0}]
    db.execute(
        f"INSERT INTO {table} (id, direction, session, score, signal_time, fill_time, exit_time, "
        "fill_price, stop_loss, take_profit_1, take_profit_2, lots, risk_amount, exit_reason, "
        "net_pnl, r_multiple, legs) VALUES (?, 'LONG', 'london', 60, ?, ?, ?, 2030.0, 2028.0, "
        "2031.0, 2032.0, 0.12, 25.0, ?, ?, ?, ?)",
        (scalp_id, closed - 300, closed - 240, closed, "tp2" if r > 0 else "stop", pnl, r,
         json.dumps(legs)),
    )  # fmt: skip


def test_results_per_mode_and_account(db: Database) -> None:
    store(db, "scalp_trades", "2026-11-17-HFT-001", 1.0, 25.0)
    store(db, "scalp_trades", "2026-11-17-HFT-002", -1.0, -25.0, hours=2)
    store(db, "scalp_trades", "2026-11-17-HFT-003", 0.5, 12.5, hours=3)
    store(db, "scalp_live_trades", "2026-11-17-INT-001", 2.0, 40.0)
    assert stats.mode_of("2026-11-17-SWS-004") == "swing" and stats.mode_of("x") == "?"
    found = stats.closed_scalps(db, EPOCH, EPOCH + dt.timedelta(days=1))
    assert [(s.id, s.mode, s.where) for s in found][:2] == [
        ("2026-11-17-HFT-001", "hft", "paper"),
        ("2026-11-17-INT-001", "intraday", "demo"),
    ]
    assert found[0].take_profit_3 == pytest.approx(2033.0)  # equal ladder steps
    hft, intraday = stats.by_mode(found)
    assert (hft.label, hft.trades, hft.wins, hft.losses) == ("HFT, paper", 3, 2, 1)
    assert hft.win_rate == pytest.approx(2 / 3) and hft.total_r == pytest.approx(0.5)
    # Average win 0.75 R, average loss 1 R: break-even at 1 / 1.75 of trades won.
    assert hft.breakeven_win_rate == pytest.approx(1 / 1.75)
    assert hft.profit_factor == pytest.approx(37.5 / 25.0) and hft.backtest_r == -0.12
    assert intraday.label == "intraday, demo" and intraday.breakeven_win_rate is None
    line = stats.stats_line(hft)
    assert line == (
        "HFT, paper: 3 scalps, won 67% (break-even 57%), PF 1.50, +0.50 R "
        "(+0.17 R each; backtest -0.12), +$12.50"
    )
    message = stats.stats_message(7, [hft], stats.summarize("Main strategy (PAPER)", []))
    assert message.splitlines() == [
        "📈 Results, last 7 days (after costs)",
        f"Scalper {line}",
        "Main strategy (PAPER): no trades",
        stats.DISCLAIMER,
    ]


def test_stats_on_telegram(settings: Settings, db: Database) -> None:
    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    assert "Scalper: no scalps closed in this period." in h.say("stats")
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))
    reply = h.say("stats", "1")
    assert reply.startswith("📈 Results, last 1 day (after costs)")
    assert "Scalper HFT, paper: 1 scalps, won 100% (break-even n/a)" in reply
    assert "Main strategy (PAPER): no trades" in reply
    assert h.say("stats", "a year") == (
        "Send /stats for the last 30 days, or /stats <days> (1 to 365)."
    )
    assert h.say("stats", "0").startswith("Send /stats")


# --------------------------------------------------------------------------- #
# scalp.json
# --------------------------------------------------------------------------- #


def panel_lines(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = path.read_bytes()
    raw.decode("ascii")  # MQL5 reads it as ANSI text
    text = raw.decode("ascii")
    assert "\r\n" in text
    lines = text.split("\r\n")
    head = json.loads(lines[0][: lines[0].index(',"items":[')] + "}")
    items = [json.loads(line.rstrip(",")) for line in lines[1:] if line.startswith("{")]
    assert json.loads(text) is not None  # the whole file is valid JSON too
    return head, items


def test_the_panel_file(tmp_path: Path) -> None:
    panel = ScalpPanel(tmp_path / "GoldSweep", chart_time, digits=2)
    now = utc(2026, 11, 17, 8, 5)
    marks = ChartMarks(
        "M1",
        "DOWN",
        1.8,
        ((utc(2026, 11, 17, 8, 0), 2031.456, -1), (utc(2026, 11, 17, 8, 1), 2031.2, -1)),
        ((utc(2026, 11, 17, 7, 58), 2032.4, "high"),),
    )
    position = {
        "id": "2026-11-17-HFT-002",
        "direction": "SHORT",
        "lots": 0.08,
        "fill_price": 2030.0,
        "stop": 2030.1,
        "targets": [2028.0],
        "risk_amount": 25.0,
        "stop_loss": 2032.0,
        "take_profits": [2029.0, 2028.0, 2027.0],
        "fill_time": utc(2026, 11, 17, 8, 2).isoformat(),
        "mode": "hft",
        "where": "paper",
    }
    closed = stats.ClosedScalp(
        "2026-11-17-HFT-001", "hft", "paper", "LONG", "london", EPOCH, EPOCH,
        utc(2026, 11, 17, 1), 2030.0, 2028.0, 2031.0, 2032.0, 0.12, "tp2", 30.0, 1.2,
        ({"price": 2031.0, "time": EPOCH.isoformat(), "reason": "tp1"},
         {"price": 2032.0, "time": utc(2026, 11, 17, 1).isoformat(), "reason": "tp2"}),
    )  # fmt: skip
    summary = stats.summarize("HFT", [closed])
    assert panel.write(
        now=now,
        mode="hft",
        where="paper",
        state="active",
        strategies="both",
        status="IN TRADE",
        marks=marks,
        open_scalps=[position],
        closed=[closed],
        today=summary,
        month=summary,
        session="london",
        spread=24.0,
        spread_median=22.0,
        news="USD CPI in 1h05m",
        price=2029.0,
    )
    head, items = panel_lines(panel.path)
    assert (head["version"], head["mode"], head["where"], head["status"]) == (
        2,
        "hft",
        "PAPER",
        "IN TRADE",
    )
    assert head["updated"] == chart_time(now) and head["updated_gmt"] == int(now.timestamp())
    assert (head["trend"], head["atr"], head["session"], head["news"]) == (
        "DOWN",
        1.8,
        "london",
        "USD CPI in 1h05m",
    )
    assert (head["today_n"], head["today_r"], head["today_net"]) == (1, 1.2, 30.0)
    # The active scalp: one of three parts left (TP1 and TP2 reached), 0.5 R up, 3 min in.
    assert (head["active"], head["active_dir"], head["active_hit"]) == (
        "2026-11-17-HFT-002",
        "SELL",
        2,
    )
    assert (head["active_children"], head["active_r"], head["active_min"]) == ("1/3", 0.5, 3)
    kinds = [item["k"] for item in items]
    assert kinds == ["step", "step", "x", "scalp", "hit", "hit", "scalp"]
    step, _, sweep, done, hit1, hit2, live = items
    assert step == {"k": "step", "t": chart_time(utc(2026, 11, 17, 8)), "v": 2031.46, "up": 0}
    assert sweep == {"k": "x", "t": chart_time(utc(2026, 11, 17, 7, 58)), "p": 2032.4, "s": "high"}
    assert (done["status"], done["dir"], done["r"], done["exit"], done["tp3"]) == (
        "closed",
        "BUY",
        1.2,
        "tp2",
        2033.0,
    )
    assert done["end"] == chart_time(utc(2026, 11, 17, 1))
    assert (hit1["tp"], hit2["tp"], hit2["p"]) == ("TP1", "TP2", 2032.0)
    assert (live["status"], live["sl"], live["stop"], live["tp3"], live["end"]) == (
        "open",
        2032.0,
        2030.1,
        2027.0,
        0,
    )


def test_the_panel_buttons_file(tmp_path: Path) -> None:
    panel = ScalpPanel(tmp_path, chart_time)
    assert panel.read_commands() == []
    panel.commands_path.write_text(
        '{"id":17,"t":1763366700,"cmd":"Pause"}\r\nnot json\r\n{"id":"x"}\r\n', encoding="ascii"
    )
    (command,) = panel.read_commands()
    assert (command.id, command.name) == (17, "pause")
    assert command.time == dt.datetime.fromtimestamp(1763366700, tz=dt.UTC)
    assert PANEL_COMMANDS == ("pause", "resume", "scalper:off")  # never an order


class PanelHarness(Harness):
    """The paper bot with the chart panel files in a temporary folder."""

    def __init__(self, settings: Settings, db: Database, tmp_path: Path, setups: Any) -> None:
        self.db = db
        self.clock = ManualClock(utc(2026, 11, 17, 7, 30, 2))
        self.market = MockBroker(clock=self.clock, candles_m1=M1)
        self.market.connect()
        self.notes = ConsoleNotifier(io.StringIO())
        self.folder = tmp_path / "Common" / "GoldSweep"
        writer = PanelWriter(self.folder / "signals.json", chart_time, 3.0, 10, 2)
        self.trader = Trader(
            settings,
            self.market,
            db,
            self.notes,
            clock=self.clock,
            starting_equity=10_000.0,
            panel=writer,
        )
        self.engine = ScriptedEngine(setups)
        self.hft = self.trader.scalper.slots["hft"]
        self.hft.logic.engine = self.engine  # type: ignore[attr-defined]


def test_the_bot_writes_the_panel_file_each_minute(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h = PanelHarness(settings, db, tmp_path, [setup_at(SIGNAL_BAR)])
    path = h.folder / "scalp.json"
    h.run_until(utc(2026, 11, 17, 7, 40))
    head, items = panel_lines(path)
    assert (head["mode"], head["status"], head["active"]) == ("off", "OFF", "")
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 1))  # the scalp is open
    head, items = panel_lines(path)
    assert (head["mode"], head["status"], head["active"]) == (
        "hft",
        "IN TRADE",
        "2026-11-17-HFT-001",
    )
    assert head["updated_utc"] == "2026-11-17 08:01 UTC"
    (live,) = [item for item in items if item["k"] == "scalp"]
    assert (live["status"], live["dir"], live["sl"]) == ("open", "BUY", 2028.1)
    h.run_until(utc(2026, 11, 17, 8, 10))  # closed at TP3
    head, items = panel_lines(path)
    assert (head["status"], head["today_n"]) == ("READY", 1)
    hits = [item["tp"] for item in items if item["k"] == "hit"]
    assert hits == ["TP1", "TP2", "TP3"]


def test_the_panel_buttons_reach_the_bot(settings: Settings, db: Database, tmp_path: Path) -> None:
    h = PanelHarness(settings, db, tmp_path, [])
    now = h.clock.now()
    h.folder.mkdir(parents=True, exist_ok=True)
    buttons = h.folder / "scalp_commands.json"

    def press(command_id: int, name: str, when: dt.datetime = now) -> None:
        buttons.write_text(
            json.dumps({"id": command_id, "t": int(when.timestamp()), "cmd": name}) + "\r\n",
            encoding="ascii",
        )
        h.trader.dashboard_job()

    press(1, "pause")
    assert h.trader.trading == PAUSED
    assert (
        h.notes.sent[-1]
        == "📊 Chart /pause: ⏸ Paused: no new trades. Open trades are still managed."
    )
    press(1, "resume")  # the same command id is carried out once only
    assert h.trader.trading == PAUSED
    press(2, "resume", now - dt.timedelta(minutes=5))  # too old
    assert h.trader.trading == PAUSED
    press(3, "stop")  # the kill switch is not on the chart
    assert h.trader.trading == PAUSED
    press(4, "resume")
    assert h.trader.trading == ACTIVE
    h.say("scalper", "hft")
    press(5, "scalper:off")
    assert h.trader.scalper_mode == "off"
    assert h.notes.sent[-1].startswith("📊 Chart /scalper off: ✅ Scalper off")


# --------------------------------------------------------------------------- #
# What the chart draws: sweeps and the step line
# --------------------------------------------------------------------------- #


def test_the_engine_keeps_its_sweeps_and_the_hft_mode_its_step_line(
    settings: Settings, db: Database
) -> None:
    engine = ScalpEngine(config(), bar=MINUTE)
    for i, row in enumerate(SELL_ROWS):
        engine.on_bar(START + i * MINUTE, *row)
    swept = ((utc(2026, 11, 17, 7, 36), 2005.6, "low"), (utc(2026, 11, 17, 7, 37), 2007.8, "high"))
    assert tuple(engine.sweeps) == swept  # a micro low at 07:36, the swing high at 07:37
    h = Harness(settings, db)
    logic = h.trader.scalper.slots["hft"].logic
    logic.engine = ScalpEngine(config(), bar=MINUTE)  # type: ignore[attr-defined]
    for i, row in enumerate(SELL_ROWS):
        logic.on_bar(START + i * MINUTE, *row)
    marks = logic.chart()
    assert (marks.timeframe, marks.trend) == ("M1", "DOWN")
    assert marks.line and all(trend == -1 for _, _, trend in marks.line)
    assert marks.sweeps == swept
    for mode, timeframe in (("intraday", "M5"), ("swing", "M15")):
        assert h.trader.scalper.slots[mode].logic.chart().timeframe == timeframe


# --------------------------------------------------------------------------- #
# The dashboard's Scalper tab
# --------------------------------------------------------------------------- #


def test_the_dashboard_reads_the_scalps(db: Database) -> None:
    store(db, "scalp_trades", "2026-11-17-HFT-001", 1.0, 25.0)
    store(db, "scalp_trades", "2026-11-17-HFT-002", -1.0, -25.0, hours=2)
    store(db, "scalp_live_trades", "2026-11-17-SWS-001", 2.0, 40.0, hours=3)
    closed = data.scalps(db, EPOCH)
    assert list(closed["mode"]) == ["hft", "hft", "swing"]
    table = data.scalp_stats(db, EPOCH)
    assert list(table["mode, account"]) == ["HFT, paper", "swing scalping, demo"]
    assert list(table["backtest R each"]) == [-0.12, -0.07]
    curve = data.cumulative_r(closed)
    assert list(curve.columns) == ["hft, paper", "swing, demo"]
    assert list(curve["hft, paper"]) == [1.0, 0.0, 0.0]  # carried forward
    assert data.cumulative_r(data.scalps(db, EPOCH + dt.timedelta(days=9))).empty


def test_the_scalper_tab_renders(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamlit.testing.v1 import AppTest

    from goldsweep.dashboard.launch import APP
    from goldsweep.live import HEARTBEAT_KEY

    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 10))
    now = dt.datetime.now(dt.UTC)
    db.set_state(HEARTBEAT_KEY, {"time": now.isoformat(), "mode": "PAPER"})
    monkeypatch.setenv("GOLDSWEEP_DASHBOARD_DB", str(db.path))
    monkeypatch.setenv("GOLDSWEEP_REPORTS_DIR", str(tmp_path / "reports"))
    page = AppTest.from_file(str(APP), default_timeout=120)
    page.run()
    assert not page.exception
    metrics = {m.label: m.value for m in page.metric}
    assert (metrics["Scalper mode"], metrics["Trades on"]) == ("hft", "paper")
    page.radio(key="scalp_days").set_value(90).run()  # the scalp closed in 2026-11
    assert not page.exception


def test_the_chart_indicator_only_writes_its_command_file() -> None:
    source = (Path(__file__).parents[1] / "mt5" / "GoldSweepPanel.mq5").read_text(encoding="utf-8")
    assert source.count("FILE_WRITE") == 1 and "InpCommandFile, FILE_WRITE" in source
    for word in ("OrderSend", "CTrade", "PositionClose", "TRADE_ACTION"):
        assert word not in source  # it never trades
    assert '"GoldSweep\\\\scalp.json"' in source and "STALE" in source

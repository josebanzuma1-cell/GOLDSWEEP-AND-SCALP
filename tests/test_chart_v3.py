"""The chart panel v3 (S8, D114): what the bot writes for MT5, and the indicator itself.

``signals.json`` v2 carries graded signals, setups forming (A+ possible or not), the
nearest pools, open gaps and the analysis; ``scalp.json`` v3 carries every scalper
mode's layer and the Quick Scalp decision; the buttons may switch strategies and
modes. The hand-made markets are those of tests/test_paper.py and
tests/test_scalper_runner.py.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from goldsweep.alerts.telegram import ConsoleNotifier
from goldsweep.core.clock import ManualClock, SessionCalculator
from goldsweep.core.settings import Settings
from goldsweep.data.mock_broker import MockBroker
from goldsweep.execution.panel import PanelWriter, ascii_text, grade_letter
from goldsweep.live import Trader
from goldsweep.scalping.bars import Candle
from goldsweep.scalping.chart import ScalpPanel
from goldsweep.scalping.intraday import IntradayEngine
from goldsweep.scalping.logic import ChartMarks, make_logic
from goldsweep.scalping.runner import Decision
from goldsweep.scalping.stats import summarize
from goldsweep.scalping.swing_scalp import SwingScalpEngine
from goldsweep.storage.db import Database
from tests.helpers import only_spec_model, utc
from tests.test_paper import MARKET, START
from tests.test_scalp_chart import PanelHarness, chart_time, panel_lines
from tests.test_scalper_runner import SIGNAL_BAR, setup_at

PROJECT = Path(__file__).parents[1]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return only_spec_model(settings)


# --------------------------------------------------------------------------- #
# signals.json v2
# --------------------------------------------------------------------------- #


def test_text_for_the_mql5_reader() -> None:
    assert ascii_text('Gold "spikes" — café\nnext\\line') == "Gold 'spikes' - cafe next/line"
    assert len(ascii_text("x" * 500)) == 240
    assert grade_letter("A+ (London playbook)") == "A+"
    assert (grade_letter("A"), grade_letter(None), grade_letter("")) == ("A", "", "")


def store_signal(db: Database, signal_id: str, action: str, **detail: Any) -> None:
    db.execute(
        "INSERT INTO signals (id, time, direction, session, score, entry, stop_loss, "
        "take_profit_1, take_profit_2, risk_reward, action, detail) "
        "VALUES (?, ?, 'LONG', 'london', 81.1, 2028.9, 2026.09, 2036.0, 2048.0, 2.5, ?, ?)",
        (signal_id, int(utc(2026, 11, 17, 7, 55).timestamp()), action, json.dumps(detail)),
    )


def test_the_signals_file_v2(db: Database, tmp_path: Path) -> None:
    store_signal(
        db,
        "2026-11-17-LDN-01",
        "traded",
        pool="Asian low",
        pool_price=2027.0,
        grade="A+ (London playbook)",
        model="sweep_mss",
        tags=["judas", "ote"],
        playbook="london",
        monitoring="monitored for the London A+ playbook",
        sweep_x=2026.4,
        sweep_t=utc(2026, 11, 17, 7, 35).isoformat(),
    )
    store_signal(db, "2026-11-17-LDN-02", "armed", pool="PDL", pool_price=2016.0, grade="A")
    writer = PanelWriter(tmp_path / "signals.json", chart_time, 3.0, 10, 2)
    now = utc(2026, 11, 17, 9, 0)
    assert writer.write(
        db,
        now=now,
        mode="LIVE DEMO",
        state="active",
        pressure="Buyers 58% / Sellers 42%",
        news_bias=0.25,
        armed_ids={"2026-11-17-LDN-02"},
        results={"2026-11-17-LDN-01": (2.1, "tp2", utc(2026, 11, 17, 8, 40))},
        analysis={"h1": "LONG", "h1_score": 0.42, "window": "London — open now"},
        forming=[
            {
                "since": utc(2026, 11, 17, 8, 50),
                "direction": "SHORT",
                "level": 2036.0,
                "label": "Asian high",
                "extreme": 2036.6,
                "stage": "swept",
                "text": "swept: waiting for the structure shift",
                "a_plus": True,
                "model": "sweep_mss",
            }
        ],
        pools=[
            {
                "price": 2048.0,
                "label": "PDH",
                "side": "above",
                "strength": 0.9,
                "created": utc(2026, 11, 17),
            }
        ],
        gaps=[
            {
                "time": utc(2026, 11, 17, 7, 45),
                "top": 2029.9,
                "bottom": 2027.9,
                "direction": "LONG",
            }
        ],
    )
    text = writer.path.read_bytes().decode("ascii")
    data = json.loads(text)
    assert (data["version"], data["h1"], data["window"]) == (2, "LONG", "London - open now")
    first, second, forming, pool, gap = data["signals"]
    # Every v1 key is still there, so an older indicator keeps working.
    assert {"id", "t", "dir", "score", "entry", "sl", "tp1", "tp2", "tp3", "pool", "status"} <= set(
        first
    )
    assert (first["grade"], first["tags"], first["playbook"], first["monitored"]) == (
        "A+",
        "judas,ote",
        "london",
        1,
    )
    assert (first["status"], first["r"], first["exit"]) == ("closed", 2.1, "tp2")
    assert first["end"] == chart_time(utc(2026, 11, 17, 8, 40))
    assert (first["sweep_x"], first["sweep_t"]) == (2026.4, chart_time(utc(2026, 11, 17, 7, 35)))
    assert (second["status"], second["grade"]) == ("armed", "A")
    assert (forming["k"], forming["dir"], forming["aplus"], forming["x"]) == (
        "forming",
        "SELL",
        1,
        2036.6,
    )
    assert (pool["k"], pool["label"], pool["side"], pool["str"]) == ("pool", "PDH", "above", 0.9)
    assert (gap["k"], gap["dir"], gap["top"], gap["bot"]) == ("fvg", "BUY", 2029.9, 2027.9)
    # One item per line, Windows line ends: the MQL5 reader's format.
    assert [line[:5] for line in text.split("\r\n")[1:6]] == [
        '{"id"',
        '{"id"',
        '{"k":',
        '{"k":',
        '{"k":',
    ]


def test_the_bot_marks_the_a_plus_raid_as_it_forms(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    clock = ManualClock(START)
    market = MockBroker(clock=clock, candles_m1=MARKET)
    market.connect()
    writer = PanelWriter(tmp_path / "signals.json", chart_time, 3.0, 10, 2)
    trader = Trader(
        settings,
        market,
        db,
        ConsoleNotifier(io.StringIO()),
        clock=clock,
        starting_equity=10_000.0,
        panel=writer,
    )
    while clock.now() <= utc(2026, 11, 17, 7, 40, 2):  # the 07:35 bar sweeps the Asian low
        assert trader.on_bar_close()
        clock.advance(dt.timedelta(minutes=5))
    data = json.loads(writer.path.read_text(encoding="ascii"))
    (forming,) = [item for item in data["signals"] if item.get("k") == "forming"]
    assert (forming["dir"], forming["level"], forming["stage"], forming["aplus"]) == (
        "BUY",
        2027.0,
        "swept",
        1,  # London, and the raid went beyond the New York midnight open
    )
    assert data["session"] == "london" and "London" in data["window"]
    assert any(item.get("k") == "pool" for item in data["signals"])
    while clock.now() <= utc(2026, 11, 17, 8, 0, 2):  # the signal at 07:55
        assert trader.on_bar_close()
        clock.advance(dt.timedelta(minutes=5))
    data = json.loads(writer.path.read_text(encoding="ascii"))
    assert not [item for item in data["signals"] if item.get("k") == "forming"]
    (signal,) = [item for item in data["signals"] if "id" in item]
    assert (signal["grade"], signal["sweep_x"]) == ("A+", 2026.4)


# --------------------------------------------------------------------------- #
# scalp.json v3: every mode's layer, the decision
# --------------------------------------------------------------------------- #


def test_the_mode_layers(settings: Settings, sessions: SessionCalculator) -> None:
    intraday = IntradayEngine(settings.config.scalper, sessions, bar=dt.timedelta(minutes=5))
    start = utc(2026, 11, 17, 0, 0)
    for i in range(30):  # Asia, then London: the VWAP starts at the London open
        moment = start + i * dt.timedelta(minutes=5) * 20
        intraday.on_bar(moment, 2030.0, 2031.0 + i * 0.1, 2029.0, 2030.5, 10.0)
    names = [name for name, *_ in intraday.ranges()]
    assert names[0] == "Asian range"
    swing = SwingScalpEngine(settings.config.scalper, sessions)
    for i in range(30):
        moment = start + i * dt.timedelta(minutes=15)
        swing.on_bar(Candle(moment, 2030.0, 2031.0, 2029.0, 2030.5, 1.0, 15), None)
    (name, t1, t2, high, low), london = swing.ranges()  # London began at 07:00
    assert (name, t1, t2, high, low) == (
        "Asian range",
        utc(2026, 11, 17, 0),
        utc(2026, 11, 17, 6),
        2031.0,
        2029.0,
    )
    assert london[0] == "London range"
    logic = make_logic("swing", settings.config, sessions, 0.1)
    for minute in range(60 * 8):
        moment = start + dt.timedelta(minutes=minute)
        logic.on_bar(moment, 2030.0, 2030.4, 2029.6, 2030.0 + (minute % 7) * 0.05)
    marks = logic.chart()
    assert {name for name, _ in marks.lines} == {"EMA", "channel low", "channel high"}
    assert marks.boxes and marks.boxes[0][0] == "Asian range"


def test_the_scalp_file_v3(tmp_path: Path) -> None:
    panel = ScalpPanel(tmp_path, chart_time)
    t = utc(2026, 11, 17, 8, 0)
    swing = ChartMarks(
        "M15",
        "UP",
        9.0,
        lines=(("EMA", ((t, 2030.0), (t + dt.timedelta(minutes=15), 2030.5))),),
        boxes=(("Asian range", utc(2026, 11, 17, 0), utc(2026, 11, 17, 6), 2036.0, 2027.0),),
    )
    hft = ChartMarks("M1", "DOWN", 2.0, ((t, 2031.0, -1),), ((t, 2032.0, "high"),))
    nothing = summarize("HFT", [])
    assert panel.write(
        now=t,
        mode="swing",
        where="paper",
        state="active",
        strategies="both",
        status="READY",
        marks=swing,
        open_scalps=[],
        closed=[],
        today=nothing,
        month=nothing,
        session="london",
        spread=24.0,
        spread_median=22.0,
        news="",
        price=2030.0,
        layers={"hft": hft, "swing": swing},
        decision=Decision(t, "NONE", "skipped: outside the active sessions", "ema_pullback"),
        modes={"hft": "HFT: standby, paper", "swing": "swing scalping: selected — paper"},
    )
    head, items = panel_lines(panel.path)
    assert (head["version"], head["signal"], head["signal_text"]) == (
        3,
        "NONE",
        "skipped: outside the active sessions",
    )
    assert head["m_swing"] == "swing scalping: selected - paper"  # plain ASCII
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_kind.setdefault(item["k"], []).append(item)
    assert [(i["mode"], i["v"]) for i in by_kind["step"]] == [("hft", 2031.0)]
    assert [(i["mode"], i["s"]) for i in by_kind["x"]] == [("hft", "high")]
    assert [(i["mode"], i["name"], i["v"]) for i in by_kind["line"]] == [
        ("swing", "EMA", 2030.0),
        ("swing", "EMA", 2030.5),
    ]
    (box,) = by_kind["box"]
    assert (box["name"], box["top"], box["bot"], box["t1"]) == (
        "Asian range",
        2036.0,
        2027.0,
        chart_time(utc(2026, 11, 17)),
    )


def test_the_quick_scalp_line_in_the_bot(settings: Settings, db: Database, tmp_path: Path) -> None:
    h = PanelHarness(settings, db, tmp_path, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 7, 58))
    head, _ = panel_lines(h.folder / "scalp.json")
    assert (head["signal"], head["signal_text"]) == ("NONE", "no setup on the last bar")
    assert head["m_hft"].startswith("HFT: selected, paper | open 0")
    assert head["m_swing"].startswith("swing scalping: standby, paper")
    h.run_until(utc(2026, 11, 17, 8, 0))
    head, _ = panel_lines(h.folder / "scalp.json")
    assert (head["signal"], head["signal_text"]) == ("BUY", "placed 2026-11-17-HFT-001 (paper)")
    assert h.hft.decision is not None and h.hft.decision.rule == "sweep_reclaim"


def test_the_buttons_switch_strategies_and_modes(
    settings: Settings, db: Database, tmp_path: Path
) -> None:
    h = PanelHarness(settings, db, tmp_path, [])
    h.folder.mkdir(parents=True, exist_ok=True)
    now = h.clock.now()
    for number, name in enumerate(("mode:scalper", "scalper:intraday", "approve:hft"), 1):
        (h.folder / "scalp_commands.json").write_text(
            json.dumps({"id": number, "t": int(now.timestamp()), "cmd": name}), encoding="ascii"
        )
        h.trader.dashboard_job()
    assert (h.trader.strategies, h.trader.scalper_mode) == ("scalper", "intraday")
    assert h.notes.sent[-2].startswith(
        "📊 Chart /mode scalper: ✅ Only the scalper may open trades"
    )
    assert h.notes.sent[-1].startswith("📊 Chart /scalper intraday: ✅ Scalper mode: intraday")
    assert db.get_state("scalper.approved") is None  # approvals stay on Telegram


# --------------------------------------------------------------------------- #
# The indicator and the install script
# --------------------------------------------------------------------------- #


def test_the_indicator_v3() -> None:
    source = (PROJECT / "mt5" / "GoldSweepPanel.mq5").read_text(encoding="utf-8")
    assert '#property version     "3.00"' in source
    assert source.count("FILE_WRITE") == 1 and "InpCommandFile, FILE_WRITE" in source
    for word in ("OrderSend", "CTrade", "PositionClose", "TRADE_ACTION", "OrderSendAsync"):
        assert word not in source  # it never trades
    for feature in (
        "InpMinGrade",  # A+ only / A and up / every signal
        "DrawForming",  # setups forming, A+ possible
        "DrawPool",
        "DrawGap",
        "DrawAnalysis",  # the analysis box and its pages
        "ThemeRestore",  # the chart theme comes off with the indicator
        "SendNotification",  # phone alerts, when switched on
        "Quick Scalp",
        "STALE",
    ):
        assert feature in source
    for page in ("Market", "Levels", "Setups", "Scalper", "Today", "Brief"):
        assert f'"{page}"' in source


def test_no_third_party_name_anywhere() -> None:
    """The reference product's name appears only in the owner's verbatim prompt, in its
    own branding rule (D98)."""
    names = ["".join(reversed(word)) for word in ("tehporp", "kcirtnaw")]
    allowed = {PROJECT / "docs" / "SCALPER_SPEC.md"}
    checked = 0
    for folder in ("goldsweep", "mt5", "deploy", "docs", "tests"):
        for path in (PROJECT / folder).rglob("*"):
            if path.suffix not in {".py", ".mq5", ".ps1", ".md", ".cmd", ".yaml"}:
                continue
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            checked += 1
            assert not any(name in text for name in names), path
    for name in ("README.md", "DECISIONS.md", "CLAUDE.md", "config.yaml"):
        text = (PROJECT / name).read_text(encoding="utf-8").lower()
        assert not any(word in text for word in names), name
    assert checked > 100


def test_the_install_script_is_safe_to_read() -> None:
    script = (PROJECT / "deploy" / "install-panel.ps1").read_text(encoding="utf-8")
    assert "[switch]$DryRun" in script and "GoldSweepPanel.prev.mq5" in script
    assert "/compile:" in script and "origin.txt" in script

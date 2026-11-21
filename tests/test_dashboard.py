"""The dashboard (Phase 7): what the bot stores for it, its data layer, charts and page.

The paper trader runs the hand-made Phase 2 market (tests/test_paper.py): one
long at 2028.90 that fills, reaches TP1 and closes at TP2 for +227.51.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from goldsweep import cli
from goldsweep.core.settings import Settings
from goldsweep.dashboard import charts, data
from goldsweep.dashboard.launch import APP, URL, run_dashboard
from goldsweep.live import HEARTBEAT_KEY, PAUSED, SNAPSHOT_KEY
from goldsweep.storage.db import Database
from tests.helpers import bars, only_spec_model, utc
from tests.test_paper import Harness


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """This module follows the spec's model end to end: the Phase 9 models are off."""
    return only_spec_model(settings)


END = utc(2026, 11, 17, 8, 40)


@pytest.fixture
def traded(settings: Settings, tmp_path: Path) -> Any:
    db = Database(tmp_path / "bot.sqlite3")
    h = Harness(settings, db)
    h.run_until(END)
    yield h
    db.close()


def test_the_bot_stores_what_the_dashboard_reads(traded: Harness) -> None:
    picture = data.snapshot(traded.db)
    assert picture is not None
    assert (picture["mode"], picture["live"], picture["state"], picture["symbol"]) == (
        "PAPER",
        False,
        "active",
        "XAUUSD",
    )
    assert picture["equity"] == pytest.approx(10_227.51)
    assert picture["positions"] == [] and picture["orders"] == []
    assert picture["pools"] and {"label", "price", "side", "strength", "swept"} <= set(
        picture["pools"][0]
    )
    assert picture["pressure"]["buyers"] + picture["pressure"]["sellers"] == pytest.approx(100)
    risk = picture["risk"]
    assert (risk["trades_today"], risk["max_trades_per_day"], risk["open"]) == (1, 3, 0)
    assert risk["day_pnl"] == pytest.approx(227.51) and risk["day_limit"] < 0
    frame = data.candles(traded.db, "XAUUSD", bars=500)
    assert not frame.empty and frame.index[-1] == pd.Timestamp(END - dt.timedelta(minutes=5))


def test_bot_status_from_the_heartbeat(traded: Harness) -> None:
    db, now = traded.db, traded.clock.now()
    assert data.bot_status(db, now).seen is None
    assert "has not run with the dashboard" in data.bot_status(db, now).describe(now)
    traded.trader.dashboard_job()
    status = data.bot_status(db, now)
    assert status.alive and (status.mode, status.state) == ("PAPER", "active")
    later = now + dt.timedelta(minutes=3)
    assert not data.bot_status(db, later).alive
    assert "NOT running (last seen 3 min ago)" in data.bot_status(db, later).describe(later)


def test_commands_from_the_dashboard(traded: Harness) -> None:
    db, trader = traded.db, traded.trader
    now = traded.clock.now()
    pause = data.send_command(db, "pause", now)
    assert data.command_result(db, pause, now).status == "pending"
    trader.dashboard_job()
    result = data.command_result(db, pause, now)
    assert result.status == "done" and result.reply.startswith("⏸ Paused")
    assert trader.trading == PAUSED and data.snapshot(db)["state"] == PAUSED
    assert traded.notes.sent[-1].startswith("🖥 Dashboard /pause: ⏸ Paused")
    stop = data.send_command(db, "stop", now)
    trader.dashboard_job()
    assert data.command_result(db, stop, now).reply.startswith("🛑 Kill switch")
    old = data.send_command(db, "resume", now - dt.timedelta(minutes=5))
    trader.dashboard_job()
    assert data.command_result(db, old, now).status == "expired"
    assert trader.trading == "stopped"  # the stale resume was not carried out
    with pytest.raises(ValueError, match="unknown command"):
        data.send_command(db, "buy", now)
    unread = data.send_command(db, "status", now)
    assert data.command_result(db, unread, now + dt.timedelta(minutes=3)).status == "expired"


def test_signals_trades_and_equity(traded: Harness) -> None:
    db = traded.db
    found = data.signals(db, END - dt.timedelta(days=1))
    (row,) = found.itertuples()
    assert (row.id, row.direction, row.action) == ("2026-11-17-LDN-01", "LONG", "traded")
    assert (row.entry, row.tp1, row.tp2) == (2028.9, 2036.0, 2048.0)
    assert row.tp3 is None  # 3R (2037.33) is not beyond TP2
    assert row.pool and row.what_happened.startswith("paper limit order placed")
    closed = data.trades(db, live=False)
    assert list(closed["exit"]) == ["tp2"] and closed["parts"].iloc[0] == 2
    curve = data.equity_curve(closed, 10_227.51)
    assert list(curve["equity"]) == pytest.approx([10_000.0, 10_227.51])
    stats = data.trade_stats(closed)
    assert (stats.trades, stats.wins, stats.losses, stats.profit_factor) == (1, 1, 0, None)
    assert data.trades(db, live=True).empty
    assert data.trade_stats(data.trades(db, live=True)).win_rate is None
    assert data.equity_curve(data.trades(db, live=True), None).empty


def test_open_gaps() -> None:
    rows = [
        (2000.0, 2001.0, 1999.0, 2000.5),
        (2000.5, 2006.0, 2000.4, 2005.5),  # displacement up: gap 2001.0 - 2003.0
        (2005.5, 2007.0, 2003.0, 2006.5),
        (2006.5, 2007.5, 2005.0, 2006.0),
        (2006.0, 2006.2, 1996.0, 1996.5),  # displacement down: gap 2001.0 .. 2005.0, and it
        (1996.5, 1997.0, 1994.0, 1994.5),  # trades back through the first gap
        (1994.5, 1996.0, 1993.0, 1995.0),
    ]
    frame = bars(rows, utc(2026, 11, 18, 8, 0))
    gaps = data.open_gaps(frame, atr_period=2, min_size_atr=0.0)
    assert [(g.direction.value, g.bottom, g.top) for g in gaps] == [("SHORT", 1997.0, 2005.0)]


def test_news_briefs_releases_and_ai_usage(db: Database, settings: Settings) -> None:
    now = utc(2026, 11, 18, 12, 0)
    stamp = int(now.timestamp())
    db.execute(
        "INSERT INTO headlines (key, source, title, published, fetched, relevant, direction, "
        "impact, drivers, confidence, reason, method) VALUES "
        "('a', 'FXStreet', 'Gold jumps as Fed signals cuts', ?, ?, 1, 'bullish', 'high', "
        "'[\"real_yields\"]', 0.8, 'rate cuts', 'llm'), "
        "('b', 'ECB', 'Euro story', ?, ?, 0, 'neutral', 'low', NULL, 0.3, '', 'rules')",
        (stamp - 600, stamp, stamp - 300, stamp),
    )
    feed = data.headlines(db, now - dt.timedelta(hours=1))
    assert list(feed["tagged_by"]) == ["keywords", "AI"]
    assert feed.iloc[1]["drivers"] == "real_yields" and bool(feed.iloc[1]["gold"])
    db.execute(
        "INSERT INTO scenarios (key, time, trigger, method, body) VALUES (?, ?, ?, ?, ?)",
        (
            "ny",
            stamp,
            "New York kill zone opening 13:00 UTC",
            "llm",
            json.dumps({"cases": [{"name": "Bullish case", "text": "..."}], "notes": ["n"]}),
        ),
    )
    (brief,) = data.scenario_briefs(db, now - dt.timedelta(hours=1))
    assert brief.cases == [("Bullish case", "...")] and brief.notes == ["n"]
    db.execute(
        "INSERT INTO calendar_events (source, event_id, time, currency, title, impact, forecast, "
        "previous, updated_at) VALUES ('forexfactory', '1', ?, 'USD', 'CPI m/m', 'high', "
        "'0.3%', '0.2%', ?), ('forexfactory', '2', ?, 'JPY', 'BoJ speech', 'low', NULL, NULL, ?)",
        (stamp + 3600, stamp, stamp + 7200, stamp),
    )
    (release,) = data.upcoming_releases(db, settings, now)
    assert (release.currency, release.title) == ("USD", "CPI m/m")
    db.execute(
        "INSERT INTO llm_usage (time, day, purpose, model, input_tokens, output_tokens, "
        "cache_read_tokens, cost_usd, outcome) VALUES (?, '2026-11-18', 'headline', 'm', 700, "
        "150, 500, 0.0, 'ok')",
        (stamp,),
    )
    assert data.llm_today(db, now) == {
        "day": "2026-11-18",
        "calls": 1,
        "input": 1200,
        "output": 150,
        "cost": 0.0,
    }


def test_backtest_reports(tmp_path: Path) -> None:
    folder = tmp_path / "backtest_2024-01-01_2026-10-31"
    folder.mkdir()
    (folder / "summary.json").write_text(
        json.dumps({"metrics": {"trades": 12, "win_rate": 0.42}, "period": ["a", "b"]}),
        encoding="utf-8",
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "summary.json").write_text("{", encoding="utf-8")
    (report,) = data.backtest_reports(tmp_path)
    assert report.name == "backtest_2024-01-01_2026-10-31"
    assert data.flatten(report.summary) == {"metrics.trades": 12, "metrics.win_rate": 0.42}
    assert data.backtest_reports(tmp_path / "missing") == []


def test_charts(traded: Harness) -> None:
    db = traded.db
    frame = data.candles(db, "XAUUSD", bars=500)
    picture = data.snapshot(db)
    assert picture is not None
    zone = dt.UTC
    gaps = data.open_gaps(frame)
    fig = charts.price_chart(
        frame,
        zone=zone,
        pools=picture["pools"],
        gaps=gaps,
        signals=data.signals(db, END - dt.timedelta(days=1)),
        positions=[{"id": "x", "fill_price": 2029.0, "stop": 2027.0}],
    )
    assert fig.data[0].type == "candlestick"
    assert any(trace.type == "scatter" and "BUY" in trace.text[0] for trace in fig.data[1:])
    assert len(fig.layout.shapes) >= 5
    assert "No candles" in charts.price_chart(pd.DataFrame(), zone=zone).layout.title.text
    closed = data.trades(db, live=False)
    assert len(charts.equity_chart(data.equity_curve(closed, None), zone).data) == 1
    assert [t.x[0] for t in charts.pressure_bar(58.0, 42.0).data] == [58.0, 42.0]


def test_the_page_renders_and_sends_commands(
    traded: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamlit.testing.v1 import AppTest

    db = traded.db
    now = dt.datetime.now(dt.UTC)
    db.set_state(HEARTBEAT_KEY, {"time": now.isoformat(), "mode": "PAPER"})  # alive
    monkeypatch.setenv("GOLDSWEEP_DASHBOARD_DB", str(db.path))
    monkeypatch.setenv("GOLDSWEEP_REPORTS_DIR", str(tmp_path / "reports"))
    page = AppTest.from_file(str(APP), default_timeout=120)
    page.run()
    assert not page.exception
    metrics = {m.label: m.value for m in page.metric}
    assert metrics["Mode"] == "PAPER" and metrics["Equity"] == "10,227.51"
    assert any("PAPER bot running" in s.value for s in page.success)
    page.button(key="pause").click().run()
    assert not page.exception
    assert db.query("SELECT name, status FROM commands") == [("pause", "pending")]
    assert data.snapshot(db) is not None and db.get_state(SNAPSHOT_KEY)["mode"] == "PAPER"


def test_the_page_without_a_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("GOLDSWEEP_DASHBOARD_DB", str(tmp_path / "empty.sqlite3"))
    monkeypatch.setenv("GOLDSWEEP_REPORTS_DIR", str(tmp_path / "reports"))
    page = AppTest.from_file(str(APP), default_timeout=120)
    page.run()
    assert not page.exception
    assert any("has not run with the dashboard yet" in w.value for w in page.warning)
    assert page.button(key="kill").disabled  # no bot, no commands


def test_cli_routes_dashboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake(settings: Settings, **kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("goldsweep.dashboard.launch.run_dashboard", fake)
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["--dashboard"]) == 0 and cli.main(["--dashboard", "--mock"]) == 0
    assert calls == [{"use_mock": False}, {"use_mock": True}]


def test_launch_runs_streamlit_on_localhost_only(settings: Settings) -> None:
    started: list[tuple[list[str], dict[str, str]]] = []
    opened: list[str] = []

    class Process:
        def poll(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    def popen(command: list[str], env: dict[str, str]) -> Process:
        started.append((command, env))
        return Process()

    code = run_dashboard(
        settings, use_mock=True, popen=popen, open_browser=opened.append, wait_seconds=0
    )
    assert code == 0 and opened == [URL]
    command, env = started[0]
    text = " ".join(command)
    assert "-m streamlit run" in text and str(APP) in command
    assert "--server.address localhost" in text and "--browser.gatherUsageStats false" in text
    assert env["GOLDSWEEP_DASHBOARD_DB"] == str(settings.mock_db_path)


def test_telegram_commands_run_in_their_own_loop(traded: Harness) -> None:
    stop = threading.Event()
    calls: list[int] = []

    def once() -> None:
        calls.append(1)
        stop.set()

    traded.trader.command_job = once  # type: ignore[method-assign]
    traded.trader.command_loop(stop)
    assert calls == [1]

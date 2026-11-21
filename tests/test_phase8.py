"""Phase 8: unattended starts and /golive, heartbeat, watchdog, backups, lock behaviour."""

from __future__ import annotations

import datetime as dt
import io
import threading
from pathlib import Path
from typing import Any

import pytest

from goldsweep import cli
from goldsweep.alerts.telegram import Command, ConsoleNotifier, TelegramBot
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import Settings
from goldsweep.execution.book import EventKind
from goldsweep.live import HEARTBEAT_KEY, run_live
from goldsweep.storage.db import Database
from goldsweep.watchdog import ALERTED_KEY, check, gold_market_open, run_watchdog
from tests.helpers import only_spec_model, utc
from tests.test_live_executor import Rig
from tests.test_live_loop import LOGIN, LiveHarness, fake_terminal, live_settings
from tests.test_paper import Harness


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """This module follows the spec's model end to end: the Phase 9 models are off."""
    return only_spec_model(settings)


# --------------------------------------------------------------------------- #
# Unattended start: watching only until /golive <account>
# --------------------------------------------------------------------------- #


def test_no_live_order_before_golive(settings: Settings, db: Database, tmp_path: Path) -> None:
    h = LiveHarness(settings, db, tmp_path)
    h.executor.armed = False  # as after an unattended start
    h.run_until(utc(2026, 11, 17, 7, 55))
    (alert,) = h.notes.sent
    assert f"blocked: live orders are not confirmed yet: send /golive {LOGIN}" in alert
    assert h.fake.count("order_send") == 0
    assert h.trader.handle(Command("golive", "123", 1)).startswith("Not confirmed.")
    assert not h.executor.armed
    reply = h.trader.handle(Command("golive", str(LOGIN), 1))
    assert reply.startswith(f"✅ Live orders confirmed for account {LOGIN}. Trading is active.")
    assert h.executor.armed
    assert "already allowed" in h.trader.handle(Command("golive", str(LOGIN), 1))


def test_unarmed_executor_holds_back_stop_moves_until_confirmed(
    settings: Settings, db: Database
) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    rig.executor.armed = False  # e.g. restarted unattended with a trade open
    events = rig.move(2660.10)  # TP1 hit at the broker
    assert [e.kind for e in events] == [EventKind.LEG_CLOSED]  # no stop moved yet
    assert {p.sl for p in rig.positions()} == {2645.21}
    rig.executor.arm()
    moved = rig.executor.sync()
    assert [e.kind for e in moved] == [EventKind.STOP_MOVED]
    assert {p.sl for p in rig.positions()} == {2650.21}


def test_kill_switch_works_before_confirmation(settings: Settings, db: Database) -> None:
    rig = Rig(settings, db)
    rig.submit()
    rig.executor.sync()
    rig.executor.armed = False
    events = rig.executor.close_everything(rig.clock.now(), 2650.0, 2650.21, "kill switch")
    assert [e.kind for e in events] == [EventKind.CLOSED] and rig.fake.open_positions == {}


def test_paper_has_nothing_to_confirm(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert (
        h.trader.handle(Command("golive", "1", 1)) == "Paper trading: there is nothing to confirm."
    )


def _start_unattended(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    clock = ManualClock(utc(2026, 11, 18, 8, 0))
    fake, client = fake_terminal(clock)
    fake.quote(2650.0)
    out = io.StringIO()
    notes = ConsoleNotifier(out)
    monkeypatch.setattr("goldsweep.live.make_notifier", lambda s, db, stream=None: (notes, None))

    def no_questions(_prompt: str) -> str:
        raise AssertionError("an unattended start must not ask at the console")

    code = run_live(
        settings,
        use_llm=False,
        out=out,
        ask=no_questions,
        broker_factory=lambda s, c, d: client,
        run_scheduler=False,
        unattended=True,
    )
    return code, out.getvalue()


def test_unattended_start_waits_for_golive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "a" * 35)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    code, text = _start_unattended(live_settings(tmp_path, monkeypatch), monkeypatch)
    assert code == 0
    assert f"Unattended start: watching only. To allow live orders, send /golive {LOGIN}" in text


def test_unattended_start_needs_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, text = _start_unattended(live_settings(tmp_path, monkeypatch), monkeypatch)
    assert code == 2 and "an unattended start needs Telegram" in text


# --------------------------------------------------------------------------- #
# Heartbeat, housekeeping
# --------------------------------------------------------------------------- #


def test_heartbeat_message(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 8, 40))
    h.trader.heartbeat_job()
    beat = h.notes.sent[-1]
    assert beat.startswith("💓 GoldSweep PAPER running | trading active | equity 10,227.51")
    assert "open trades 0 | last bar 08:35 UTC" in beat


def test_heartbeats_arrive_quietly() -> None:
    sent: list[dict[str, Any]] = []

    def post(url: str, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent.append(payload)
        return {"ok": True, "result": {}}

    bot = TelegramBot("123456:" + "a" * 35, 42, post=post)
    assert bot.send("💓 alive", quiet=True) and bot.send("🟢 setup")
    assert [p["disable_notification"] for p in sent] == [True, False]


def test_housekeeping_backs_up_and_prunes(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    folder = settings.data_dir / "backups"
    folder.mkdir(parents=True)
    stem = Path(db.path).stem
    for day in range(1, 17):  # sixteen older copies
        (folder / f"{stem}-2026-10-{day:02d}.sqlite3").write_bytes(b"old")
    db.write_log(time="2026-09-01T00:00:00.000Z", level="INFO", logger="x", message="old")
    db.write_log(time="2026-11-17T07:00:00.000Z", level="INFO", logger="x", message="new")
    h.trader.housekeeping_job()
    copies = sorted(p.name for p in folder.glob("*.sqlite3"))
    assert len(copies) == 14 and copies[-1] == f"{stem}-2026-11-17.sqlite3"
    with Database(folder / copies[-1]) as copy:
        assert copy.schema_version == db.schema_version
    messages = [row["message"] for row in db.recent_logs(1000)]
    assert "old" not in messages and "new" in messages


def test_dashboard_job_never_waits_for_a_busy_lock(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    db.execute("INSERT INTO commands (created, source, name) VALUES (?, 'dashboard', 'pause')",
               (int(h.clock.now().timestamp()),))  # fmt: skip
    holding, release = threading.Event(), threading.Event()

    def busy() -> None:
        with h.trader._lock:  # noqa: SLF001 - the bar job holding the lock
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=busy)
    worker.start()
    holding.wait(5)
    h.trader.dashboard_job()
    assert db.get_state(HEARTBEAT_KEY) is not None  # alive, even while the lock is busy
    assert db.query("SELECT status FROM commands") == [("pending",)]  # done next time
    release.set()
    worker.join()
    h.trader.dashboard_job()
    assert db.query("SELECT status FROM commands") == [("done",)]


# --------------------------------------------------------------------------- #
# The watchdog
# --------------------------------------------------------------------------- #

WEDNESDAY = utc(2026, 11, 18, 14, 0)


def test_market_hours() -> None:
    assert gold_market_open(WEDNESDAY)
    assert not gold_market_open(utc(2026, 11, 18, 22, 30))  # 17:30 New York: daily pause
    assert not gold_market_open(utc(2026, 11, 21, 12, 0))  # Saturday
    assert not gold_market_open(utc(2026, 11, 22, 22, 0))  # Sunday 17:00 New York
    assert gold_market_open(utc(2026, 11, 22, 23, 30))  # Sunday 18:30 New York
    assert not gold_market_open(utc(2026, 11, 20, 22, 30))  # Friday after the close


def test_watchdog_alerts_once_then_gives_the_all_clear(db: Database) -> None:
    assert check(db, WEDNESDAY, 15) == "⚠️ GoldSweep has never reported in. Is it started?"
    assert check(db, WEDNESDAY + dt.timedelta(minutes=15), 15) is None  # not twice
    db.set_state(HEARTBEAT_KEY, {"time": (WEDNESDAY + dt.timedelta(minutes=29)).isoformat()})
    assert check(db, WEDNESDAY + dt.timedelta(minutes=30), 15).startswith(
        "✅ GoldSweep is running again"
    )
    assert db.get_state(ALERTED_KEY) is None
    late = WEDNESDAY + dt.timedelta(minutes=60)
    text = check(db, late, 15)
    assert text is not None and text.startswith("⚠️ GoldSweep has not been seen for 31 min")
    weekend = utc(2026, 11, 21, 12, 0)
    db.set_state(ALERTED_KEY, None)
    assert check(db, weekend, 15) is None  # the market is shut: silence is normal


def test_run_watchdog(settings: Settings) -> None:
    notes = ConsoleNotifier(io.StringIO())
    out = io.StringIO()
    assert run_watchdog(settings, clock=ManualClock(WEDNESDAY), notifier=notes, out=out) == 0
    assert notes.sent == ["⚠️ GoldSweep has never reported in. Is it started?"]
    assert run_watchdog(settings, clock=ManualClock(WEDNESDAY), notifier=notes, out=out) == 0
    assert "nothing to report" in out.getvalue() and len(notes.sent) == 1


def test_cli_routes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "goldsweep.watchdog.run_watchdog", lambda s, **kw: calls.append(("watchdog", kw)) or 0
    )
    monkeypatch.setattr("goldsweep.live.run_live", lambda s, **kw: calls.append(("live", kw)) or 0)
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    assert cli.main(["--watchdog"]) == 0
    assert cli.main(["--live", "--unattended"]) == 0
    assert calls == [("watchdog", {}), ("live", {"use_llm": True, "unattended": True})]

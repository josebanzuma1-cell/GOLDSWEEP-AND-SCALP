"""The strategy and scalper-mode switch (S2, D103), and latency reporting.

On the hand-made London raid of tests/test_paper.py.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from goldsweep.alerts.telegram import Command
from goldsweep.core.settings import Settings
from goldsweep.dashboard import data
from goldsweep.live import (
    SCALPER_APPROVED_KEY,
    SCALPER_MODE_KEY,
    STRATEGIES_KEY,
)
from goldsweep.scalping import modes
from goldsweep.storage.db import Database
from tests.helpers import only_spec_model, utc
from tests.test_paper import Harness

END = utc(2026, 11, 17, 8, 40)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return only_spec_model(settings)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "switch.sqlite3")
    yield database
    database.close()


def say(h: Harness, name: str, argument: str = "") -> str:
    return h.trader.handle(Command(name, argument, 1))


def test_the_defaults_come_from_the_config(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert (h.trader.strategies, h.trader.scalper_mode) == ("main", "off")
    assert "Strategies that may trade: main | scalper: off" in h.trader.status()
    other = settings.config.model_copy(
        update={"switch": settings.config.switch.model_copy(update={"strategies": "both"})}
    )
    with Database(":memory:") as fresh:
        assert (
            Harness(dataclasses.replace(settings, config=other), fresh).trader.strategies == "both"
        )


def test_main_switched_off_only_alerts(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    reply = say(h, "mode", "scalper")
    assert reply.startswith("✅ Only the scalper may open trades; main setups are alerts only.")
    assert "The scalper's mode is off" in reply
    assert db.get_state(STRATEGIES_KEY) == "scalper"
    h.run_until(END)
    alert = h.notes.sent[0]
    assert "Action: alert only: the main strategy is switched off (/mode)" in alert
    assert db.query("SELECT COUNT(*) FROM paper_trades") == [(0,)]
    assert say(h, "mode", "main").startswith("✅ Only the main strategy may open trades")


def test_both_strategies_trade_side_by_side(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert say(h, "mode", "both").startswith("✅ Both strategies may open trades")
    h.run_until(END)
    assert db.query("SELECT exit_reason FROM paper_trades") == [("tp2",)]  # main still trades


def test_mode_answers_and_refusals(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert say(h, "mode").startswith("Strategies that may trade: main.")
    assert say(h, "mode", "everything") == "Send /mode main, /mode scalper or /mode both."
    assert db.get_state(STRATEGIES_KEY) is None


def test_scalper_modes_that_are_not_built_do_nothing(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    reply = say(h, "scalper", "tick")
    assert reply == (
        "✅ Scalper mode: tick (seconds to two minutes): selected, not built yet (phase S5): "
        "nothing runs. The scalper may not trade while /mode is main."
    )
    assert db.get_state(SCALPER_MODE_KEY) == "tick"
    assert "scalper: tick (seconds to two minutes): selected, not built yet" in h.trader.status()
    h.trader = h.make()  # a restart keeps the choice
    assert h.trader.scalper_mode == "tick"
    assert "Its tick mode is not built yet (phase S5)" in say(h, "mode", "both")
    assert say(h, "scalper", "hft").startswith("Send /scalper off")
    assert say(h, "scalper", "off") == "✅ Scalper off: no scalper mode runs."
    assert say(h, "scalper").startswith("Scalper mode: off.")


def test_approving_a_scalper_mode_needs_its_evidence(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(settings, db)
    assert say(h, "approve", "scalper tick") == (
        "The tick mode is not built yet (phase S5): nothing to approve."
    )
    assert say(h, "approve", "scalper hft") == "Send /approve scalper tick, intraday or swing."
    built = dataclasses.replace(modes.REGISTRY["swing"], built=True)
    monkeypatch.setitem(modes.REGISTRY, "swing", built)
    assert say(h, "approve", "scalper swing").startswith("Not approved: the swing mode has not")
    say(h, "scalper", "swing")
    assert "swing (days to weeks): paper" in h.trader.status()
    monkeypatch.setitem(modes.REGISTRY, "swing", dataclasses.replace(built, passed=True))
    assert say(h, "approve", "scalper swing").startswith("✅ The scalper's swing mode is approved")
    assert db.get_state(SCALPER_APPROVED_KEY) == ["swing"]
    assert "swing (days to weeks): trading" in h.trader.status()
    # A session playbook approval still works as before (no monitored trades yet here).
    assert say(h, "approve", "london").startswith("Not approved: the London A+ playbook needs 20")


def test_mode_states() -> None:
    assert modes.mode_state("tick", selected="off", approved=False) == modes.OFF
    assert modes.mode_state("tick", selected="tick", approved=True) == modes.NOT_BUILT
    assert modes.describe("swing", selected="intraday", approved=False) == (
        "swing (days to weeks): off"
    )


def test_the_dashboard_switches_too(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    h.run_until(utc(2026, 11, 17, 7, 35))  # a snapshot exists
    assert data.switch_state(db) == {"strategies": "main", "scalper": "off"}
    now = h.clock.now()
    data.send_command(db, "mode:both", now)
    data.send_command(db, "scalper:intraday", now)
    h.trader.dashboard_job()
    assert db.get_state(STRATEGIES_KEY) == "both"
    assert db.get_state(SCALPER_MODE_KEY) == "intraday"
    assert any(n.startswith("🖥 Dashboard /mode both: ✅ Both strategies") for n in h.notes.sent)
    assert data.switch_state(db)["strategies"] == "both"
    with pytest.raises(ValueError, match="unknown command"):
        data.send_command(db, "mode:everything", now)


def test_latency_in_the_status(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert "Latency:" not in h.trader.status()  # the mock terminal reports no ping
    for elapsed in (120.0, 80.0, 95.0):
        db.execute(
            "INSERT INTO order_log (time, action, ok, retcode, message, request, elapsed_ms) "
            "VALUES (0, 'market', 1, 10009, 'done', '{}', ?)",
            (elapsed,),
        )
    assert "Latency: orders 95 ms (median of the last 3)" in h.trader.status()

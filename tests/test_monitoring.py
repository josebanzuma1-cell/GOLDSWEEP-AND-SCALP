"""Phase 10 stage 2 (D87): session playbooks in the trading loop.

A setup that meets its session's A+ candidate is traded on the shadow book (paper
only, never sent), its result is counted without a message, and after enough
finished trades with a positive average the owner is asked to ``/approve <session>``.
These tests replay the hand-made London raid of tests/test_paper.py: a long at
2028.90 that reaches TP2, graded A+ by a candidate asking for a Judas swing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from goldsweep.alerts import messages
from goldsweep.alerts.telegram import Command
from goldsweep.analysis.grades import parse_playbook, playbook_name
from goldsweep.core.settings import PlaybookRule, SessionPlaybook, Settings
from goldsweep.execution.shadow import Tally, fingerprint
from goldsweep.live import APPROVED_KEY, ASKED_KEY, STOPPED
from goldsweep.storage.db import Database
from tests.helpers import only_spec_model, utc
from tests.test_paper import Harness

JUDAS = PlaybookRule(model="sweep_mss", min_score=60, tags_any=("judas",))
#: A stricter candidate the textbook setup (score 81.1) does not meet.
STRICT = PlaybookRule(model="sweep_mss", min_score=90, tags_any=("judas",))
END = utc(2026, 11, 17, 8, 40)  # the textbook trade has reached TP2


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return only_spec_model(settings)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "monitoring.sqlite3")
    yield database
    database.close()


def with_london(settings: Settings, book: SessionPlaybook, *, needed: int = 20) -> Settings:
    """``settings`` with London's playbook replaced."""
    config = settings.config
    playbooks = config.playbooks.model_copy(update={"london": book, "monitor_trades": needed})
    return dataclasses.replace(settings, config=config.model_copy(update={"playbooks": playbooks}))


#: London as Asia ships: nothing trades yet, the A+ candidate is monitored.
MONITORED = SessionPlaybook(trading=None, candidate=JUDAS)


def seed(db: Database, rule: PlaybookRule, count: int, r: float = 0.5) -> None:
    """``count`` earlier monitored London trades of ``rule``."""
    for n in range(count):
        db.execute(
            "INSERT INTO shadow_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"old-{n}", "london", fingerprint(rule), "LONG", "london", 70.0, 0, 0, 0, 1.0, 0.5,
             "tp1", r * 50, r),
        )  # fmt: skip


# --------------------------------------------------------------------------- #
# Pieces
# --------------------------------------------------------------------------- #


def test_tally_and_fingerprint() -> None:
    assert Tally(0, 0, 0.0).average_r is None
    assert not Tally(0, 0, 0.0).confirmed(1)
    assert Tally(20, 8, 4.0).average_r == pytest.approx(0.2)
    assert Tally(20, 8, 4.0).confirmed(20)
    assert not Tally(19, 8, 4.0).confirmed(20)
    assert not Tally(20, 8, -1.0).confirmed(20)  # a negative average is not confirmed
    assert not Tally(20, 8, 0.0).confirmed(20)  # nor a zero one
    assert fingerprint(JUDAS) == fingerprint(JUDAS.model_copy())
    assert fingerprint(JUDAS) != fingerprint(STRICT)  # an edit is a new candidate


def test_playbook_names() -> None:
    assert [playbook_name(p) for p in ("asia", "london", "newyork")] == [
        "Asia",
        "London",
        "New York",
    ]
    assert parse_playbook("New York") == "newyork"
    assert parse_playbook(" NY ") == "newyork"
    assert parse_playbook("tokyo") == parse_playbook("Asia") == "asia"
    assert parse_playbook("ldn") == "london"
    assert parse_playbook("sydney") is None
    assert parse_playbook("") is None


def test_message_texts() -> None:
    assert messages.tally_text(Tally(0, 0, 0.0)) == "0 finished"
    assert messages.tally_text(Tally(4, 3, 2.0)) == "4 finished, 3 won, +0.50 R average"
    ask = messages.approval_request("asia", "Asia", "sweep_mss, score >= 70", Tally(20, 9, 5.0))
    assert ask.startswith("✅ Asia A+ playbook confirmed in monitoring: 20 finished, 9 won")
    assert "Send /approve asia to let it trade" in ask
    assert "every trade can lose" in ask  # never a promise
    assert "/approve <session>" in messages.HELP and "/revoke <session>" in messages.HELP


# --------------------------------------------------------------------------- #
# The trading loop
# --------------------------------------------------------------------------- #


def test_a_candidate_setup_is_monitored_and_never_sent(settings: Settings, db: Database) -> None:
    h = Harness(with_london(settings, MONITORED), db)
    h.run_until(END)
    # One message: the alert. The monitored trade's result is counted, not messaged.
    (alert,) = h.notes.sent
    assert alert.startswith("🟡 XAUUSD LONG setup (score 81.1) — London kill zone")
    assert "Grade: A+ (London playbook)" in alert
    assert "Action: alert only: the London playbook does not trade yet" in alert
    assert "👀 monitored for the London A+ playbook (0 of 20 finished)" in alert
    # Nothing traded for real: no order, no trade, the balance untouched.
    assert db.query("SELECT COUNT(*) FROM paper_trades") == [(0,)]
    assert h.trader.executor.equity == pytest.approx(10_000.0)
    assert db.query("SELECT playbook, fingerprint, exit_reason FROM shadow_trades") == [
        ("london", fingerprint(JUDAS), "tp2")
    ]
    (detail,) = db.query("SELECT detail FROM signals")[0]
    assert '"grade": "A+ (London playbook)"' in detail
    assert '"monitoring": "monitored for the London A+ playbook (0 of 20 finished)"' in detail
    status = h.trader.status()
    assert "Playbooks:" in status
    assert "• Asia: trades alerts only | no A+ candidate" in status
    assert (
        "• London: trades alerts only | A+ candidate monitored: 1 finished, 1 won, "
        "+4.79 R average (needs 20)"
    ) in status
    assert "• New York: trades sweep_mss, score >= 60 | A+ candidate monitored" in status


def test_a_setup_below_the_candidate_is_not_monitored(settings: Settings, db: Database) -> None:
    h = Harness(with_london(settings, SessionPlaybook(trading=None, candidate=STRICT)), db)
    h.run_until(END)
    (alert,) = h.notes.sent
    assert "Grade: A\n" in alert  # it clears the alert threshold, not the A+ rule
    assert "👀" not in alert
    assert db.query("SELECT COUNT(*) FROM shadow_trades") == [(0,)]


def test_the_trading_rule_decides_what_trades(settings: Settings, db: Database) -> None:
    # London trades only setups with an SMT divergence: the textbook setup has none.
    smt_only = PlaybookRule(model="sweep_mss", tags_any=("smt",))
    h = Harness(with_london(settings, SessionPlaybook(trading=smt_only)), db)
    h.run_until(utc(2026, 11, 17, 7, 55))
    (alert,) = h.notes.sent
    assert (
        "Action: alert only: outside the London playbook (sweep_mss, score >= 60, with smt)"
        in alert
    )
    assert not h.trader.executor.pending_orders


def test_the_twentieth_finished_trade_asks_for_approval(settings: Settings, db: Database) -> None:
    seed(db, JUDAS, 19)
    h = Harness(with_london(settings, MONITORED), db)
    early = h.trader.handle(Command("approve", "london", 1))
    assert early.startswith("Not approved: the London A+ playbook needs 20 finished monitored")
    assert "So far: 19 finished, 19 won, +0.50 R average." in early
    h.run_until(END)
    alert, ask = h.notes.sent  # the setup's alert, then the request: nothing in between
    assert "👀 monitored for the London A+ playbook (19 of 20 finished)" in alert
    assert ask.startswith("✅ London A+ playbook confirmed in monitoring: 20 finished, 20 won")
    assert "Rule: sweep_mss, score >= 60, with judas." in ask
    assert "Send /approve london" in ask
    assert db.get_state(ASKED_KEY) == {"london": fingerprint(JUDAS)}
    assert db.get_state(APPROVED_KEY) is None  # asking never approves


def test_approve_switches_the_session_to_its_candidate(settings: Settings, db: Database) -> None:
    seed(db, JUDAS, 20)
    h = Harness(with_london(settings, MONITORED), db)
    reply = h.trader.handle(Command("approve", "London", 1))
    assert reply.startswith("✅ London A+ playbook approved: sweep_mss, score >= 60, with judas.")
    assert "/revoke london undoes it" in reply
    assert db.get_state(APPROVED_KEY) == {"london": fingerprint(JUDAS)}
    assert h.trader.handle(Command("approve", "ldn", 1)).startswith(
        "The London A+ playbook is already approved"
    )
    assert "• London: trades sweep_mss, score >= 60, with judas | A+ candidate approved" in (
        h.trader.status()
    )
    h.run_until(END)
    alert, *_ = h.notes.sent
    # It now trades through the executor, and it is no longer monitored.
    assert alert.startswith("🟢 XAUUSD LONG setup (score 81.1)")
    assert "👀" not in alert
    assert db.query("SELECT exit_reason FROM paper_trades") == [("tp2",)]
    assert db.query("SELECT COUNT(*) FROM shadow_trades") == [(20,)]  # only the seeded ones


def test_an_approved_candidate_only_takes_its_own_setups(settings: Settings, db: Database) -> None:
    seed(db, STRICT, 20)
    # London traded everything; its approved candidate wants a score of 90.
    book = SessionPlaybook(trading=PlaybookRule(), candidate=STRICT)
    h = Harness(with_london(settings, book), db)
    assert h.trader.handle(Command("approve", "london", 1)).startswith("✅")
    h.run_until(utc(2026, 11, 17, 7, 55))
    (alert,) = h.notes.sent
    assert "Action: alert only: outside the London playbook (sweep_mss, score >= 90" in alert
    assert not h.trader.executor.pending_orders


def test_editing_the_candidate_restarts_its_monitoring(settings: Settings, db: Database) -> None:
    seed(db, JUDAS, 20)
    first = Harness(with_london(settings, MONITORED), db)
    assert first.trader.handle(Command("approve", "london", 1)).startswith("✅")
    # The owner edits the candidate in config.yaml: a new rule, a new count.
    edited = JUDAS.model_copy(update={"min_score": 65})
    h = Harness(with_london(settings, SessionPlaybook(trading=None, candidate=edited)), db)
    status = h.trader.status()
    assert "• London: trades alerts only | A+ candidate monitored: 0 finished (needs 20)" in status
    h.run_until(END)
    (alert,) = h.notes.sent
    assert "Action: alert only: the London playbook does not trade yet" in alert
    assert "👀 monitored for the London A+ playbook (0 of 20 finished)" in alert


def test_revoke(settings: Settings, db: Database) -> None:
    seed(db, JUDAS, 20)
    book = SessionPlaybook(trading=PlaybookRule(), candidate=JUDAS)
    h = Harness(with_london(settings, book), db)
    assert h.trader.handle(Command("revoke", "london", 1)) == (
        "The London A+ playbook was not approved: nothing to revoke."
    )
    assert h.trader.handle(Command("approve", "london", 1)).startswith("✅")
    assert h.trader.handle(Command("revoke", "london", 1)) == (
        "↩️ London A+ playbook approval revoked. The London session now uses: "
        "sweep_mss, score >= 60."
    )
    assert db.get_state(APPROVED_KEY) == {}
    # Back to the trading rule, and the candidate is monitored again; the owner was
    # already asked about it once, so its next result asks nothing.
    db.set_state(ASKED_KEY, {"london": fingerprint(JUDAS)})
    h.run_until(END)
    alert = h.notes.sent[0]
    assert alert.startswith("🟢 XAUUSD LONG setup")
    assert "👀 monitored for the London A+ playbook (20 of 20 finished)" in alert
    assert db.query("SELECT COUNT(*) FROM shadow_trades") == [(21,)]
    assert not any(note.startswith("✅ London A+ playbook confirmed") for note in h.notes.sent)


def test_approve_needs_a_known_session_with_a_candidate(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    assert h.trader.handle(Command("approve", "", 1)) == (
        "Send /approve asia, /approve london or /approve newyork."
    )
    assert h.trader.handle(Command("revoke", "mars", 1)).startswith("Send /revoke asia")
    assert h.trader.handle(Command("approve", "tokyo", 1)) == (
        "The Asia playbook has no A+ candidate to approve."
    )


def test_approving_a_model_that_does_not_trade_says_so(settings: Settings, db: Database) -> None:
    cisd = PlaybookRule(model="cisd", min_score=60)
    seed(db, cisd, 20)
    h = Harness(with_london(settings, SessionPlaybook(trading=None, candidate=cisd)), db)
    reply = h.trader.handle(Command("approve", "london", 1))
    assert "models.cisd.trade is false in config.yaml" in reply


def test_monitoring_survives_a_restart(settings: Settings, db: Database) -> None:
    s = with_london(settings, MONITORED)
    h = Harness(s, db)
    h.run_until(utc(2026, 11, 17, 8, 5))  # the monitored copy has filled
    assert "Monitored now (paper only): 2026-11-17-LDN-01" in h.trader.status()
    h.trader = h.make()  # the program restarts
    assert "Monitored now (paper only): 2026-11-17-LDN-01" in h.trader.status()
    h.run_until(END)
    assert db.query("SELECT exit_reason FROM shadow_trades") == [("tp2",)]


def test_the_kill_switch_clears_the_shadow_book(settings: Settings, db: Database) -> None:
    h = Harness(with_london(settings, MONITORED), db)
    h.run_until(utc(2026, 11, 17, 8, 5))
    assert "Monitored now (paper only)" in h.trader.status()
    h.trader.handle(Command("stop", "", 1))
    assert h.trader.trading == STOPPED
    assert "Monitored now" not in h.trader.status()
    h.run_until(END)
    assert db.query("SELECT COUNT(*) FROM shadow_trades") == [(0,)]  # nothing counted

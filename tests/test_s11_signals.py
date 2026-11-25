"""S11 (D118): signals only, retest entries, graded scalp signals and reminders.

The owner trades every signal by hand. Scalps wait for price to come back to the level
they broke (the retest) and are graded A+, A or B; main setups are sent with their
retest entry (the gap middle) and followed on M1 bars for "price is at the entry
zone", "TP1 reached: move your stop to breakeven", the stop, an expiry or a miss.

Bot-level tests reuse the hand-made M1 market of tests/test_scalper_runner.py: quiet
bars at 2030 from 06:00 UTC on Tuesday 17 November 2026 and a scripted HFT setup on the
07:59 bar (it sweeps the Asian low 2028.50 to 2028.30 and closes back at 2030.00).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from pathlib import Path
from typing import Any

import pytest

from goldsweep.alerts import messages
from goldsweep.analysis.signals import Signal
from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import TradePlan
from goldsweep.execution.signal_watch import DONE, ENTERED, SignalWatch, advance
from goldsweep.live import WATCH_KEY
from goldsweep.scalping import mode_backtest
from goldsweep.scalping.grade_filter import GradedSetup, PathBar, backing_grade
from goldsweep.scalping.grading import Analysis, Grade, grade_scalp, key_level
from goldsweep.scalping.logic import Candidate, anchored_plan
from goldsweep.scalping.plan import LadderCosts
from goldsweep.scalping.retest import (
    CONFIRMED,
    FAILED,
    MISSED,
    RetestWatch,
    check,
    confirmed,
    retest_level,
)
from goldsweep.storage.db import Database
from tests.helpers import bars, only_spec_model, quiet, utc, without_grade_filter
from tests.test_scalp_modes import WEEK
from tests.test_scalper_runner import (
    MINUTE,
    SIGNAL_BAR,
    Harness,
    demo_harness,
    setup_at,
)
from tests.test_simulator import make_signal

LONG, SHORT = Direction.LONG, Direction.SHORT
T0 = utc(2026, 11, 17, 8, 0)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "s11.sqlite3")
    yield database
    database.close()


def signals_on(settings: Settings, *, retest: bool = True) -> Settings:
    """Signals only, scalps on their retest (as shipped), the grade filters off."""
    config = settings.config
    execution = config.execution.model_copy(update={"signals_only": True})
    scalper = config.scalper.model_copy(
        update={"retest": config.scalper.retest.model_copy(update={"enabled": retest})}
    )
    turned = dataclasses.replace(
        settings, config=config.model_copy(update={"execution": execution, "scalper": scalper})
    )
    return without_grade_filter(only_spec_model(turned))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_the_shipped_settings(tmp_path: Path) -> None:
    shipped = load_settings(
        DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path, log_dir=tmp_path
    ).config
    assert shipped.execution.signals_only
    retest = shipped.scalper.retest
    assert retest.enabled and retest.modes == ("hft", "intraday", "swing")
    assert (retest.pullback_r, retest.tolerance_atr) == (0.35, 0.1)
    assert [retest.window(m) for m in ("hft", "intraday", "swing")] == [10, 30, 60]
    assert (shipped.scalper.signal_min_grade, shipped.scalper.signal_valid_min) == ("B", 2)


# --------------------------------------------------------------------------- #
# The retest
# --------------------------------------------------------------------------- #


def candidate(
    direction: Direction = LONG,
    *,
    reference: float = 2030.0,
    anchor: float = 2028.3,
    level: float | None = 2028.5,
    source: Any = None,
) -> Candidate:
    return Candidate(
        T0, direction, "test", reference, anchor, 2.0, "test setup", None, source, level
    )


def test_the_level_a_setup_waits_for() -> None:
    assert retest_level(candidate(), 0.35) == 2028.5  # its own level, inside its range
    # No level, or one on the wrong side: 35% of the way back towards the stop.
    assert retest_level(candidate(level=None), 0.35) == pytest.approx(2029.405)
    assert retest_level(candidate(level=2031.0), 0.35) == pytest.approx(2029.405)
    assert retest_level(candidate(level=math.nan), 0.35) == pytest.approx(2029.405)
    short = candidate(SHORT, reference=2030.0, anchor=2031.7, level=2031.5)
    assert retest_level(short, 0.35) == 2031.5


def long_watch() -> RetestWatch:
    return RetestWatch(candidate(), 2028.5, target=2030.6, tolerance=0.2, deadline=T0 + 10 * MINUTE)


def test_a_retest_is_confirmed_missed_or_failed() -> None:
    watch = long_watch()
    at = T0 + MINUTE
    assert check(watch, at, 2029.9, 2029.0, 2029.5) is None  # not back yet
    assert check(watch, at, 2029.6, 2028.6, 2029.1) == CONFIRMED  # touched, closed back up
    assert check(watch, at, 2029.0, 2028.6, 2028.45) is None  # touched, closed below
    assert check(watch, at, 2029.0, 2028.2, 2028.9) == FAILED  # through the stop anchor
    assert check(watch, at, 2030.7, 2029.9, 2030.5) == MISSED  # ran to TP1 without us
    assert check(watch, T0 + 10 * MINUTE, 2029.6, 2028.6, 2029.1) == MISSED  # too late
    short = RetestWatch(
        candidate(SHORT, anchor=2031.7, level=2031.5),
        2031.5,
        target=2029.4,
        tolerance=0.2,
        deadline=T0 + 10 * MINUTE,
    )
    assert check(short, at, 2031.4, 2030.9, 2031.0) == CONFIRMED
    assert check(short, at, 2031.8, 2031.0, 2031.2) == FAILED


def test_the_entry_moves_to_the_retest() -> None:
    setup = setup_at(SIGNAL_BAR)
    entered = confirmed(candidate(source=setup), 2029.1, T0 + 2 * MINUTE)
    assert (entered.reference, entered.time, entered.retested) == (2029.1, T0 + 2 * MINUTE, True)
    assert (entered.source.reference, entered.source.time) == (2029.1, T0 + 2 * MINUTE)


def test_a_retest_entry_keeps_the_minimum_stop() -> None:
    costs = LadderCosts(spread=0.2, slippage=0.1, commission=0.0)
    near = dataclasses.replace(candidate(reference=2028.9), retested=True)  # 0.40 above the anchor

    def plan(c: Candidate) -> Any:
        return anchored_plan(
            c,
            costs,
            ladder=(1.0, 2.0, 3.0),
            sl_buffer_atr=0.1,
            sl_min_atr=0.5,
            sl_max_atr=3.0,
            cost_multiple=2.0,
            plan_id="X",
            session="london",
        )

    made = plan(near)
    assert made.plan is not None and made.plan.stop_loss == pytest.approx(2027.9)  # 0.5 ATR
    # A market entry this close to its anchor is still refused (as before S11).
    refused = plan(dataclasses.replace(near, retested=False))
    assert refused.plan is None and refused.reason.startswith("no stop within")


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #


def graded(
    backing: str = "none",
    *,
    bias: Direction | None = LONG,
    minutes: float | None = None,
    kind: str | None = None,
    pools: tuple[tuple[float, str], ...] = (),
) -> Grade:
    analysis = Analysis(
        bias=bias,
        bias_score=0.4 if bias is not None else None,
        next_event="USD CPI in 25m" if minutes is not None else "",
        minutes_to_event=minutes,
        above=pools,
    )
    return grade_scalp(
        LONG,
        backing=backing,
        backing_label="A+ BUY 07:40 UTC",
        analysis=analysis,
        level=2028.5,
        level_kind=kind,
        atr=2.0,
    )


def test_grading_a_scalp() -> None:
    assert graded("A+").letter == "A+"
    assert graded("A+", bias=None).letter == "A"  # backed, but the bias is neutral
    assert graded("A").letter == "A"
    assert graded("A", bias=SHORT).letter == "A"  # a main setup outweighs the bias
    assert graded(kind="asia").letter == "A"  # with the bias, at a key level
    assert graded(pools=((2028.8, "round number"),)).letter == "A"  # near a pool
    assert graded().letter == "B"  # with the bias only
    assert graded(bias=None).letter == "B"
    assert graded("A+", minutes=25).letter == "B"  # big news within 30 minutes
    against = graded(bias=SHORT)
    assert against.letter is None and not against.at_least("B")
    assert graded("A+").reasons == (
        "backed by the A+ BUY 07:40 UTC main setup",
        "with the H1 bias (LONG (+0.40))",
    )
    assert graded("A").at_least("B") and not graded().at_least("A")
    assert key_level(2028.5, "london", 2.0, ()) == "a London session level"
    assert key_level(2028.5, None, 2.0, ((2029.0, "swing high"),)) is None


def test_the_best_main_setup_behind_a_scalp() -> None:
    path = [PathBar(T0 - 5 * MINUTE, 2030.2, 2029.8)]
    setups = [
        GradedSetup("B-1", T0 - 20 * MINUTE, LONG, "B", 2040.0, 2025.0),
        GradedSetup("A-1", T0 - 30 * MINUTE, LONG, "A", 2040.0, 2025.0),
        GradedSetup("F-1", T0 - 10 * MINUTE, LONG, "A+ possible", forming=True),
        GradedSetup("S-1", T0 - 10 * MINUTE, SHORT, "A+", 2020.0, 2035.0),
    ]
    window = dt.timedelta(minutes=60)
    grade, setup = backing_grade(LONG, setups, path, T0, window)
    assert (grade, setup.id if setup else None) == ("A+", "F-1")
    assert backing_grade(LONG, setups[:2], path, T0, window)[0] == "A"
    assert backing_grade(SHORT, setups[:2], path, T0, window) == ("none", None)


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


def test_the_scalp_signal_message() -> None:
    plan = TradePlan(
        id="2026-11-17-HFT-001",
        time=T0,
        direction=LONG,
        session="london",
        entry_mode="market",
        entry=2029.1,
        stop_loss=2028.1,
        take_profit_1=2029.7,
        take_profit_2=2030.3,
        take_profit_3=2030.9,
        take_profit_4=2031.5,
        take_profit_5=2032.1,
        expires_at=T0 + MINUTE,
        risk_pct=0.25,
    )
    analysis = Analysis(
        bias=LONG,
        bias_score=0.42,
        news_bias=0.1,
        pressure="Buyers 61% / Sellers 39% (buyers)",
        next_event="USD CPI in 3h10m",
        minutes_to_event=190,
        above=((2035.0, "round number"),),
        below=((2025.0, "round number"),),
    )
    grade = Grade("A", ("with the H1 bias (LONG (+0.42))", "retest at an Asian session level"))
    text = messages.scalp_signal(
        plan, "hft", grade, analysis, reason="asia low swept", retest=2028.5, lots=0.25, valid_min=2
    )
    lines = text.split("\n")
    assert lines[0] == "📲 SCALP SIGNAL HFT A BUY XAUUSD (manual trade)"
    assert lines[1] == "Type: HFT SCALP: 1-minute chart, held seconds to 15 minutes (TP1-TP5)"
    assert lines[2] == "Enter now ~2029.10: retest of 2028.50 confirmed"
    assert lines[3] == (
        "SL 2028.10 | TP1 2029.70 | TP2 2030.30 | TP3 2030.90 | TP4 2031.50 | TP5 2032.10"
    )
    assert lines[4] == "Risk 0.25%: about 0.25 lots on your account"
    assert lines[5] == "Why A: with the H1 bias (LONG (+0.42)); retest at an Asian session level"
    assert "Market: H1 LONG (+0.42) | news bias +0.10 | pressure Buyers 61%" in text
    assert "(estimate) | next: USD CPI in 3h10m" in text
    assert "Levels: above 2035.00 (round number) | below 2025.00 (round number)" in text
    assert (
        "skip it if price is already past 2029.40. After TP1, move your stop to breakeven." in text
    )
    for mode, kind in (("intraday", "INTRADAY SCALP"), ("swing", "SWING SCALP")):
        assert f"Type: {kind}" in messages.scalp_signal(
            plan, mode, grade, analysis, reason="", retest=None, lots=None, valid_min=2
        )


def test_the_main_signal_says_how_to_take_it(base_signal: Signal) -> None:
    signal = make_signal(base_signal)
    text = messages.signal_alert(
        signal,
        action="MANUAL TRADE: place a limit at 2029.00 (gap middle: the retest zone), valid "
        "until 08:30 UTC. After TP1, move your stop to breakeven.",
        traded=False,
        blocked=False,
        lots=0.27,
        risk_pct=0.5,
        pressure=None,
        bias=None,
        next_event=None,
        now=T0,
        mode="LIVE DEMO",
        grade="A+ (London playbook)",
        manual=True,
    )
    lines = text.split("\n")
    assert lines[0].startswith("📲 MAIN SIGNAL A+ ")
    assert lines[1] == "Type: MAIN TRADE: M5 liquidity sweep, held for hours (TP1-TP3)"
    assert "Risk 0.5%: about 0.27 lots on your account" in text
    assert "Action: MANUAL TRADE: place a limit at 2029.00" in text


# --------------------------------------------------------------------------- #
# Following a manual main setup
# --------------------------------------------------------------------------- #


def watch(**changes: Any) -> SignalWatch:
    base = SignalWatch(
        id="2026-11-17-LDN-01",
        direction=LONG,
        grade="A",
        entry=2030.0,
        stop=2028.0,
        take_profit_1=2030.9,
        take_profit_2=2032.0,
        take_profit_3=None,
        created=T0,
        expires_at=T0 + 60 * MINUTE,
    )
    return dataclasses.replace(base, **changes)


def path(*rows: tuple[float, float]) -> list[PathBar]:
    return [PathBar(T0 + n * MINUTE, high, low) for n, (high, low) in enumerate(rows)]


def test_following_a_main_setup() -> None:
    later, events = advance(watch(), path((2030.4, 2030.2), (2030.3, 2029.9), (2031.0, 2030.1)))
    assert events == ["entry", "tp1"] and later.stage == DONE
    later, events = advance(watch(), path((2030.3, 2029.9), (2030.2, 2027.9)))
    assert events == ["entry", "stopped"]
    assert advance(watch(), path((2030.3, 2027.5)))[1] == [
        "entry",
        "stopped",
    ]  # one bar: worse case
    assert advance(watch(), path((2031.0, 2030.2)))[1] == ["missed"]  # TP1 without the retest
    short = watch(direction=SHORT, entry=2030.0, stop=2032.0, take_profit_1=2029.0)
    assert advance(short, path((2032.5, 2029.5)))[1] == ["entry", "stopped"]
    expired = watch(expires_at=T0 + 2 * MINUTE)
    assert advance(expired, path((2030.4, 2030.2), (2030.4, 2030.2), (2030.4, 2030.2)))[1] == [
        "expired"
    ]
    # Bars already read are not read again; the state survives a restart.
    first, _ = advance(watch(), path((2030.3, 2029.9)))
    assert first.stage == ENTERED and SignalWatch.from_state(first.to_state()) == first
    again, events = advance(first, path((2030.3, 2029.9)))
    assert events == [] and again == first
    gap = watch(entry=2029.0)
    assert advance(gap, path((2029.5, 2027.5)))[1] == ["entry", "stopped"]
    failed = watch(entry=2029.0, stop=2029.5)  # a stop above the entry never happens
    assert failed.stop > failed.entry  # (shape only: the plan guarantees the order)


def test_the_watch_messages() -> None:
    w = watch()
    assert messages.watch_message(w, "entry").startswith(
        "🎯 Main BUY 2026-11-17-LDN-01 (A): price is at the entry zone now (2030.00), the retest."
    )
    assert messages.watch_message(w, "tp1") == (
        "✅ Main BUY 2026-11-17-LDN-01 (A): TP1 2030.90 reached. Take part profit and move "
        "your stop to breakeven (2030.00)."
    )
    assert "do not enter" in messages.watch_message(w, "expired")
    assert "do not chase" in messages.watch_message(w, "missed")
    assert "setup failed" in messages.watch_message(w, "failed")


def test_the_bot_follows_a_manual_main_setup(settings: Settings, db: Database) -> None:
    h = Harness(signals_on(settings), db)  # the scalper's M1 bars feed the watcher
    db.set_state(WATCH_KEY, [watch().to_state()])  # as _handle_signal leaves it
    h.run_until(utc(2026, 11, 17, 8, 3))
    assert h.notes.sent[0].startswith("🎯 Main BUY 2026-11-17-LDN-01 (A): price is at the entry")
    assert h.notes.sent[1].startswith("✅ Main BUY 2026-11-17-LDN-01 (A): TP1 2030.90 reached")
    assert db.get_state(WATCH_KEY) == []  # done: no longer followed


# --------------------------------------------------------------------------- #
# Scalp signals in the bot
# --------------------------------------------------------------------------- #

RETEST = bars(
    quiet(120, 2030.0)
    + [
        (2030.0, 2030.1, 2029.4, 2029.5),  # 08:00: not back to the Asian low yet
        (2029.5, 2029.6, 2028.6, 2029.1),  # 08:01: back to 2028.50 (+0.10), closes above
        (2029.1, 2029.8, 2029.0, 2029.75),  # 08:02: fills at 2029.10, reaches TP1 2029.70
    ]
    + quiet(20, 2029.75),
    utc(2026, 11, 17, 6, 0),
    minutes=1,
)


def test_a_scalp_signal_after_its_retest(settings: Settings, db: Database) -> None:
    h = Harness(signals_on(settings), db, [setup_at(SIGNAL_BAR)], market=RETEST)
    h.scalper_on()
    h.run_until(T0)
    assert h.notes.sent == []
    assert h.hft.decision is not None and h.hft.decision.text == (
        "waiting for a retest of 2028.50 until 08:10 UTC"
    )
    h.run_until(utc(2026, 11, 17, 8, 4))
    signal, reminder = h.notes.sent[:2]
    lines = signal.split("\n")
    assert lines[0] == "📲 SCALP SIGNAL HFT B BUY XAUUSD (manual trade)"
    assert lines[2] == "Enter now ~2029.10: retest of 2028.50 confirmed"
    assert lines[3].startswith("SL 2028.10 | TP1 2029.70 | TP2 ")
    assert lines[4] == "Risk 0.25%: about 0.25 lots on your account"
    assert lines[5] == "Why B: H1 bias neutral; retest at an Asian session level"
    assert reminder.startswith("✅ HFT BUY 2026-11-17-HFT-001: TP1 2029.70 reached. Take part ")
    assert "move your stop to breakeven (" in reminder


def test_no_signal_when_the_retest_never_comes(settings: Settings, db: Database) -> None:
    h = Harness(signals_on(settings), db, [setup_at(SIGNAL_BAR)])  # the market rises at once
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 2))  # 08:01 reached TP1 without coming back
    assert h.hft.decision is not None and h.hft.decision.text.startswith(
        "skipped: no retest of 2028.50"
    )
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert h.notes.sent == []  # no signal: it ran without us


def test_signals_only_sends_nothing_to_mt5(settings: Settings, db: Database) -> None:
    demo, h = demo_harness(signals_on(settings, retest=False), db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    assert "HFT (M1, seconds to 15 minutes): on paper" in h.trader.status()
    h.run_until(T0)
    assert demo.fake.open_positions == {} and demo.fake.sent == []
    assert db.get_state("scalper.approved") == ["hft"]  # kept, and does nothing
    assert len(h.hft.paper.pending_orders) == 1  # tracked on paper
    assert h.notes.sent[0].startswith("📲 SCALP SIGNAL HFT B BUY XAUUSD (manual trade)")


# --------------------------------------------------------------------------- #
# The backtest
# --------------------------------------------------------------------------- #


def test_the_mode_backtest_waits_for_the_retest(settings: Settings) -> None:
    spec = default_gold_spec()
    start, end = utc(2026, 11, 16), utc(2026, 11, 21)
    on = signals_on(settings)
    run = mode_backtest.simulate(on, WEEK, spec, "hft", start, end)
    assert run.retest and run.outcomes["waited for a retest"] > 0
    assert run.outcomes["traded"] <= run.outcomes["retest confirmed"]
    plain = mode_backtest.simulate(mode_backtest.without_retest(on), WEEK, spec, "hft", start, end)
    assert not plain.retest and plain.outcomes["waited for a retest"] == 0


def test_a_main_setup_is_sent_as_a_manual_trade(settings: Settings, db: Database) -> None:
    from tests.test_paper import Harness as PaperHarness

    h = PaperHarness(signals_on(settings), db)
    h.run_until(utc(2026, 11, 17, 8, 40))
    alert = h.notes.sent[0]
    lines = alert.split("\n")
    assert lines[0].startswith("📲 MAIN SIGNAL A+ XAUUSD LONG setup (score 81.1)")
    assert lines[1] == "Type: MAIN TRADE: M5 liquidity sweep, held for hours (TP1-TP3)"
    assert "Risk 0.5%: about 0.17 lots on your account" in alert
    assert (
        "Action: MANUAL TRADE: place a limit at 2028.90 (FVG mid, limit: the retest zone), "
        "valid until 09:55 UTC. After TP1, move your stop to breakeven."
    ) in alert
    assert db.query("SELECT id, action FROM signals") == [("2026-11-17-LDN-01", "signal")]
    assert db.query("SELECT COUNT(*) FROM paper_trades") == [(0,)]  # nothing traded
    (followed,) = db.get_state(WATCH_KEY)
    assert (followed["id"], followed["entry"], followed["grade"]) == (
        "2026-11-17-LDN-01",
        2028.9,
        "A+",
    )


def test_the_status_says_signals_only(settings: Settings, db: Database) -> None:
    h = Harness(signals_on(settings), db)
    db.set_state(WATCH_KEY, [watch().to_state()])
    status = h.trader.status()
    assert status.startswith("📲 SIGNALS ONLY: you place every trade; nothing is sent to MT5.")
    assert "Main setups followed: 2026-11-17-LDN-01 (A, waiting)" in status

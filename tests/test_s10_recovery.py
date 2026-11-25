"""S10 (D116): the owner's capped recovery features for the scalper (martingale, grid /
averaging down, HFT stacking, hedging between modes), every mode following the main
strategy's graded setups, and the close once a setup has done its move.

Bot-level tests reuse the hand-made M1 market of tests/test_scalper_runner.py: quiet
bars at 2030 from 06:00 UTC on Tuesday 17 November 2026 and a scripted HFT setup on the
07:59 bar (it closes at 08:00, inside London).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import (
    AverageDownConfig,
    MartingaleConfig,
    ScalperConfig,
    Settings,
    StackConfig,
)
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import EventKind, ExitReason, InvalidPlan, Management, TradePlan
from goldsweep.execution.executor import SCALP_PAPER_TABLE
from goldsweep.risk.account_guard import AccountGuard, Exposure
from goldsweep.scalping.recovery import (
    OpenScalp,
    add_due,
    add_plan,
    budget_pct,
    idea_room,
    is_add,
    martingale_risk,
    open_risk,
    primary_of,
    recovery_line,
    stack_refusal,
)
from goldsweep.scalping.runner import ModeSlot
from goldsweep.storage.db import Database
from tests.helpers import (
    bars,
    only_spec_model,
    quiet,
    utc,
    with_grade_filters,
    with_recovery,
    without_grade_filter,
)
from tests.test_s9_modes import T0, StubBook, position, runner, signal_row, store
from tests.test_scalp_book import T0 as BOOK_T0
from tests.test_scalp_book import bar, book
from tests.test_scalper_runner import MINUTE, SIGNAL_BAR, Harness, demo_harness, setup_at

LONG, SHORT = Direction.LONG, Direction.SHORT


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "s10.sqlite3")
    yield database
    database.close()


@pytest.fixture
def recovering(settings: Settings) -> Settings:
    """The recovery features on, at D116's caps (the shipped config has them off)."""
    return with_recovery(settings)


@pytest.fixture
def unfiltered(recovering: Settings) -> Settings:
    return without_grade_filter(only_spec_model(recovering))


@pytest.fixture
def filtered(recovering: Settings) -> Settings:
    return with_grade_filters(only_spec_model(recovering))


# --------------------------------------------------------------------------- #
# Settings and the pure rules
# --------------------------------------------------------------------------- #


def test_the_recovery_settings(settings: Settings) -> None:
    # Shipped OFF: the owner approved S10 with the recovery features off (D116); the caps
    # are kept for when one is switched on.
    recovery = settings.config.scalper.recovery
    assert recovery.martingale == MartingaleConfig(
        enabled=False, multiplier=2.0, max_steps=1, max_risk_pct=0.5
    )
    assert recovery.average_down == AverageDownConfig(
        enabled=False, add_at_r=(0.4, 0.7), add_risk_pct=0.125
    )
    assert recovery.hft_stack == StackConfig(enabled=False, max_open=3)
    assert (recovery.max_idea_risk_pct, recovery.hedge_modes, recovery.max_open_per_mode) == (
        0.5,
        False,
        3,
    )
    assert recovery.close_when_setup_done  # the setup-done close stays on
    assert settings.config.execution.max_total_lots == 2.0
    scalper = settings.config.scalper
    # The owner switched intraday's (and HFT's) filter off (D117); swing keeps A+ to B.
    assert (scalper.intraday.grade_filter, scalper.intraday.setup_window_min) == ("off", 240)
    assert (scalper.swing_scalp.grade_filter, scalper.swing_scalp.setup_window_min) == ("b", 480)
    # The built-in defaults keep every recovery feature off.
    off = ScalperConfig().recovery
    assert not (off.martingale.enabled or off.average_down.enabled or off.hft_stack.enabled)
    assert not off.hedge_modes
    with pytest.raises(ValidationError, match="inside the first stop"):
        AverageDownConfig(add_at_r=(0.4, 1.2))
    with pytest.raises(ValidationError, match="max_idea_risk_pct"):
        ScalperConfig(recovery={"martingale": {"max_risk_pct": 0.8}, "max_idea_risk_pct": 0.5})


def test_martingale_steps_and_resets() -> None:
    config = MartingaleConfig(enabled=True, multiplier=2.0, max_steps=1, max_risk_pct=0.5)
    assert martingale_risk(0.25, [], config) == (0.25, 0)
    assert martingale_risk(0.25, [-20.0], config) == (0.5, 1)
    assert martingale_risk(0.25, [-20.0, -40.0], config) == (0.5, 1)  # never past one step
    assert martingale_risk(0.25, [-20.0, 15.0], config) == (0.25, 0)  # a win resets it
    two = config.model_copy(update={"max_steps": 2, "max_risk_pct": 0.75})
    assert martingale_risk(0.25, [-1.0, -1.0], two) == (0.75, 2)  # capped at 0.75, not 1.0
    assert martingale_risk(0.25, [-1.0], config.model_copy(update={"enabled": False})) == (0.25, 0)


LONG_SCALP = TradePlan(
    id="2026-11-17-HFT-004",
    time=T0,
    direction=LONG,
    session="london",
    entry_mode="market",
    entry=2030.0,
    stop_loss=2028.0,
    take_profit_1=2030.5,
    take_profit_2=2031.0,
    expires_at=T0 + MINUTE,
)


def test_grid_and_averaging_down_levels() -> None:
    config = AverageDownConfig(enabled=True, add_at_r=(0.4, 0.7), add_risk_pct=0.125)
    scalp = OpenScalp(LONG_SCALP, fill=2030.0, stop=2028.0)  # R 2.00
    assert not add_due(scalp, 2029.3, 0, config)  # only 0.35 R against
    assert add_due(scalp, 2029.2, 0, config)  # 0.4 R against: add 1
    assert not add_due(scalp, 2029.0, 1, config)  # add 2 waits for 0.7 R
    assert add_due(scalp, 2028.6, 1, config)
    assert not add_due(scalp, 2028.0, 1, config)  # at the stop: nothing to add
    assert not add_due(scalp, 2028.5, 2, config)  # two adds at most
    moved = OpenScalp(LONG_SCALP, fill=2030.0, stop=2030.1)  # breakeven+: no rescue needed
    assert not add_due(moved, 2029.2, 0, config)
    assert not add_due(scalp, 2029.2, 0, config.model_copy(update={"enabled": False}))
    add = add_plan(
        scalp, 1, close=2029.2, time=T0, expires_at=T0 + MINUTE, risk_pct=0.125, level_r=0.4
    )
    assert add is not None
    assert (add.id, add.stop_loss, add.take_profit_1, add.take_profit_2, add.risk_pct) == (
        "2026-11-17-HFT-004-A1",
        2028.0,  # the scalp's own first stop
        2030.5,  # one take-profit: the scalp's TP1
        None,
        0.125,
    )
    assert is_add(add.id) and primary_of(add.id) == LONG_SCALP.id and not is_add(LONG_SCALP.id)
    # A close far past its level, near the stop: the add's risk shrinks so its lots are
    # never larger than at the level itself (0.20 of the nominal 0.60 left: a third).
    late = add_plan(
        scalp, 2, close=2028.2, time=T0, expires_at=T0 + MINUTE, risk_pct=0.125, level_r=0.7
    )
    assert late is not None and late.risk_pct == pytest.approx(0.0417)
    assert (
        add_plan(scalp, 2, close=2028.0, time=T0, expires_at=T0, risk_pct=0.125, level_r=0.7)
        is None
    )
    assert idea_room(0.25, [0.125], 0.5) == 0.125 and idea_room(0.5, [], 0.5) == 0.0


def test_hft_stacking_rules() -> None:
    config = StackConfig(enabled=True, max_open=3)
    safe = position(LONG, 2030.0, 2030.1)  # past its entry: risk-free
    risky = position(LONG, 2030.0, 2028.0)
    assert stack_refusal([], 0, LONG, config) is None
    assert stack_refusal([safe], 0, LONG, config) is None
    assert stack_refusal([risky], 0, LONG, config) == (
        "the open HFT scalp is not risk-free yet (stacking waits for its TP1)"
    )
    assert stack_refusal([safe], 0, SHORT, config) == "an HFT scalp is open the other way"
    assert stack_refusal([safe, safe, safe], 0, LONG, config) == (
        "3 HFT scalps are already open (the stacking cap)"
    )
    assert stack_refusal([], 1, LONG, config) == (
        "an HFT scalp is still waiting for its fill (no stacking)"
    )
    assert stack_refusal([safe], 0, LONG, config.model_copy(update={"enabled": False})) == (
        "a scalp of this mode is already open"
    )


def test_open_risk_and_the_daily_budget() -> None:
    value = default_gold_spec().value_per_price_unit  # 100 a lot per 1.00
    held = {"direction": "LONG", "fill_price": 2030.0, "stop": 2029.0, "lots": 0.1}
    assert open_risk(held, value) == pytest.approx(10.0)
    assert open_risk({**held, "stop": 2030.2}, value) == 0.0  # risk-free
    # 1% of 10,000 = 100: 40 lost today and 20 still at risk leave 0.4%.
    assert budget_pct(1.0, -40.0, 20.0, 10_000.0) == pytest.approx(0.4)
    assert budget_pct(1.0, -100.0, 0.0, 10_000.0) == pytest.approx(0.0)
    line = recovery_line(ScalperConfig.model_validate({"recovery": {"hedge_modes": True}}).recovery)
    assert line.startswith("Scalper recovery (paper and demo only): modes may hedge each other")


# --------------------------------------------------------------------------- #
# A plan's own risk, closing one plan
# --------------------------------------------------------------------------- #


def test_a_plan_carries_its_own_risk(settings: Settings) -> None:
    plan = dataclasses.replace(LONG_SCALP, time=BOOK_T0, expires_at=BOOK_T0 + MINUTE, risk_pct=0.5)
    assert TradePlan.from_state(plan.to_state()) == plan
    with pytest.raises(InvalidPlan, match="risk 3.0%"):
        dataclasses.replace(plan, risk_pct=3.0)
    scalps = book(settings, Management(split=(0.4, 0.3, 0.3)))
    placed = scalps.submit(plan, spread_points=0.0, median_spread_points=None)
    base = dataclasses.replace(plan, id="X-2", risk_pct=None)
    other = book(settings, Management(split=(0.4, 0.3, 0.3))).submit(
        base, spread_points=0.0, median_spread_points=None
    )
    assert (placed.lots, other.lots) == (0.25, 0.12)  # 50.00 and 25.00 over 200 a lot


def test_closing_one_plan_on_paper(settings: Settings) -> None:
    scalps = book(settings, Management(split=(0.4, 0.3, 0.3)))
    first = dataclasses.replace(LONG_SCALP, time=BOOK_T0, expires_at=BOOK_T0 + MINUTE)
    second = dataclasses.replace(first, id="2026-11-17-HFT-005")
    scalps.submit(first, spread_points=0.0, median_spread_points=None)
    scalps.submit(second, spread_points=0.0, median_spread_points=None)
    scalps.on_bar(bar(0, 2030.0, 2030.2, 2029.9, 2030.1))  # both fill
    events = scalps.close_plan(
        BOOK_T0 + 2 * MINUTE, 2030.2, 2030.4, first.id, ExitReason.SETUP_DONE, "done"
    )
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed.trade is not None and closed.trade.exit_reason is ExitReason.SETUP_DONE
    assert [p["id"] for p in scalps.positions] == [second.id]  # the other is untouched
    assert scalps.plan_of(second.id) == second and scalps.plan_of(first.id) is None


def test_the_demo_account_sizes_by_the_plan_and_closes_one_plan(
    unfiltered: Settings, db: Database
) -> None:
    demo, _h = demo_harness(unfiltered, db)
    plan = dataclasses.replace(
        LONG_SCALP, time=demo.clock.now(), expires_at=demo.clock.now() + MINUTE, risk_pct=0.5
    )
    placed = demo.scalp.submit(plan, spread_points=20.0, median_spread_points=None)
    # Sized at the ask (2030.21): 0.5% of 10,000 over 2.21 a unit is 0.22 lots.
    assert placed.kind is EventKind.PLACED and placed.lots == 0.22
    assert demo.scalp.plan_for(plan.id) == plan
    demo.fake.quote(2030.40)
    events = demo.scalp.close_plan(demo.clock.now(), 2030.40, 2030.61, plan.id, "done")
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed.trade is not None and closed.trade.exit_reason is ExitReason.SETUP_DONE
    assert demo.fake.open_positions == {}


def test_the_guard_counts_only_the_main_strategy_for_hedging(db: Database) -> None:
    guard = AccountGuard(
        db,
        only_settings_boundary(),
        tables=("live_trades", "scalp_live_trades"),
        daily_loss_pct=2.0,
        weekly_loss_pct=5.0,
        max_open_risk_pct=0.75,
    )
    scalps = [Exposure(SHORT, 20.0)]  # another scalper mode is short
    allowed = guard.check(
        T0, equity=10_000.0, direction=LONG, new_risk=25.0, exposures=scalps, hedge_exposures=[]
    )
    assert allowed.allowed
    refused = guard.check(
        T0,
        equity=10_000.0,
        direction=LONG,
        new_risk=25.0,
        exposures=scalps,
        hedge_exposures=[Exposure(SHORT, 50.0)],  # the main strategy is short: never hedged
    )
    assert refused.reasons == ("the other strategy holds the opposite side (no hedging)",)


def only_settings_boundary() -> Any:
    from goldsweep.core.settings import DayBoundaryConfig

    return DayBoundaryConfig()


# --------------------------------------------------------------------------- #
# The runner's shared rules with the recovery features
# --------------------------------------------------------------------------- #


def test_modes_may_hedge_each_other(settings: Settings, db: Database) -> None:
    swing = StubBook([position(SHORT, 2030.0, 2032.0)])
    r = runner(settings, db, hft=StubBook(), swing=swing)
    hft = r.slots["hft"]
    assert r.shared_refusal(hft, LONG, demo=False, now=T0, hedge=True) is None
    assert r.shared_refusal(hft, LONG, demo=False, now=T0) == (
        "the swing scalp is open the other way (no hedging)"
    )
    # The same way still needs the other scalp risk-free (no stacking across modes).
    swing.open_positions = [position(LONG, 2030.0, 2028.0)]
    assert r.shared_refusal(hft, LONG, demo=False, now=T0, hedge=True) == (
        "the swing scalp this way is not risk-free yet (no stacking)"
    )


def test_a_scalp_and_its_adds_count_as_one(settings: Settings, db: Database) -> None:
    r = runner(settings, db, hft=StubBook(), swing=StubBook())
    hft = r.slots["hft"]
    for n in range(1, 4):  # one losing scalp with two losing adds: one loss, not three
        suffix = "" if n == 1 else f"-A{n - 1}"
        store(db, SCALP_PAPER_TABLE, f"2026-11-17-HFT-001{suffix}", T0 - 30 * MINUTE, -0.5)
    assert r.shared_refusal(hft, LONG, demo=False, now=T0) is None
    for n in (2, 3):
        store(db, SCALP_PAPER_TABLE, f"2026-11-17-HFT-00{n}", T0 - (20 - n) * MINUTE, -1.0)
    refusal = r.shared_refusal(hft, LONG, demo=False, now=T0)
    assert refusal is not None and refusal.startswith("3 losses in a row across the modes")


def test_martingale_and_the_daily_budget_size_a_scalp(recovering: Settings, db: Database) -> None:
    r = runner(recovering, db, hft=StubBook(), swing=StubBook())
    hft = r.slots["hft"]
    assert r._risk_pct(hft, hft.paper, False, T0) == (0.25, [])  # noqa: SLF001
    store(db, SCALP_PAPER_TABLE, "2026-11-17-HFT-001", T0 - 30 * MINUTE, -1.0)  # -25.00
    assert r._risk_pct(hft, hft.paper, False, T0) == (  # noqa: SLF001
        0.5,
        ["martingale step 1 after a loss"],
    )
    # Another mode's loss does not step HFT up, but it uses the shared daily budget.
    store(db, SCALP_PAPER_TABLE, "2026-11-17-SWS-001", T0 - 20 * MINUTE, -2.2)  # -55.00
    pct, notes = r._risk_pct(hft, hft.paper, False, T0)  # noqa: SLF001
    assert pct == pytest.approx(0.2)  # 1% - 0.25% - 0.55%
    assert notes == ["martingale step 1 after a loss", "sized to what the daily loss limit leaves"]


def test_as_shipped_a_loss_takes_no_martingale_step(settings: Settings, db: Database) -> None:
    r = runner(settings, db, hft=StubBook())
    store(db, SCALP_PAPER_TABLE, "2026-11-17-HFT-001", T0 - 30 * MINUTE, -1.0)
    assert r._risk_pct(r.slots["hft"], r.slots["hft"].paper, False, T0) == (  # noqa: SLF001
        0.25,
        [],
    )


def test_no_recovery_feature_on_a_real_account(recovering: Settings, db: Database) -> None:
    settings = recovering
    real = StubBook()
    real.demo = False  # type: ignore[attr-defined]
    on_demo = StubBook()
    on_demo.demo = True  # type: ignore[attr-defined]
    slot = ModeSlot(SimpleNamespace(name="hft"), StubBook(), real)  # type: ignore[arg-type]
    r = runner(settings, db, hft=StubBook())
    r.slots["hft"] = slot
    assert r.recovery_on(slot, slot.paper) and not r.recovery_on(slot, real)
    assert r.recovery_on(ModeSlot(slot.logic, StubBook(), on_demo), on_demo)  # type: ignore[arg-type]
    store(db, "scalp_live_trades", "2026-11-17-HFT-001", T0 - 30 * MINUTE, -1.0)
    assert r._risk_pct(slot, real, True, T0) == (0.25, [])  # noqa: SLF001 - no martingale


# --------------------------------------------------------------------------- #
# In the bot, on paper
# --------------------------------------------------------------------------- #


def test_a_scalp_after_a_loss_takes_the_martingale_step(unfiltered: Settings, db: Database) -> None:
    store(db, SCALP_PAPER_TABLE, "2026-11-17-HFT-000", utc(2026, 11, 17, 7, 0), -1.0)
    h = Harness(unfiltered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(T0)
    (placed,) = h.notes.sent
    assert placed.startswith("⚡ Scalp HFT LONG 2026-11-17-HFT-001 (paper, 0.26 lots, 0.5% risk)")
    assert placed.endswith("martingale step 1 after a loss")


DIP = bars(
    quiet(120, 2030.0)
    + [
        (2030.0, 2030.1, 2029.1, 2029.2),  # 08:00: filled at 2030.10, closes 0.45 R against
        (2029.2, 2029.3, 2028.5, 2028.6),  # 08:01: add 1 fills; closes 0.75 R against
        (2028.6, 2030.8, 2028.5, 2030.7),  # 08:02: add 2 fills; back up through TP1 2030.60
    ]
    + quiet(30, 2030.7),
    utc(2026, 11, 17, 6, 0),
    minutes=1,
)


def test_grid_adds_on_a_dip_then_out_at_tp1(unfiltered: Settings, db: Database) -> None:
    h = Harness(unfiltered, db, [setup_at(SIGNAL_BAR)], market=DIP)
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 5))
    placed = [n for n in h.notes.sent if n.startswith("⚡")]
    assert len(placed) == 3
    assert "add 1 of 2 to 2026-11-17-HFT-001: 0.4 R against (grid / averaging down)" in placed[1]
    assert "add 2 of 2 to 2026-11-17-HFT-001: 0.7 R against" in placed[2]
    assert all("SL 2028.10" in n for n in placed)  # one stop for the scalp and its adds
    rows = dict(db.query("SELECT id, exit_reason FROM scalp_trades"))
    assert rows["2026-11-17-HFT-001-A1"] == rows["2026-11-17-HFT-001-A2"] == "tp1"
    # The scalp's later parts are still open (breakeven+): its adds are remembered.
    # Both closes went past their level, so each add risks less than 0.125%.
    assert h.trader.scalper.adds == {"2026-11-17-HFT-001": [0.1042, 0.0947]}
    (still,) = h.hft.paper.open_positions
    assert still["id"] == "2026-11-17-HFT-001" and still["stop"] > still["fill_price"]


def test_hft_stacks_only_once_the_first_scalp_is_risk_free(
    unfiltered: Settings, db: Database
) -> None:
    # The second setup comes once the first scalp has taken TP1 (its stop past the entry).
    second = setup_at(SIGNAL_BAR + 2 * MINUTE, reference=2030.9, extreme=2029.5)
    h = Harness(unfiltered, db, [setup_at(SIGNAL_BAR), second])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 2))
    placed = [n for n in h.notes.sent if n.startswith("⚡")]
    assert len(placed) == 2 and "stacked: HFT scalp 2 of 3" in placed[1]
    # Too early: one minute after the first fill it has not reached TP1 yet.
    early = Harness(
        unfiltered, Database(":memory:"), [setup_at(SIGNAL_BAR), setup_at(SIGNAL_BAR + MINUTE)]
    )
    early.scalper_on()
    early.run_until(utc(2026, 11, 17, 8, 1))
    assert len([n for n in early.notes.sent if n.startswith("⚡")]) == 1
    assert early.hft.decision is not None and early.hft.decision.text == (
        "skipped: the open HFT scalp is not risk-free yet (stacking waits for its TP1)"
    )
    early.db.close()


def test_a_scalp_closes_when_its_setup_has_done_its_move(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    # The A+ buy's last target, 2031.50, is reached on the 08:02 bar.
    signal_row(db, "A-1", utc(2026, 11, 17, 7, 40), Direction.LONG, "A+", target=2031.5)
    h.run_until(utc(2026, 11, 17, 8, 4))
    placed, closed = h.notes.sent
    assert placed.endswith("follows the A+ BUY 07:40 UTC setup")
    assert "closed (setup_done, paper)" in closed
    (row,) = db.query("SELECT exit_reason, legs FROM scalp_trades")
    reasons = [leg["reason"] for leg in json.loads(row[1])]
    assert reasons[:3] == ["tp1", "tp2", "tp3"] and reasons[3:] == ["setup_done", "setup_done"]
    assert h.trader.scalper.follows == {}


def test_intraday_and_swing_follow_a_plus_to_b_setups(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db)
    signal_row(db, "B-1", utc(2026, 11, 17, 6, 0), Direction.SHORT, "B")
    now = h.clock.now()
    (setup,) = h.trader._mode_setups("intraday", now) or []  # noqa: SLF001
    assert (setup.grade, setup.direction) == ("B", Direction.SHORT)
    assert h.trader._mode_setups("swing", now) is not None  # noqa: SLF001
    assert h.trader._mode_setups("hft", now) == []  # noqa: SLF001 - HFT wants A or A+
    h.say("mode", "both")
    h.say("scalper", "all")
    context = h.trader._scalp_context(now)  # noqa: SLF001
    assert context.setups is not None and set(context.setups) == {"hft", "intraday", "swing"}
    assert "Scalper recovery (paper and demo only): martingale up to 0.5%" in h.trader.status()


def test_a_sliver_of_daily_budget_skips_the_scalp(unfiltered: Settings, db: Database) -> None:
    # 99.999 of the day's 100.00 already lost: what is left rounds to no risk at all.
    store(db, SCALP_PAPER_TABLE, "2026-11-17-SWS-000", utc(2026, 11, 17, 7, 0), -3.99996)
    h = Harness(unfiltered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(T0)
    assert h.notes.sent == []
    assert h.hft.decision is not None and h.hft.decision.text == (
        "skipped: the daily loss limit leaves no room for another scalp"
    )

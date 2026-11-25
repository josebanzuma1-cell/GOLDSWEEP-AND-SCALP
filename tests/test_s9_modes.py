"""S9 (D115): one, two or three scalper modes side by side; HFT in five quick parts; HFT
only with the main strategy's A or A+ setups, and only while they last.

The bot-level tests reuse the hand-made M1 market of tests/test_scalper_runner.py: quiet
bars at 2030 from 06:00 UTC on Tuesday 17 November 2026, a scripted HFT setup on the
07:59 bar (it closes at 08:00, inside London), then a rise through every target.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import HftConfig, Settings, SwitchConfig
from goldsweep.data.mock_broker import MockBroker, default_gold_spec
from goldsweep.execution.book import (
    EventKind,
    ExitReason,
    InvalidPlan,
    Management,
    TradePlan,
    plan_legs,
    previous_target,
    target_number,
)
from goldsweep.execution.executor import SCALP_LIVE_TABLE, SCALP_PAPER_TABLE
from goldsweep.live import SCALPER_MODE_KEY, SCALPER_MODES_KEY
from goldsweep.scalping import modes
from goldsweep.scalping.chart import PANEL_COMMANDS
from goldsweep.scalping.grade_filter import (
    GradedSetup,
    PathBar,
    accepts,
    allowed_directions,
    ended,
    hft_permission,
)
from goldsweep.scalping.logic import mode_management
from goldsweep.scalping.plan import LadderCosts, scalp_plan, scalp_risk
from goldsweep.scalping.runner import ModeSlot, ScalperRunner, risk_free
from goldsweep.storage.db import Database
from tests.helpers import only_spec_model, utc, with_grade_filters, without_grade_filter
from tests.test_scalp_book import T0 as BOOK_T0
from tests.test_scalp_book import bar, book
from tests.test_scalp_chart import PanelHarness, panel_lines
from tests.test_scalper_runner import (
    M1,
    MINUTE,
    SIGNAL_BAR,
    Harness,
    demo_harness,
    setup_at,
)

T0 = utc(2026, 11, 17, 8, 0)
WINDOW = dt.timedelta(minutes=60)


@pytest.fixture
def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "s9.sqlite3")
    yield database
    database.close()


@pytest.fixture
def unfiltered(settings: Settings) -> Settings:
    """The spec's model only, HFT's A/A+ filter off (for the sizing and switch tests)."""
    return without_grade_filter(only_spec_model(settings))


@pytest.fixture
def filtered(settings: Settings) -> Settings:
    """The spec's model only, HFT's A/A+ filter on (``a``, 60 minutes; shipped off
    since D117)."""
    return with_grade_filters(only_spec_model(settings))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_the_hft_settings(settings: Settings) -> None:
    hft = settings.config.scalper.hft
    assert (hft.split, hft.step_r, hft.grade_filter, hft.a_window_min) == (
        (0.2, 0.2, 0.2, 0.2, 0.2),
        0.25,
        "off",  # the owner switched the A/A+ filter off (D117); built-in "a"
        60,
    )
    assert HftConfig().grade_filter == "a"
    assert HftConfig(grade_filter=False).grade_filter == "off"  # YAML's unquoted off
    with pytest.raises(ValidationError, match="one to five"):
        HftConfig(split=(0.2,) * 6)
    with pytest.raises(ValidationError, match="add up to 1"):
        HftConfig(split=(0.2, 0.2))
    # The main strategy's and the other modes' splits stay three at most.
    assert settings.config.scalper.split == (0.4, 0.3, 0.3)


def test_the_switch_settings() -> None:
    assert SwitchConfig().modes == ()
    assert SwitchConfig(scalper_modes=("hft", "swing")).modes == ("hft", "swing")
    assert SwitchConfig(scalper_mode="intraday").modes == ("intraday",)  # S5-S8's key
    with pytest.raises(ValidationError, match="twice"):
        SwitchConfig(scalper_modes=("hft", "hft"))


def test_parsing_a_selection() -> None:
    assert modes.parse_modes("off") == ()
    assert modes.parse_modes("all") == ("hft", "intraday", "swing")
    assert modes.parse_modes("swing,hft") == ("hft", "swing")  # the fixed order
    assert modes.parse_modes("HFT swing") == ("hft", "swing")
    assert modes.parse_modes("hft+intraday") == ("hft", "intraday")
    assert modes.parse_modes("hft,hft") == ("hft",)
    assert modes.parse_modes("hft,tick") is None
    assert modes.parse_modes("") is None
    assert modes.modes_key([]) == "off"
    assert modes.modes_key(["swing", "hft"]) == "hft,swing"
    assert modes.all_selections() == (
        "off",
        "hft",
        "intraday",
        "swing",
        "hft,intraday",
        "hft,swing",
        "intraday,swing",
        "hft,intraday,swing",
    )
    assert modes.mode_state("swing", selected=("hft", "swing"), approved=False) == modes.PAPER
    assert modes.mode_state("intraday", selected="hft,swing", approved=False) == modes.OFF
    # Never an order, an approval or the kill switch from the chart.
    assert {c for c in PANEL_COMMANDS if not c.startswith(("scalper:", "mode:"))} == {
        "pause",
        "resume",
    }


# --------------------------------------------------------------------------- #
# Five parts, the ladder rule
# --------------------------------------------------------------------------- #

LONG5 = TradePlan(
    id="2026-11-17-HFT-009",
    time=BOOK_T0,
    direction=Direction.LONG,
    session="london",
    entry_mode="market",
    entry=2000.0,
    stop_loss=1998.0,
    take_profit_1=2000.5,
    take_profit_2=2001.0,
    take_profit_3=2001.5,
    take_profit_4=2002.0,
    take_profit_5=2002.5,
    expires_at=BOOK_T0 + MINUTE,
)
FIVE = (0.2, 0.2, 0.2, 0.2, 0.2)


def test_five_parts_in_whole_steps(settings: Settings) -> None:
    spec, risk = default_gold_spec(), scalp_risk(settings.config)
    legs = plan_legs(LONG5, 0.08, spec, risk, FIVE)
    # 0.08 lots: not 0.01 x 4 + 0.04, but 2/2/2/1/1 steps, the largest remainders first.
    assert [(leg.lots, leg.reason, leg.target) for leg in legs] == [
        (0.02, ExitReason.TP1, 2000.5),
        (0.02, ExitReason.TP2, 2001.0),
        (0.02, ExitReason.TP3, 2001.5),
        (0.01, ExitReason.TP4, 2002.0),
        (0.01, ExitReason.TP5, 2002.5),
    ]
    assert sum(leg.lots for leg in plan_legs(LONG5, 0.13, spec, risk, FIVE)) == pytest.approx(0.13)
    # Three lots steps for five targets: the first three parts, nothing below the minimum.
    assert [leg.reason for leg in plan_legs(LONG5, 0.03, spec, risk, FIVE)] == [
        ExitReason.TP1,
        ExitReason.TP2,
        ExitReason.TP3,
    ]
    # A plan with three targets folds the missing shares into TP3.
    three = dataclasses.replace(LONG5, take_profit_4=None, take_profit_5=None)
    assert [(leg.lots, leg.reason) for leg in plan_legs(three, 0.10, spec, risk, FIVE)] == [
        (0.02, ExitReason.TP1),
        (0.02, ExitReason.TP2),
        (0.06, ExitReason.TP3),
    ]


def test_targets_and_the_ladder_helpers() -> None:
    assert LONG5.targets == (2000.5, 2001.0, 2001.5, 2002.0, 2002.5)
    assert [target_number(r) for r in (ExitReason.TP1, ExitReason.TP5, ExitReason.STOP)] == [
        1,
        5,
        0,
    ]
    assert previous_target(LONG5, 1) is None  # after TP1: breakeven+, not a target
    assert previous_target(LONG5, 2) == 2000.5
    assert previous_target(LONG5, 4) == 2001.5
    with pytest.raises(InvalidPlan, match="TP4 is not on the winning side"):
        dataclasses.replace(LONG5, take_profit_4=1999.0)  # a target behind the entry


def test_the_ladder_on_paper(settings: Settings) -> None:
    """After TP1 the stop goes past the entry; after TP k to TP k-1 (D115)."""
    rules = Management(split=FIVE, breakeven_offset=0.1, stop_to_tp1_after_tp2=True)
    scalps = book(settings, rules)
    placed = scalps.submit(LONG5, spread_points=0.0, median_spread_points=None)
    assert placed.kind is EventKind.PLACED and placed.lots == 0.12  # 25.00 / 200 per lot
    events = []
    events += scalps.on_bar(bar(0, 2000.0, 2000.3, 1999.8, 2000.2))  # filled at the open
    events += scalps.on_bar(bar(1, 2000.2, 2000.6, 2000.1, 2000.5))  # TP1
    events += scalps.on_bar(bar(2, 2000.5, 2001.1, 2000.4, 2001.0))  # TP2
    events += scalps.on_bar(bar(3, 2001.0, 2001.6, 2000.9, 2001.5))  # TP3
    events += scalps.on_bar(bar(4, 2001.5, 2001.7, 2000.9, 2001.0))  # back to TP2
    moves = [(e.detail, e.price) for e in events if e.kind is EventKind.STOP_MOVED]
    assert moves == [
        ("breakeven", pytest.approx(2000.1)),
        ("trail", 2000.5),  # TP1, after TP2
        ("trail", 2001.0),  # TP2, after TP3
    ]
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed.trade is not None
    assert [(leg.lots, leg.reason) for leg in closed.trade.legs] == [
        (0.03, ExitReason.TP1),
        (0.03, ExitReason.TP2),
        (0.02, ExitReason.TP3),
        (0.02, ExitReason.TRAIL),  # TP4's and TP5's parts out at TP2: still a gain
        (0.02, ExitReason.TRAIL),
    ]
    assert closed.trade.net_pnl > 0


def test_three_part_scalps_keep_their_ladder(settings: Settings) -> None:
    """Intraday and swing scalps: after TP2 the stop goes to TP1, as before S9."""
    config = settings.config.scalper
    assert mode_management("intraday", config, 0.1).split == (0.4, 0.3, 0.3)
    assert mode_management("hft", config, 0.1).split == FIVE


def test_the_hft_plan_has_five_targets(settings: Settings) -> None:
    config = settings.config.scalper
    costs = LadderCosts(spread=0.2, slippage=0.1, commission=0.0)  # round trip 0.30
    setup = setup_at(SIGNAL_BAR, reference=2030.0, extreme=2026.3)  # stop 2026.10: R 3.90
    made = scalp_plan(
        setup,
        config,
        costs,
        plan_id="X",
        session="london",
        bar=MINUTE,
        split=config.hft.split,
        step_r=config.hft.step_r,
    )
    assert made.plan is not None and made.step_r == 0.25  # 0.975 clears 2 x 0.30
    assert made.plan.targets == pytest.approx((2030.975, 2031.95, 2032.925, 2033.9, 2034.875))
    # A tighter stop: 0.25 R would not clear the costs, so the step widens.
    tight = setup_at(SIGNAL_BAR, reference=2030.0, extreme=2028.3)  # R 1.90
    widened = scalp_plan(
        tight, config, costs, plan_id="Y", session="london", bar=MINUTE, split=FIVE, step_r=0.25
    )
    assert widened.plan is not None and widened.plan.take_profit_1 == pytest.approx(2030.6)
    # The S1 backtest keeps its three-part ladder when no split is given.
    assert scalp_plan(tight, config, costs, plan_id="Z", session=None, bar=MINUTE).plan.targets[
        3:
    ] == (None, None)


def test_the_ladder_on_the_demo_account(unfiltered: Settings, db: Database) -> None:
    """The same rule on MT5: the stop of the parts still open moves to TP2 after TP3."""
    demo, h = demo_harness(unfiltered, db)
    h.scalper_on()
    h.say("approve", "scalper hft")
    h.run_until(T0)
    assert len(demo.fake.open_positions) == 5
    for bid in (2031.00, 2031.95, 2031.95):  # TP1; TP2 and TP3; then a bar closes above TP2
        demo.fake.quote(bid)
        h.clock.advance(MINUTE)
        h.trader.scalper_tick()
    left = sorted(demo.fake.open_positions.values(), key=lambda p: p.ticket)
    assert [(p.tp, p.sl) for p in left] == [(2032.4, 2031.2), (2033.0, 2031.2)]


# --------------------------------------------------------------------------- #
# One, two or three modes
# --------------------------------------------------------------------------- #


def test_choosing_several_modes(unfiltered: Settings, db: Database) -> None:
    h = Harness(unfiltered, db)
    assert h.trader.scalper_modes == ()
    reply = h.say("scalper", "hft,swing")
    assert reply == (
        "✅ Scalper modes (2 side by side): HFT (M1, seconds to 15 minutes): on paper; "
        "swing scalping (M15, hours to two days): on paper. "
        "The scalper may not trade while /mode is main."
    )
    assert db.get_state(SCALPER_MODES_KEY) == ["hft", "swing"]
    assert h.trader.scalper_mode == "hft,swing"
    note = h.say("mode", "both")
    assert "Its hft mode runs on paper until /approve scalper hft" in note
    assert "its swing mode runs on paper until /approve scalper swing" in note
    status = h.trader.status()
    assert "HFT (M1, seconds to 15 minutes): on paper; swing scalping" in status
    assert h.say("scalper", "all").startswith("✅ Scalper modes (3 side by side)")
    assert h.say("scalper", "hft,tick").startswith("Send /scalper off")
    assert h.trader.scalper_modes == ("hft", "intraday", "swing")  # unchanged
    assert h.say("scalper", "off") == "✅ Scalper off: no scalper mode runs."
    assert h.trader.scalper_modes == ()
    context = h.trader._scalp_context(h.clock.now())  # noqa: SLF001
    assert (context.modes, context.may_open) == (frozenset(), False)


def test_the_old_single_mode_is_still_read(unfiltered: Settings, db: Database) -> None:
    db.set_state(SCALPER_MODE_KEY, "intraday")  # stored by S5-S8
    h = Harness(unfiltered, db)
    assert h.trader.scalper_modes == ("intraday",)
    h.say("scalper", "hft,intraday")
    assert h.trader.scalper_modes == ("hft", "intraday")  # the list wins from now on


def test_two_modes_trade_side_by_side(unfiltered: Settings, db: Database) -> None:
    h = Harness(unfiltered, db, [setup_at(SIGNAL_BAR)])
    h.say("mode", "both")
    h.say("scalper", "hft,swing")
    context = h.trader._scalp_context(h.clock.now())  # noqa: SLF001
    assert context.modes == frozenset({"hft", "swing"}) and context.demo_modes == frozenset()
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert any("closed (tp5, paper)" in note for note in h.notes.sent)
    picture = db.get_state("dashboard.snapshot")["scalper"]
    assert (picture["mode"], picture["modes"], picture["where"]) == (
        "hft,swing",
        ["hft", "swing"],
        "paper",
    )


def test_the_panel_buttons_send_a_list(unfiltered: Settings, db: Database, tmp_path: Path) -> None:
    """The panel's toggles send the whole list; the bot carries it out like /scalper."""
    h = PanelHarness(unfiltered, db, tmp_path, [])
    h.folder.mkdir(parents=True, exist_ok=True)
    stamp = int(h.clock.now().timestamp())
    (h.folder / "scalp_commands.json").write_text(
        f'{{"id":{stamp}01,"t":{stamp},"cmd":"scalper:intraday,swing"}}\r\n', encoding="ascii"
    )
    h.trader.dashboard_job()  # reads the chart's commands
    h.trader.scalper_tick()  # writes scalp.json
    assert h.trader.scalper_modes == ("intraday", "swing")
    assert any("Chart /scalper intraday,swing: ✅ Scalper modes (2" in n for n in h.notes.sent)
    head, _items = panel_lines(h.folder / "scalp.json")
    assert head["mode"] == "intraday,swing"


# --------------------------------------------------------------------------- #
# The rules the modes share in one account
# --------------------------------------------------------------------------- #


class StubBook:
    """Stands in for an executor: its open scalps and waiting orders."""

    def __init__(
        self, positions: list[dict[str, Any]] | None = None, orders: list[Any] | None = None
    ) -> None:
        self.open_positions = positions or []
        self.pending_orders = orders or []
        self.equity = 10_000.0


def position(direction: Direction, fill: float, stop: float) -> dict[str, Any]:
    return {"id": "x", "direction": direction.value, "fill_price": fill, "stop": stop}


def runner(settings: Settings, db: Database, **books: StubBook) -> ScalperRunner:
    slots = {
        name: ModeSlot(SimpleNamespace(name=name), book)  # type: ignore[arg-type]
        for name, book in books.items()
    }
    clock = ManualClock(T0)
    return ScalperRunner(
        settings, MockBroker(clock=clock), db, default_gold_spec(), clock, slots=slots
    )


def store(db: Database, table: str, scalp_id: str, exit_time: dt.datetime, r: float) -> None:
    db.execute(
        f"INSERT INTO {table} (id, direction, session, score, signal_time, fill_time, "  # noqa: S608
        "exit_time, fill_price, stop_loss, take_profit_1, take_profit_2, lots, risk_amount, "
        "exit_reason, net_pnl, r_multiple, legs) VALUES (?, 'LONG', 'london', 60, ?, ?, ?, "
        "2030, 2028, 2031, 2032, 0.1, 25, ?, ?, ?, '[]')",
        (
            scalp_id,
            int(exit_time.timestamp()) - 300,
            int(exit_time.timestamp()) - 300,
            int(exit_time.timestamp()),
            "stop" if r < 0 else "tp1",
            25.0 * r,
            r,
        ),
    )


def test_no_hedging_and_no_stacking_across_modes(settings: Settings, db: Database) -> None:
    long, short = Direction.LONG, Direction.SHORT
    swing = StubBook([position(short, 2030.0, 2032.0)])
    r = runner(settings, db, hft=StubBook(), swing=swing)
    hft = r.slots["hft"]
    assert r.shared_refusal(hft, long, demo=False, now=T0) == (
        "the swing scalp is open the other way (no hedging)"
    )
    swing.open_positions = [position(long, 2030.0, 2028.0)]
    assert r.shared_refusal(hft, long, demo=False, now=T0) == (
        "the swing scalp this way is not risk-free yet (no stacking)"
    )
    swing.open_positions = [position(long, 2030.0, 2030.1)]  # breakeven+: risk-free
    assert r.shared_refusal(hft, long, demo=False, now=T0) is None
    assert risk_free(position(short, 2030.0, 2029.9), short)
    swing.open_positions, swing.pending_orders = [], [LONG5]
    assert r.shared_refusal(hft, long, demo=False, now=T0) == (
        "the swing scalp this way is still waiting (no stacking)"
    )
    # A paper scalp of another mode does not block the demo account, and the reverse.
    swing.pending_orders = []
    swing.open_positions = [position(short, 2030.0, 2032.0)]
    assert r.shared_refusal(hft, long, demo=True, now=T0) is None


def test_the_daily_limits_are_shared(settings: Settings, db: Database) -> None:
    r = runner(settings, db, hft=StubBook(), intraday=StubBook(), swing=StubBook())
    hft = r.slots["hft"]
    # -4 R a day (1% over 0.25% a scalp), counted over every mode in the account.
    store(db, SCALP_PAPER_TABLE, "2026-11-17-INT-001", T0 - 3 * WINDOW, -1.0)
    store(db, SCALP_PAPER_TABLE, "2026-11-17-SWS-001", T0 - 2 * WINDOW, 1.5)
    assert r.shared_refusal(hft, Direction.LONG, demo=False, now=T0) is None
    for n in range(2, 6):
        store(db, SCALP_PAPER_TABLE, f"2026-11-17-INT-00{n}", T0 - WINDOW + n * MINUTE, -1.2)
    refusal = r.shared_refusal(hft, Direction.LONG, demo=False, now=T0)
    assert refusal is not None and refusal.startswith("3 losses in a row across the modes")
    # The demo account keeps its own count.
    assert r.shared_refusal(hft, Direction.LONG, demo=True, now=T0) is None


def test_the_daily_loss_and_count_across_modes(settings: Settings, db: Database) -> None:
    r = runner(settings, db, hft=StubBook(), swing=StubBook())
    hft = r.slots["hft"]
    for n, result in enumerate((-2.0, 0.5, -2.6)):
        store(db, SCALP_LIVE_TABLE, f"2026-11-17-SWS-00{n}", T0 - WINDOW + n * MINUTE, result)
    # In money since S10 (martingale makes R uneven): 1% of 10,000.
    assert r.shared_refusal(hft, Direction.LONG, demo=True, now=T0) == (
        "the daily loss limit across the modes (-102.50 of -100.00, 1%)"
    )
    db.execute("DELETE FROM scalp_live_trades")
    for n in range(8):
        store(db, SCALP_LIVE_TABLE, f"2026-11-17-SWS-01{n}", T0 - WINDOW + n * MINUTE, 0.1)
    assert r.shared_refusal(hft, Direction.LONG, demo=True, now=T0) == (
        "8 scalps today across the modes (the daily maximum)"
    )


# --------------------------------------------------------------------------- #
# HFT only with an A or A+ setup, while it lasts
# --------------------------------------------------------------------------- #


def a_setup(
    direction: Direction = Direction.LONG,
    *,
    time: dt.datetime = T0 - 20 * MINUTE,
    target: float | None = 2040.0,
    invalid: float | None = 2025.0,
    closed: bool = False,
    grade: str = "A+",
) -> GradedSetup:
    return GradedSetup("S", time, direction, grade, target, invalid, closed)


PATH = [PathBar(T0 - (30 - n) * MINUTE, 2030.2, 2029.8) for n in range(30)]


def test_when_a_setup_stops_allowing_hft() -> None:
    long = Direction.LONG
    assert ended(a_setup(), PATH, T0, WINDOW) is None
    assert ended(a_setup(target=2030.1), PATH, T0, WINDOW) == "reached its target"
    assert ended(a_setup(invalid=2029.9), PATH, T0, WINDOW) == "ran back through its sweep"
    assert ended(a_setup(closed=True), PATH, T0, WINDOW) == "has closed its trade"
    old = a_setup(time=T0 - 61 * MINUTE)
    assert ended(old, PATH, T0, WINDOW) == "is over 60 minutes old"
    # Bars before the signal's close do not count (they made the sweep).
    assert ended(a_setup(time=T0, invalid=2029.9), PATH, T0, WINDOW) is None
    forming = GradedSetup("F", T0 - 90 * MINUTE, long, "A+ possible", forming=True)
    assert ended(forming, PATH, T0, WINDOW) is None  # allowed while it is forming
    short = a_setup(Direction.SHORT, target=2020.0, invalid=2035.0)
    assert allowed_directions([a_setup(), short], PATH, T0, WINDOW) == {long, Direction.SHORT}


def test_the_reasons_hft_gives() -> None:
    long, short = Direction.LONG, Direction.SHORT
    assert hft_permission(long, [a_setup()], PATH, T0, WINDOW) is None
    assert hft_permission(short, [], PATH, T0, WINDOW) == (
        "HFT follows A or A+ setups: none to the sell side in the last 60 min"
    )
    assert hft_permission(short, [a_setup()], PATH, T0, WINDOW) == (
        "HFT follows A or A+ setups: the live one is A+ BUY 07:40 UTC, not a sell"
    )
    assert hft_permission(long, [a_setup(target=2030.1)], PATH, T0, WINDOW, "A+") == (
        "HFT follows A+ setups: the A+ BUY 07:40 UTC setup reached its target"
    )
    assert accepts("A+ (London playbook)", "a") and accepts("A", "a")
    assert not accepts("A", "a_plus") and not accepts("B", "a") and not accepts(None, "a")


def signal_row(
    db: Database,
    signal_id: str,
    time: dt.datetime,
    direction: Direction,
    grade: str,
    *,
    sweep_x: float = 2027.0,
    target: float = 2040.0,
) -> None:
    detail = {"grade": grade, "sweep_x": sweep_x, "take_profit_3": target}
    db.execute(
        "INSERT INTO signals (id, time, direction, session, score, entry, stop_loss, "
        "take_profit_1, take_profit_2, risk_reward, action, detail) "
        "VALUES (?, ?, ?, 'london', 80, 2030, 2026.5, 2035, 2038, 2, 'alert', ?)",
        (signal_id, int(time.timestamp()), direction.value, json.dumps(detail)),
    )


def test_hft_waits_for_an_a_setup(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    signal_row(db, "B-1", utc(2026, 11, 17, 7, 40), Direction.LONG, "B")  # not graded A
    h.run_until(T0)
    assert h.notes.sent == []
    assert h.hft.decision is not None and h.hft.decision.text == (
        "skipped: HFT follows A or A+ setups: none to the buy side in the last 60 min"
    )


def test_hft_follows_a_live_a_plus_setup(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    signal_row(db, "A-1", utc(2026, 11, 17, 7, 40), Direction.LONG, "A+ (London playbook)")
    h.run_until(utc(2026, 11, 17, 8, 10))
    placed = h.notes.sent[0]
    assert placed.startswith("⚡ Scalp HFT LONG 2026-11-17-HFT-001 (paper")
    assert "closed (tp5, paper)" in h.notes.sent[-1]


def test_hft_stops_when_the_setup_has_done_its_move(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    # Its last target (2030.10) was reached by the quiet bars after it formed.
    signal_row(db, "A-2", utc(2026, 11, 17, 7, 40), Direction.LONG, "A", target=2030.1)
    h.run_until(T0)
    assert h.notes.sent == []
    assert h.hft.decision is not None and h.hft.decision.text == (
        "skipped: HFT follows A or A+ setups: the A BUY 07:40 UTC setup reached its target"
    )


def test_hft_stops_when_the_setup_fails(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    signal_row(db, "A-3", utc(2026, 11, 17, 7, 40), Direction.LONG, "A", sweep_x=2029.9)
    h.run_until(T0)
    assert h.notes.sent == []
    assert h.hft.decision is not None
    assert h.hft.decision.text.endswith("the A BUY 07:40 UTC setup ran back through its sweep")


def test_an_a_plus_setup_forming_allows_hft(filtered: Settings, db: Database) -> None:
    h = Harness(filtered, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    raid = SimpleNamespace(
        model="sweep_mss", since=utc(2026, 11, 17, 7, 50), direction=Direction.LONG
    )
    h.trader._forming = [(raid, True)]  # noqa: SLF001 - as the last M5 bar left it
    setups = h.trader._hft_setups(h.clock.now())  # noqa: SLF001
    assert setups is not None and [(s.grade, s.forming) for s in setups] == [("A+ possible", True)]
    h.run_until(T0)
    assert h.notes.sent and h.notes.sent[0].startswith("⚡ Scalp HFT LONG")


def test_the_filter_can_be_switched_off(unfiltered: Settings, db: Database) -> None:
    h = Harness(unfiltered, db)
    assert h.trader._hft_setups(h.clock.now()) is None  # noqa: SLF001
    h.say("scalper", "swing")
    context = h.trader._scalp_context(h.clock.now())  # noqa: SLF001
    assert context.hft_setups is None  # HFT not selected: nothing to filter


def test_market_bars_are_the_runner_path(unfiltered: Settings, db: Database) -> None:
    h = Harness(unfiltered, db)
    h.run_until(utc(2026, 11, 17, 7, 35))  # the 07:34 bar closed at 07:35
    path = h.trader.scalper.path
    newest = utc(2026, 11, 17, 7, 34)
    assert path[-1].time == newest and len(path) == len(M1.loc[:newest])

"""The trade rules shared by backtests, paper and live trading (execution/book.py)."""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import DayBoundaryConfig, NewsConfig, RiskConfig, SessionsConfig
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import (
    Bar,
    EventKind,
    ExitReason,
    InvalidPlan,
    Outcome,
    TradeBook,
    TradePlan,
    breakeven_due,
    entry_deadline,
    plan_legs,
    third_target,
    to_ns,
    trailed_stop,
)
from goldsweep.risk.risk_manager import NewsEvent
from tests.helpers import utc

GOLD = default_gold_spec()  # volume step and minimum 0.01
SESSIONS = SessionCalculator(SessionsConfig())  # the spec's windows
FIVE = dt.timedelta(minutes=5)


def plan(**changes: object) -> TradePlan:
    values: dict[str, object] = {
        "id": "T-01",
        "time": utc(2026, 11, 18, 8, 0),
        "direction": Direction.LONG,
        "session": "london",
        "entry_mode": "fvg_mid",
        "entry": 2029.0,
        "stop_loss": 2027.0,
        "take_profit_1": 2033.0,
        "take_profit_2": 2034.0,
        "take_profit_3": 2035.0,
        "expires_at": utc(2026, 11, 18, 8, 30),
    }
    values.update(changes)
    return TradePlan(**values)  # type: ignore[arg-type]


def split(the_plan: TradePlan, lots: float, **risk: float) -> list[tuple[float, str]]:
    legs = plan_legs(the_plan, lots, GOLD, RiskConfig(**risk))
    return [(leg.lots, leg.reason.value) for leg in legs]


def test_the_spec_split() -> None:
    assert split(plan(), 0.10) == [(0.05, "tp1"), (0.03, "tp2"), (0.02, "tp3")]
    assert split(plan(), 0.25) == [(0.12, "tp1"), (0.07, "tp2"), (0.06, "tp3")]


def test_missing_targets_hand_their_share_to_an_earlier_one() -> None:
    assert split(plan(take_profit_3=None), 0.10) == [(0.05, "tp1"), (0.05, "tp2")]
    assert split(plan(take_profit_2=None), 0.10) == [(0.08, "tp1"), (0.02, "tp3")]
    assert split(plan(take_profit_2=None, take_profit_3=None), 0.10) == [(0.1, "tp1")]


def test_small_positions_fold_from_the_far_end() -> None:
    assert split(plan(), 0.01) == [(0.01, "tp1")]
    assert split(plan(), 0.02) == [(0.01, "tp1"), (0.01, "tp2")]
    assert split(plan(), 0.03) == [(0.01, "tp1"), (0.02, "tp2")]


def test_split_settings() -> None:
    assert split(plan(), 0.10, partial_close_at_tp1_pct=100) == [(0.1, "tp1")]
    assert split(plan(), 0.10, partial_close_at_tp1_pct=0, partial_close_at_tp2_pct=0) == [
        (0.1, "tp3")
    ]
    assert split(plan(), 0.10, partial_close_at_tp1_pct=60, partial_close_at_tp2_pct=60) == [
        (0.06, "tp1"),
        (0.04, "tp2"),
    ]


def test_third_target() -> None:
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2034.0), 3.0) == 2035.0
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2037.0), 3.0) is None
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, None), 3.0) == 2035.0
    assert third_target(Direction.SHORT, 2029.0, 2031.0, (2025.0, 2024.0), 3.0) == 2023.0
    assert third_target(Direction.LONG, 2029.0, 2027.0, (2033.0, 2034.0), 0.0) is None


def test_fixed_r_targets_replace_the_pools() -> None:
    original = plan()  # long 2029.00, stop 2027.00: one R is 2.00
    assert original.with_exit_targets(RiskConfig()) is original  # pools: unchanged
    one = original.with_exit_targets(RiskConfig(target_mode="fixed_r", fixed_targets_r=(3.0,)))
    assert one.targets == (2035.0, None, None) and one.risk_reward_1 == 3.0
    assert split(one, 0.10) == [(0.10, "tp1")]  # one target: everything closes there
    three = original.with_exit_targets(
        RiskConfig(target_mode="fixed_r", fixed_targets_r=(2.0, 3.0, 5.0))
    )
    assert three.targets == (2033.0, 2035.0, 2039.0)
    assert split(three, 0.10) == [(0.05, "tp1"), (0.03, "tp2"), (0.02, "tp3")]
    short = plan(
        direction=Direction.SHORT,
        entry=2029.0,
        stop_loss=2031.0,
        take_profit_1=2025.0,
        take_profit_2=2024.0,
        take_profit_3=2023.0,
    )
    fixed = short.with_exit_targets(RiskConfig(target_mode="fixed_r", fixed_targets_r=(2.5,)))
    assert fixed.targets == (2024.0, None, None)


@pytest.mark.parametrize("values", [(), (3.0, 2.0), (1.0, 2.0, 3.0, 4.0), (0.0,), (2.0, 2.0)])
def test_fixed_targets_must_be_one_to_three_rising_values(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError, match="fixed_targets_r"):
        RiskConfig(target_mode="fixed_r", fixed_targets_r=values)


def test_tp3_is_checked_and_saved() -> None:
    with pytest.raises(InvalidPlan, match="TP3"):
        plan(take_profit_3=2028.0)
    original = plan()
    assert TradePlan.from_state(original.to_state()) == original
    old = original.to_state()
    del old["take_profit_3"]  # state saved before Phase 6
    assert TradePlan.from_state(old).take_profit_3 is None


def test_breakeven_and_trailing_rules() -> None:
    common = {"fill_price": 2029.0, "initial_stop": 2027.0, "tp1_done": False, "at_r": 1.0}
    assert breakeven_due(long=True, best_price=2031.0, **common)  # type: ignore[arg-type]
    assert not breakeven_due(long=True, best_price=2030.9, **common)  # type: ignore[arg-type]
    assert breakeven_due(long=True, best_price=2029.5, **{**common, "tp1_done": True})  # type: ignore[arg-type]
    # D84: at_r 0 never moves the stop by distance, but reaching TP1 still does.
    never = {**common, "at_r": 0.0}
    assert not breakeven_due(long=True, best_price=2040.0, **never)  # type: ignore[arg-type]
    assert breakeven_due(long=True, best_price=2029.5, **{**never, "tp1_done": True})  # type: ignore[arg-type]
    assert breakeven_due(long=True, best_price=2035.0, **{**common, "at_r": 3.0})  # type: ignore[arg-type]
    assert not breakeven_due(long=True, best_price=2034.9, **{**common, "at_r": 3.0})  # type: ignore[arg-type]
    nan = float("nan")
    assert (
        trailed_stop(
            long=True, stop=2029.0, swing_low=2030.5, swing_high=nan, close=2031.0, spread=0.2
        )
        == 2030.5
    )
    assert (
        trailed_stop(
            long=True, stop=2029.0, swing_low=2031.5, swing_high=nan, close=2031.0, spread=0.2
        )
        is None
    )
    assert trailed_stop(
        long=False, stop=2031.0, swing_low=nan, swing_high=2029.5, close=2028.0, spread=0.2
    ) == pytest.approx(2029.7)
    assert ExitReason.TP3.value == "tp3"
    assert dt.timedelta(0) == dt.timedelta(0)


# --------------------------------------------------------------------------- #
# Waiting entries and the clock (Phase 9: no entry may fill in a blocked time)
# --------------------------------------------------------------------------- #

TUESDAY_1635_NY = utc(2026, 11, 17, 21, 35)  # the rollover block starts at 16:45 New York


def test_entry_deadline_stops_before_the_rollover_block() -> None:
    deadline = entry_deadline(TUESDAY_1635_NY, TUESDAY_1635_NY + 6 * FIVE, sessions=SESSIONS)
    assert deadline == (utc(2026, 11, 17, 21, 45), "the rollover block")


def test_entry_deadline_stops_before_the_friday_cutoff() -> None:
    placed = utc(2026, 11, 20, 16, 50)  # Friday 11:50 New York
    assert entry_deadline(placed, placed + 6 * FIVE, sessions=SESSIONS) == (
        utc(2026, 11, 20, 17, 0),
        "the Friday cutoff",
    )


def test_entry_deadline_stops_before_a_news_blackout() -> None:
    placed = utc(2026, 11, 18, 13, 0)
    cpi = NewsEvent(utc(2026, 11, 18, 13, 50), "US CPI")
    small = NewsEvent(utc(2026, 11, 18, 13, 10), "Redbook", impact="low")  # not high impact
    over = NewsEvent(utc(2026, 11, 18, 11, 0), "Earlier release")  # its blackout is long gone
    deadline = entry_deadline(
        placed,
        placed + 24 * FIVE,
        sessions=SESSIONS,
        events=[small, over, cpi],
        blackout_before_min=30,
    )
    assert deadline == (utc(2026, 11, 18, 13, 20), "the news blackout for US CPI")


def test_entry_deadline_leaves_an_ordinary_order_alone() -> None:
    placed = utc(2026, 11, 18, 8, 0)  # Wednesday, London kill zone
    assert entry_deadline(placed, placed + 24 * FIVE, sessions=SESSIONS) == (
        placed + 24 * FIVE,
        "",
    )
    original = plan()
    assert original.with_deadline(sessions=SESSIONS) is original
    assert (original.expiry_outcome, original.withdrawal_detail) == (
        Outcome.EXPIRED,
        "the entry was not reached",
    )


def test_a_cut_deadline_is_saved_with_its_reason() -> None:
    late = plan(time=TUESDAY_1635_NY, expires_at=TUESDAY_1635_NY + 6 * FIVE).with_deadline(
        sessions=SESSIONS
    )
    assert (late.expires_at, late.expiry_note) == (utc(2026, 11, 17, 21, 45), "the rollover block")
    assert late.withdrawal_detail == "withdrawn before the rollover block"
    assert late.expiry_outcome is Outcome.WITHDRAWN
    assert TradePlan.from_state(late.to_state()) == late
    old = late.to_state()
    del old["expiry_note"]  # state saved before Phase 9
    assert TradePlan.from_state(old).expiry_note == ""


def a_book(**risk: int) -> TradeBook:
    return TradeBook(
        GOLD,
        RiskConfig(**risk),
        NewsConfig(),
        DayBoundaryConfig(),
        equity=10_000.0,
        slippage_points=0,
        commission_per_lot=0,
        trail_after_tp1=False,
    )


def bar_at(opened: dt.datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(to_ns(opened), to_ns(opened + FIVE), o, h, low, c, 0.2)


def test_the_book_withdraws_a_waiting_entry_at_its_deadline() -> None:
    book = a_book()
    late = plan(time=TUESDAY_1635_NY, expires_at=TUESDAY_1635_NY + 6 * FIVE).with_deadline(
        sessions=SESSIONS
    )
    assert book.submit(late, spread_points=20, median_spread_points=20).kind is EventKind.PLACED
    quiet = (2030.0, 2030.2, 2029.8, 2030.0)  # ask low 2030.00: above the 2029.00 entry
    assert book.on_bar(bar_at(utc(2026, 11, 17, 21, 35), *quiet)) == []
    assert book.on_bar(bar_at(utc(2026, 11, 17, 21, 40), *quiet)) == []
    would_fill = bar_at(utc(2026, 11, 17, 21, 45), 2029.3, 2029.4, 2028.0, 2029.0)
    (gone,) = book.on_bar(would_fill)
    assert (gone.kind, gone.outcome, gone.detail) == (
        EventKind.EXPIRED,
        Outcome.WITHDRAWN,
        "withdrawn before the rollover block",
    )
    assert book.idle and book.manager.open_positions == 0


def test_withdraw_orders_leaves_positions_and_market_orders_alone() -> None:
    book = a_book(max_open_positions=3)
    book.submit(plan(), spread_points=20, median_spread_points=20)
    book.on_bar(bar_at(utc(2026, 11, 18, 8, 0), 2029.3, 2029.4, 2028.5, 2029.0))  # T-01 fills
    later = utc(2026, 11, 18, 8, 5)
    book.submit(
        plan(id="T-02", time=later, expires_at=later + 6 * FIVE),
        spread_points=20,
        median_spread_points=20,
    )
    book.submit(
        plan(id="T-03", time=later, expires_at=later + 6 * FIVE, entry_mode="market"),
        spread_points=20,
        median_spread_points=20,
    )
    events = book.withdraw_orders(later, "withdrawn before the news blackout for US CPI")
    assert [(e.kind, e.outcome, e.plan.id) for e in events] == [
        (EventKind.EXPIRED, Outcome.WITHDRAWN, "T-02")
    ]
    assert [o.id for o in book.orders] == ["T-03"] and len(book.positions) == 1

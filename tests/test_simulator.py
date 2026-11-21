"""Trade simulation: fills, exits, management and costs, on hand-made bars.

Every scenario uses one long setup unless it says otherwise: entry 2029.00 (a buy
limit), stop 2027.00 (R = 2.00), TP1 2033.00 (2R), TP2 2037.00 (4R). With 10,000
equity and 0.5% risk that is 50.00 at risk: 0.25 lots, split 0.12 at TP1 and 0.13
at TP2. Bars are bid prices with a 0.20 spread, so the buy limit fills only when
a bar's low reaches 2028.80.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from goldsweep.analysis.bias import Bias
from goldsweep.analysis.signals import Signal
from goldsweep.analysis.structure import Direction
from goldsweep.backtest.simulator import (
    ExitReason,
    Outcome,
    PriceSeries,
    SignalRecord,
    SimulationResult,
    Simulator,
    TrailLevels,
    combine_series,
)
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import (
    BacktestConfig,
    CostConfig,
    DayBoundaryConfig,
    NewsConfig,
    RiskConfig,
    SessionsConfig,
)
from goldsweep.core.types import Timeframe
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.risk.risk_manager import NewsEvent
from tests.helpers import Row, bars
from tests.test_signals import SIGNAL_TIME

GOLD = default_gold_spec()
BAR = dt.timedelta(minutes=5)
FREE = BacktestConfig(
    trail_after_tp1=False, costs=CostConfig(slippage_points=0, commission_per_lot=0)
)
QUIET: Row = (2030.0, 2030.2, 2029.8, 2030.0)
NO_FILL: Row = (2030.0, 2030.1, 2029.15, 2029.3)  # ask low 2029.35: above the entry
FILL: Row = (2029.3, 2029.4, 2028.75, 2029.0)  # ask low 2028.95: fills at 2029.00
TO_1R: Row = (2029.0, 2031.2, 2028.9, 2031.0)  # +1R reached: stop to breakeven next bar
TO_TP1: Row = (2031.0, 2033.1, 2030.9, 2033.0)
TO_TP2: Row = (2033.0, 2037.2, 2032.9, 2037.0)


def make_signal(
    base: Signal,
    *,
    direction: Direction = Direction.LONG,
    entry: float = 2029.0,
    stop: float = 2027.0,
    tp1: float = 2033.0,
    tp2: float | None = 2037.0,
    time: dt.datetime = SIGNAL_TIME,
    entry_mode: str = "fvg_mid",
    number: int = 1,
) -> Signal:
    return dataclasses.replace(
        base,
        id=f"TEST-{number:02d}",
        time=time,
        direction=direction,
        entry=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        risk_reward_1=abs(tp1 - entry) / abs(entry - stop),
        entry_mode=entry_mode,
        expires_at=time + 6 * BAR,
        bias=dataclasses.replace(base.bias, bias=Bias.NEUTRAL),
    )


TWO_TARGETS = RiskConfig(tp3_r=0)


def record(signal: Signal) -> SignalRecord:
    return SignalRecord(signal, spread_points=20.0, median_spread_points=20.0)


def simulate(
    rows: Sequence[Row],
    signals: Sequence[Signal],
    *,
    backtest: BacktestConfig = FREE,
    risk: RiskConfig | None = None,
    trail: int | None = None,
    select: object = None,
    events: Sequence[NewsEvent] = (),
    signal_time: dt.datetime = SIGNAL_TIME,
    sessions: SessionCalculator | None = None,
    entry_lifetime: dt.timedelta | None = None,
) -> SimulationResult:
    """Bars from the signal bar (closing at ``signal_time``) on, then one run.

    The default risk settings have no TP3 (the two-target split of spec 6); the
    three-way split of spec 7b has its own tests below.
    """
    frame = bars(rows, signal_time - BAR)
    series = PriceSeries.from_candles(frame, Timeframe.M5, point=GOLD.point)
    levels = TrailLevels.from_candles(frame, Timeframe.M5, trail) if trail else None
    simulator = Simulator(
        series,
        GOLD,
        risk or TWO_TARGETS,
        NewsConfig(),
        backtest,
        DayBoundaryConfig(),
        trail=levels,
        events=events,
        sessions=sessions,
        entry_lifetime=entry_lifetime,
    )
    return simulator.run(
        [record(signal) for signal in signals],
        select=select or (lambda _record: None),  # type: ignore[arg-type]
        start=SIGNAL_TIME - dt.timedelta(days=1),
        end=SIGNAL_TIME + dt.timedelta(days=1),
    )


def only_trade(result: SimulationResult):  # noqa: ANN201
    assert len(result.trades) == 1, [(o.outcome, o.reason) for o in result.outcomes]
    return result.trades[0]


# --------------------------------------------------------------------------- #
# Winners and losers
# --------------------------------------------------------------------------- #


def test_textbook_winner_takes_half_at_tp1_and_the_rest_at_tp2(base_signal: Signal) -> None:
    result = simulate([QUIET, NO_FILL, FILL, TO_1R, TO_TP1, TO_TP2], [make_signal(base_signal)])
    trade = only_trade(result)
    assert trade.fill_time == SIGNAL_TIME + BAR  # the second bar after the signal
    assert (trade.fill_price, trade.lots) == (2029.0, 0.25)
    assert [(leg.lots, leg.exit_price, leg.reason) for leg in trade.legs] == [
        (0.12, 2033.0, ExitReason.TP1),
        (0.13, 2037.0, ExitReason.TP2),
    ]
    # 4.00 x 0.12 x 100 + 8.00 x 0.13 x 100 = 152.00 on 50.00 risked.
    assert trade.net_pnl == pytest.approx(152.0)
    assert trade.risk_amount == pytest.approx(50.0)
    assert trade.r_multiple == pytest.approx(3.04)
    assert result.final_equity == pytest.approx(10_152.0)
    assert [outcome.outcome for outcome in result.outcomes] == [Outcome.TRADED]
    assert result.equity[-1] == (trade.exit_time, pytest.approx(10_152.0))


def test_stop_loss_costs_one_r_plus_slippage_and_commission(base_signal: Signal) -> None:
    stopped: Row = (2029.0, 2029.1, 2026.9, 2027.2)
    result = simulate([QUIET, FILL, stopped], [make_signal(base_signal)], backtest=BacktestConfig())
    trade = only_trade(result)
    # Both parts of the split position (0.12 + 0.13 lots) leave at the stop.
    assert {leg.reason for leg in trade.legs} == {ExitReason.STOP}
    assert [leg.lots for leg in trade.legs] == [0.12, 0.13]
    for leg in trade.legs:
        assert leg.exit_price == pytest.approx(2026.9)  # the stop, 10 points of slippage worse
    assert trade.commission == pytest.approx(7.0 * 0.25)
    assert trade.net_pnl == pytest.approx(-2.1 * 0.25 * 100 - 1.75)
    assert trade.r_multiple == pytest.approx(-54.25 / 50.0)


def test_stop_and_target_in_one_bar_counts_as_the_stop(base_signal: Signal) -> None:
    both: Row = (2029.0, 2037.5, 2026.5, 2030.0)
    trade = only_trade(simulate([QUIET, FILL, both], [make_signal(base_signal)]))
    assert trade.exit_reason is ExitReason.STOP and trade.net_pnl < 0


def test_the_fill_bar_can_stop_out_but_not_take_profit(base_signal: Signal) -> None:
    fill_and_stop: Row = (2030.0, 2030.1, 2026.8, 2027.5)
    trade = only_trade(simulate([QUIET, fill_and_stop], [make_signal(base_signal)]))
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_time == trade.fill_time + BAR  # stopped on the fill bar itself

    fill_and_spike: Row = (2030.0, 2033.5, 2028.5, 2033.2)  # also trades through TP1
    flat: Row = (2033.2, 2033.4, 2033.0, 2033.2)
    trade = only_trade(simulate([QUIET, fill_and_spike, flat], [make_signal(base_signal)]))
    tp1 = trade.legs[0]
    assert tp1.reason is ExitReason.TP1
    assert tp1.exit_time == trade.fill_time + 2 * BAR  # one bar later, not on the fill bar


def test_gap_through_the_stop_exits_at_the_open(base_signal: Signal) -> None:
    gap: Row = (2026.0, 2026.5, 2025.5, 2026.2)
    trade = only_trade(simulate([QUIET, FILL, gap], [make_signal(base_signal)]))
    assert trade.legs[0].exit_price == 2026.0  # worse than the stop: no slippage configured
    assert trade.r_multiple == pytest.approx(-1.5)


def test_breakeven_after_one_r_then_a_pullback(base_signal: Signal) -> None:
    back: Row = (2031.0, 2031.1, 2028.5, 2028.8)
    result = simulate(
        [QUIET, FILL, TO_1R, back], [make_signal(base_signal)], backtest=BacktestConfig()
    )
    trade = only_trade(result)
    assert trade.exit_reason is ExitReason.BREAKEVEN
    assert trade.legs[0].exit_price == pytest.approx(2028.9)  # entry minus slippage
    assert trade.net_pnl == pytest.approx(-0.1 * 0.25 * 100 - 1.75)  # costs only


def test_breakeven_waits_for_the_next_bar(base_signal: Signal) -> None:
    up_and_down: Row = (2029.0, 2031.5, 2027.5, 2028.0)  # +1R and back below entry, same bar
    trade = only_trade(simulate([QUIET, FILL, up_and_down], [make_signal(base_signal)]))
    assert trade.exit_reason is not ExitReason.BREAKEVEN  # the stop was still at 2027
    stopped_later: Row = (2028.0, 2028.2, 2026.0, 2026.5)
    trade = only_trade(
        simulate([QUIET, FILL, up_and_down, stopped_later], [make_signal(base_signal)])
    )
    assert trade.exit_reason is ExitReason.BREAKEVEN  # moved after the bar: exit at entry


def test_trailing_stop_after_tp1(base_signal: Signal) -> None:
    rows = [
        QUIET,
        FILL,
        TO_1R,
        (2031.0, 2033.2, 2030.9, 2033.0),  # TP1
        (2033.0, 2033.2, 2030.5, 2031.0),  # dips: swing low 2030.50 ...
        (2031.0, 2034.0, 2031.0, 2033.8),  # ... confirmed here: the stop trails to it
        (2033.8, 2034.0, 2030.0, 2030.2),  # back through 2030.50
    ]
    trailing = FREE.model_copy(update={"trail_after_tp1": True})
    signal = make_signal(base_signal, tp2=2041.0)
    trade = only_trade(simulate(rows, [signal], backtest=trailing, trail=1))
    assert [leg.reason for leg in trade.legs] == [ExitReason.TP1, ExitReason.TRAIL]
    assert trade.legs[1].exit_price == pytest.approx(2030.5)


def test_whole_position_at_tp1_without_a_second_target(base_signal: Signal) -> None:
    trade = only_trade(simulate([QUIET, FILL, TO_1R, TO_TP1], [make_signal(base_signal, tp2=None)]))
    assert [(leg.lots, leg.reason) for leg in trade.legs] == [(0.25, ExitReason.TP1)]


def test_partial_close_settings(base_signal: Signal) -> None:
    rows = [QUIET, FILL, TO_1R, TO_TP1, TO_TP2]
    all_at_tp2 = only_trade(
        simulate(
            rows, [make_signal(base_signal)], risk=RiskConfig(partial_close_at_tp1_pct=0, tp3_r=0)
        )
    )
    assert [(leg.lots, leg.reason) for leg in all_at_tp2.legs] == [(0.25, ExitReason.TP2)]
    all_at_tp1 = only_trade(
        simulate(
            rows, [make_signal(base_signal)], risk=RiskConfig(partial_close_at_tp1_pct=100, tp3_r=0)
        )
    )
    assert [(leg.lots, leg.reason) for leg in all_at_tp1.legs] == [(0.25, ExitReason.TP1)]


def test_data_ending_closes_the_position(base_signal: Signal) -> None:
    trade = only_trade(simulate([QUIET, FILL, QUIET], [make_signal(base_signal)]))
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.legs[0].exit_price == 2030.0  # the last close (bid)


# --------------------------------------------------------------------------- #
# Shorts
# --------------------------------------------------------------------------- #


def short_signal(base: Signal) -> Signal:
    return make_signal(
        base, direction=Direction.SHORT, entry=2031.0, stop=2033.0, tp1=2027.2, tp2=2023.2
    )


def test_short_fills_on_the_bid_and_is_stopped_on_the_ask(base_signal: Signal) -> None:
    fill: Row = (2030.0, 2031.1, 2029.9, 2030.9)  # bid reaches 2031.00
    ask_hits_stop: Row = (2030.9, 2032.85, 2030.5, 2032.0)  # ask high 2033.05
    trade = only_trade(simulate([QUIET, fill, ask_hits_stop], [short_signal(base_signal)]))
    assert trade.direction is Direction.SHORT
    assert trade.fill_price == 2031.0
    assert trade.exit_reason is ExitReason.STOP
    assert trade.legs[0].exit_price == 2033.0
    assert trade.r_multiple == pytest.approx(-1.0)


def test_short_targets_are_reached_by_the_ask(base_signal: Signal) -> None:
    fill: Row = (2030.0, 2031.1, 2029.9, 2030.9)
    near: Row = (2030.9, 2031.0, 2027.05, 2027.3)  # ask low 2027.25: not yet 2027.20
    tp1: Row = (2027.3, 2027.5, 2026.9, 2027.1)  # ask low 2027.10
    tp2: Row = (2027.1, 2027.2, 2022.9, 2023.0)
    trade = only_trade(simulate([QUIET, fill, near, tp1, tp2], [short_signal(base_signal)]))
    assert [(leg.exit_price, leg.reason) for leg in trade.legs] == [
        (2027.2, ExitReason.TP1),
        (2023.2, ExitReason.TP2),
    ]
    assert trade.legs[0].exit_time == SIGNAL_TIME + 3 * BAR


# --------------------------------------------------------------------------- #
# Signals that do not become trades
# --------------------------------------------------------------------------- #


def test_entry_order_expires_after_six_bars(base_signal: Signal) -> None:
    result = simulate([QUIET] + [NO_FILL] * 8, [make_signal(base_signal)])
    assert result.trades == []
    (outcome,) = result.outcomes
    assert outcome.outcome is Outcome.EXPIRED


def test_the_configured_entry_lifetime_replaces_the_signals_own(base_signal: Signal) -> None:
    rows = [QUIET] + [NO_FILL] * 7 + [FILL, TO_TP1]  # the dip comes 40 minutes after the signal
    assert simulate(rows, [make_signal(base_signal)]).trades == []  # 6 bars: expired at 08:25
    longer = simulate(rows, [make_signal(base_signal)], entry_lifetime=12 * BAR)
    assert only_trade(longer).fill_time == SIGNAL_TIME + 7 * BAR
    shorter = simulate([QUIET, NO_FILL, FILL], [make_signal(base_signal)], entry_lifetime=BAR)
    assert [o.outcome for o in shorter.outcomes] == [Outcome.EXPIRED]


def refined_run(base_signal: Signal, rows: Sequence[Row]) -> SimulationResult:
    """The M1 fixture of tests/test_refine.py, traded with M1 refinement (D83)."""
    from goldsweep.analysis.refine import M1Refinement
    from tests.test_refine import CONTEXT, PLAIN, SETUP_TIME

    frame = bars(CONTEXT + list(rows), SETUP_TIME - 30 * dt.timedelta(minutes=1), minutes=1)
    simulator = Simulator(
        PriceSeries.from_candles(frame, Timeframe.M1, point=GOLD.point),
        GOLD,
        TWO_TARGETS,
        NewsConfig(),
        FREE,
        DayBoundaryConfig(),
        refinement=M1Refinement(PLAIN, frame),
    )
    signal = make_signal(base_signal, time=SETUP_TIME, stop=2026.0, tp1=2045.0, tp2=2050.0)
    return simulator.run(
        [record(signal)],
        select=lambda _record: None,
        start=SETUP_TIME - dt.timedelta(days=1),
        end=SETUP_TIME + dt.timedelta(days=1),
    )


def test_a_refined_entry_is_placed_when_the_m1_turn_closes(base_signal: Signal) -> None:
    from tests.test_refine import PULLBACK

    to_tp1: Row = (2031.9, 2045.2, 2031.8, 2045.0)
    to_tp2: Row = (2045.0, 2050.3, 2044.9, 2050.0)
    trade = only_trade(refined_run(base_signal, [*PULLBACK, to_tp1, to_tp2]))
    # The turn closed at 08:10; the market order fills at the 08:10 open on the ask.
    assert trade.fill_time == dt.datetime(2026, 11, 17, 8, 10, tzinfo=dt.UTC)
    assert trade.fill_price == pytest.approx(2031.6 + 0.2)
    assert trade.stop_loss == pytest.approx(2028.3)  # the refined stop, not the M5 one
    assert [leg.reason for leg in trade.legs] == [ExitReason.TP1, ExitReason.TP2]


def test_no_trade_when_the_m1_turn_never_comes(base_signal: Signal) -> None:
    from tests.test_refine import PULLBACK

    result = refined_run(base_signal, PULLBACK[:7] + quiet_rows(30, 2029.0))
    assert result.trades == []
    assert [(o.outcome, o.reason) for o in result.outcomes] == [
        (Outcome.NO_TRIGGER, "no M1 turn inside the gap in time")
    ]


def quiet_rows(count: int, price: float) -> list[Row]:
    return [(price, price + 0.1, price - 0.1, price)] * count


def test_order_cancelled_when_tp1_comes_first(base_signal: Signal) -> None:
    runaway: Row = (2030.0, 2033.5, 2029.9, 2033.2)
    result = simulate([QUIET, runaway, FILL], [make_signal(base_signal)])
    assert result.trades == []
    assert result.outcomes[0].outcome is Outcome.TP1_FIRST


#: Tuesday 16:35 New York time (EST): the rollover block starts ten minutes later.
BEFORE_ROLLOVER = dt.datetime(2026, 11, 17, 21, 35, tzinfo=dt.UTC)
SPEC_SESSIONS = SessionCalculator(SessionsConfig())


def test_entry_order_withdrawn_before_the_rollover_block(base_signal: Signal) -> None:
    signal = make_signal(base_signal, time=BEFORE_ROLLOVER)
    rows = [QUIET, NO_FILL, NO_FILL, FILL, TO_TP1]  # the fill bar opens at 21:45, in the block
    result = simulate(rows, [signal], signal_time=BEFORE_ROLLOVER, sessions=SPEC_SESSIONS)
    assert result.trades == []
    (outcome,) = result.outcomes
    assert (outcome.outcome, outcome.reason) == (
        Outcome.WITHDRAWN,
        "withdrawn before the rollover block",
    )
    # Without the clock rule the same order would have filled inside the block.
    assert simulate(rows, [signal], signal_time=BEFORE_ROLLOVER).trades


def test_an_entry_filled_before_the_block_is_kept(base_signal: Signal) -> None:
    signal = make_signal(base_signal, time=BEFORE_ROLLOVER)
    result = simulate(
        [QUIET, FILL, TO_1R, TO_TP1, TO_TP2],
        [signal],
        signal_time=BEFORE_ROLLOVER,
        sessions=SPEC_SESSIONS,
    )
    assert only_trade(result).fill_time == BEFORE_ROLLOVER  # managed through the block


def test_entry_order_withdrawn_before_a_news_blackout(base_signal: Signal) -> None:
    cpi = NewsEvent(SIGNAL_TIME + dt.timedelta(minutes=40), "US CPI")  # blackout from 08:05
    result = simulate(
        [QUIET, NO_FILL, NO_FILL, FILL, TO_TP1],
        [make_signal(base_signal)],
        events=[cpi],
        sessions=SPEC_SESSIONS,
    )
    assert result.trades == []
    assert [(o.outcome, o.reason) for o in result.outcomes] == [
        (Outcome.WITHDRAWN, "withdrawn before the news blackout for US CPI")
    ]


def test_market_entry_fills_at_the_next_open_with_spread_and_slippage(
    base_signal: Signal,
) -> None:
    signal = make_signal(base_signal, entry=2030.0, stop=2028.0, tp1=2034.0, entry_mode="market")
    opens_higher: Row = (2030.05, 2030.5, 2029.9, 2030.3)
    result = simulate([QUIET, opens_higher], [signal], backtest=BacktestConfig())
    trade = only_trade(result)
    assert trade.fill_price == pytest.approx(2030.05 + 0.20 + 0.10)  # ask plus slippage
    assert trade.risk_amount == pytest.approx((2030.35 - 2028.0) * trade.lots * 100)


def test_one_position_at_a_time(base_signal: Signal) -> None:
    first = make_signal(base_signal)
    second = make_signal(base_signal, time=SIGNAL_TIME + 2 * BAR, number=2)
    result = simulate([QUIET, FILL, QUIET, QUIET, TO_TP1, TO_TP2], [first, second])
    assert len(result.trades) == 1
    blocked = [o for o in result.outcomes if o.outcome is Outcome.RISK_BLOCKED]
    assert len(blocked) == 1 and "already open" in blocked[0].reason


def test_signals_the_selector_passes_over(base_signal: Signal) -> None:
    result = simulate([QUIET, FILL], [make_signal(base_signal)], select=lambda _r: "score too low")
    assert result.trades == []
    assert (result.outcomes[0].outcome, result.outcomes[0].reason) == (
        Outcome.NOT_SELECTED,
        "score too low",
    )


def test_account_too_small_for_the_minimum_lot(base_signal: Signal) -> None:
    tiny = FREE.model_copy(update={"initial_balance": 100.0})
    result = simulate([QUIET, FILL], [make_signal(base_signal)], backtest=tiny)
    assert result.trades == [] and result.outcomes[0].outcome is Outcome.NO_SIZE


def test_signals_outside_the_window_are_ignored(base_signal: Signal) -> None:
    late = make_signal(base_signal, time=SIGNAL_TIME + dt.timedelta(days=2))
    result = simulate([QUIET, FILL], [late])
    assert result.trades == [] and result.outcomes == []
    assert result.final_equity == FREE.initial_balance


def test_order_still_waiting_when_the_data_ends(base_signal: Signal) -> None:
    result = simulate([QUIET, NO_FILL], [make_signal(base_signal)])
    assert result.outcomes[0].outcome is Outcome.EXPIRED
    assert "data ended" in result.outcomes[0].reason


# --------------------------------------------------------------------------- #
# Price series helpers
# --------------------------------------------------------------------------- #


def test_one_minute_bars_replace_the_five_minute_bar_they_cover() -> None:
    coarse_frame = bars([QUIET, QUIET, QUIET], SIGNAL_TIME)
    fine_frame = bars([QUIET] * 5, SIGNAL_TIME + BAR, minutes=1)
    coarse = PriceSeries.from_candles(coarse_frame, Timeframe.M5, point=0.01)
    fine = PriceSeries.from_candles(fine_frame, Timeframe.M1, point=0.01)
    combined = combine_series(coarse, fine)
    opens = pd.to_datetime(combined.open_ns, utc=True)
    assert len(combined) == 7  # 5-minute, five 1-minute, 5-minute
    assert opens[0] == SIGNAL_TIME and opens[-1] == SIGNAL_TIME + 2 * BAR
    assert np.all(np.diff(combined.open_ns) > 0)
    assert (
        combine_series(
            coarse, PriceSeries.from_candles(bars([], SIGNAL_TIME), Timeframe.M1, point=0.01)
        )
        is coarse
    )


def test_fixed_spread_and_trail_levels() -> None:
    frame = bars([QUIET, (2030.0, 2030.2, 2029.0, 2029.5), QUIET], SIGNAL_TIME)
    series = PriceSeries.from_candles(frame, Timeframe.M5, point=0.01, spread_points=35.0)
    assert series.spread.tolist() == pytest.approx([0.35, 0.35, 0.35])
    levels = TrailLevels.from_candles(frame, Timeframe.M5, 1)
    before = int(pd.Timestamp(SIGNAL_TIME).as_unit("ns").value)
    assert all(np.isnan(value) for value in levels.at(before))
    low, _high = levels.at(int(levels.close_ns[-1]))
    assert low == 2029.0  # the middle bar's low, confirmed by the bar after it


# --------------------------------------------------------------------------- #
# Spec 7b: three targets, and breakeven before a release (spec 5.6)
# --------------------------------------------------------------------------- #


def test_three_way_split(base_signal: Signal) -> None:
    rows = [QUIET, FILL, TO_1R, TO_TP1, (2033.0, 2035.2, 2032.9, 2035.0)]
    signal = make_signal(base_signal, tp2=2034.0)  # TP3 at 3R = 2035.00, beyond TP2
    trade = only_trade(simulate(rows, [signal], risk=RiskConfig()))
    assert trade.take_profit_3 == pytest.approx(2035.0)
    assert [(leg.lots, leg.reason) for leg in trade.legs] == [
        (0.12, ExitReason.TP1),
        (0.07, ExitReason.TP2),
        (0.06, ExitReason.TP3),
    ]
    assert trade.net_pnl == pytest.approx(0.12 * 400 + 0.07 * 500 + 0.06 * 600)


def test_tp3_inside_tp2_folds_into_tp2(base_signal: Signal) -> None:
    rows = [QUIET, FILL, TO_1R, TO_TP1, TO_TP2]
    trade = only_trade(simulate(rows, [make_signal(base_signal)], risk=RiskConfig()))
    assert trade.take_profit_3 is None  # 3R (2035) is not beyond TP2 (2037)
    assert [(leg.lots, leg.reason) for leg in trade.legs] == [
        (0.12, ExitReason.TP1),
        (0.13, ExitReason.TP2),
    ]


def test_breakeven_before_news(base_signal: Signal) -> None:
    dip: Row = (2031.0, 2031.2, 2028.5, 2029.0)  # back under the entry, above the stop
    rows = [QUIET, FILL, TO_1R, dip]
    late_breakeven = RiskConfig(breakeven_at_r=2.0, tp3_r=0)
    blackout_starts = SIGNAL_TIME + dt.timedelta(minutes=8)  # inside the +1R bar
    cpi = NewsEvent(blackout_starts + dt.timedelta(minutes=30), "US CPI")
    with_news = only_trade(
        simulate(rows, [make_signal(base_signal)], risk=late_breakeven, events=[cpi])
    )
    assert with_news.exit_reason is ExitReason.BREAKEVEN  # +1R at the blackout: stop to entry
    without = simulate(rows, [make_signal(base_signal)], risk=late_breakeven)
    assert only_trade(without).exit_reason is ExitReason.END_OF_DATA  # the stop was at 2027

"""Backtest statistics and the walk-forward machinery."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence

import pytest

from goldsweep.analysis.bias import Bias
from goldsweep.analysis.pressure import BALANCED, BUYERS, PressureReading
from goldsweep.analysis.signals import Signal
from goldsweep.analysis.structure import Direction
from goldsweep.backtest.metrics import (
    breakdown,
    by_direction,
    by_pressure,
    by_session,
    by_weekday,
    drawdown,
    equity_points,
    summarize,
)
from goldsweep.backtest.simulator import ExitReason, Leg, SignalRecord, SimulationResult, Trade
from goldsweep.backtest.walkforward import (
    Combo,
    Fold,
    FoldResult,
    add_months,
    grid_combos,
    make_folds,
    make_selector,
    overfitting_warnings,
    walk_forward,
)
from goldsweep.core.settings import WalkForwardConfig, WalkForwardGrid
from goldsweep.execution.book import TradePlan
from tests.helpers import utc
from tests.test_simulator import make_signal, record

MONDAY = utc(2026, 11, 16, 8, 0)
RISK = 50.0


def trade(
    base: Signal,
    r: float,
    *,
    when: dt.datetime = MONDAY,
    session: str = "london",
    direction: Direction = Direction.LONG,
    pressure: PressureReading | None = None,
    number: int = 1,
) -> Trade:
    """A closed one-leg trade that made ``r`` times the 50.00 it risked."""
    prices = (
        {}
        if direction is Direction.LONG
        else {"entry": 2031.0, "stop": 2033.0, "tp1": 2027.0, "tp2": 2023.0}
    )
    signal = dataclasses.replace(
        make_signal(base, time=when, direction=direction, number=number, **prices),
        session=session,
    )
    leg = Leg(
        lots=0.25,
        exit_price=2033.0 if r > 0 else 2027.0,
        exit_time=when + dt.timedelta(minutes=45),
        reason=ExitReason.TP1 if r > 0 else ExitReason.STOP,
        pnl=r * RISK,
        commission=0.0,
    )
    return Trade(
        TradePlan.from_signal(signal),
        SignalRecord(signal, 20.0, 20.0, pressure),
        fill_time=when + dt.timedelta(minutes=5),
        fill_price=2029.0,
        stop_loss=2027.0,
        take_profit_1=2033.0,
        take_profit_2=None,
        lots=0.25,
        risk_amount=RISK,
        legs=(leg,),
    )


def series(base: Signal, results: Sequence[float]) -> list[Trade]:
    return [
        trade(base, r, when=MONDAY + dt.timedelta(hours=2 * n), number=n)
        for n, r in enumerate(results)
    ]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_headline_statistics(base_signal: Signal) -> None:
    metrics = summarize(series(base_signal, [2, -1, -1, 3, -1]), 10_000.0)
    assert (metrics.trades, metrics.wins, metrics.losses) == (5, 2, 3)
    assert metrics.win_rate == pytest.approx(0.4)
    assert metrics.profit_factor == pytest.approx(250.0 / 150.0)
    assert metrics.expectancy_r == pytest.approx(0.4)
    assert metrics.total_r == pytest.approx(2.0)
    assert (metrics.average_win_r, metrics.average_loss_r) == (pytest.approx(2.5), -1.0)
    assert metrics.net_profit == pytest.approx(100.0)
    assert metrics.return_pct == pytest.approx(1.0)
    # Equity 10,000 -> 10,100 -> 10,050 -> 10,000: 100 below the 10,100 peak.
    assert metrics.max_drawdown == pytest.approx(100.0)
    assert metrics.max_drawdown_pct == pytest.approx(100.0 / 10_100.0 * 100)
    assert metrics.longest_losing_streak == 2
    assert metrics.average_hold_minutes == pytest.approx(40.0)
    assert metrics.average_planned_rr == pytest.approx(2.0)
    assert set(metrics.as_dict()) >= {"win_rate", "profit_factor", "expectancy_r"}


def test_statistics_without_trades_or_losses(base_signal: Signal) -> None:
    empty = summarize([], 10_000.0)
    assert (empty.trades, empty.win_rate, empty.profit_factor, empty.expectancy_r) == (
        0,
        None,
        None,
        None,
    )
    assert empty.max_drawdown == 0.0
    only_wins = summarize(series(base_signal, [1.0, 2.0]), 10_000.0)
    assert only_wins.profit_factor is None  # no losses: undefined, not infinite


def test_equity_points_and_drawdown(base_signal: Signal) -> None:
    trades = series(base_signal, [1, -2])
    points = equity_points(trades, 1_000.0, MONDAY)
    assert [value for _t, value in points] == [1_000.0, 1_050.0, 950.0]
    assert drawdown([]) == (0.0, 0.0)
    assert drawdown([0.0, -5.0]) == (5.0, 0.0)  # no positive peak to measure against


def test_breakdowns(base_signal: Signal) -> None:
    buyers = PressureReading(MONDAY, 70.0, 30.0, 0.4, BUYERS, {}, 24)
    balanced = PressureReading(MONDAY, 52.0, 48.0, 0.04, BALANCED, {}, 24)
    trades = [
        trade(base_signal, 2.0, session="newyork", pressure=buyers),
        trade(base_signal, -1.0, direction=Direction.SHORT, pressure=buyers, number=2),
        trade(base_signal, 1.0, pressure=balanced, number=3),
        trade(base_signal, -1.0, when=MONDAY + dt.timedelta(days=1), number=4),
    ]
    sessions = breakdown(trades, by_session, 10_000.0, order=["london", "newyork"])
    assert list(sessions) == ["london", "newyork"]
    assert sessions["london"].trades == 3 and sessions["newyork"].total_r == 2.0
    weekdays = breakdown(trades, by_weekday, 10_000.0)
    assert {name: m.trades for name, m in weekdays.items()} == {"Monday": 3, "Tuesday": 1}
    assert set(breakdown(trades, by_direction, 10_000.0)) == {"LONG", "SHORT"}
    meter = breakdown(trades, by_pressure, 10_000.0)
    assert {name: m.trades for name, m in meter.items()} == {
        "meter agreed": 1,
        "meter disagreed": 1,
        "meter balanced": 1,
        "no reading": 1,
    }
    unordered = breakdown(trades, by_session, 10_000.0, order=["overlap"])
    assert list(unordered) == ["newyork", "london"]


# --------------------------------------------------------------------------- #
# Walk-forward pieces
# --------------------------------------------------------------------------- #


def test_add_months_clamps_to_the_month_end() -> None:
    assert add_months(utc(2024, 1, 31), 1) == utc(2024, 2, 29)
    assert add_months(utc(2024, 11, 15), 3) == utc(2025, 2, 15)
    assert add_months(utc(2024, 3, 1), -2) == utc(2024, 1, 1)


def test_folds_roll_by_the_test_length() -> None:
    folds = make_folds(utc(2024, 1, 1), utc(2025, 1, 1), 6, 2)
    assert [(f.train_start.month, f.test_start.month, f.test_end) for f in folds] == [
        (1, 7, utc(2024, 9, 1)),
        (3, 9, utc(2024, 11, 1)),
        (5, 11, utc(2025, 1, 1)),
    ]
    short = make_folds(utc(2024, 1, 1), utc(2024, 7, 20), 6, 2)
    assert len(short) == 1 and short[0].test_end == utc(2024, 7, 20)  # cut at the end
    assert make_folds(utc(2024, 1, 1), utc(2024, 6, 1), 6, 2) == []


def test_selector_needs_the_score_and_the_rr(base_signal: Signal) -> None:
    select = make_selector(80.0, 2.5, neutral_extra=5.0)
    signal = dataclasses.replace(make_signal(base_signal), score=81.1)  # NEUTRAL, R:R 2.0
    assert select(record(signal)) == "score 81.1 is below 85"  # 80 + 5 for NEUTRAL
    long_bias = dataclasses.replace(signal, bias=dataclasses.replace(signal.bias, bias=Bias.LONG))
    assert select(record(long_bias)) == "R:R 2.00 is below 2.5"
    assert make_selector(80.0, 2.0, 5.0)(record(long_bias)) is None


def test_grid_combinations() -> None:
    combos = grid_combos(
        WalkForwardGrid(auto_trade_threshold=(85, 65), min_rr=(2.0,), entry_expiry_bars=(12, 6))
    )
    assert combos == [  # sorted by the config model
        Combo(65.0, 2.0, 6),
        Combo(65.0, 2.0, 12),
        Combo(85.0, 2.0, 6),
        Combo(85.0, 2.0, 12),
    ]
    assert combos[0].describe() == "score >= 65, R:R >= 2, entries wait 6 bars"
    assert Combo(75.0, 2.0).describe() == "score >= 75, R:R >= 2"  # lifetime left alone


class StubSimulator:
    """Stands in for the price simulation: each signal's result is known in advance."""

    def __init__(self, outcomes: dict[str, Trade]) -> None:
        self._outcomes = outcomes

    def run(self, records, *, select, start, end):  # noqa: ANN001, ANN201
        chosen = [
            self._outcomes[r.signal.id]
            for r in records
            if start <= r.time < end and select(r) is None
        ]
        return SimulationResult(trades=chosen, final_equity=10_000.0)


def test_walk_forward_picks_the_threshold_that_works(base_signal: Signal) -> None:
    # One signal a week through 2024: scores 90 always win 2R, scores 70 always lose 1R.
    records, outcomes = [], {}
    for week in range(52):
        when = utc(2024, 1, 2, 8) + dt.timedelta(weeks=week)
        score = 90.0 if week % 2 else 70.0
        signal = dataclasses.replace(
            make_signal(base_signal, time=when, number=week),
            score=score,
            bias=dataclasses.replace(base_signal.bias, bias=Bias.LONG),
        )
        records.append(record(signal))
        outcomes[signal.id] = trade(
            base_signal, 2.0 if score > 80 else -1.0, when=when, number=week
        )
    config = WalkForwardConfig(
        min_train_trades=5,
        grid=WalkForwardGrid(auto_trade_threshold=(65, 85), min_rr=(2.0,), entry_expiry_bars=(6,)),
    )
    result = walk_forward(
        StubSimulator(outcomes),  # type: ignore[arg-type]
        records,
        config=config,
        start=utc(2024, 1, 1),
        end=utc(2025, 1, 1),
        defaults=Combo(75.0, 2.0),
        neutral_extra=5.0,
        initial_balance=10_000.0,
    )
    assert len(result.folds) == 3
    assert {fold.chosen for fold in result.folds} == {Combo(85.0, 2.0, 6)}
    assert all(fold.eligible == 2 for fold in result.folds)
    assert result.oos_metrics.trades == len(result.out_of_sample.trades)
    assert result.oos_metrics.expectancy_r == pytest.approx(2.0)  # only the winners traded
    assert result.in_sample_expectancy_r == pytest.approx(2.0)
    assert any("too few to tell skill from luck" in w for w in result.warnings)


class LifetimeStub(StubSimulator):
    """Also notes the entry lifetimes the walk-forward asks the simulator to use."""

    def __init__(self, outcomes: dict[str, Trade]) -> None:
        super().__init__(outcomes)
        self.lifetimes: set[dt.timedelta | None] = set()

    def run(self, records, *, select, start, end, lifetime=None):  # noqa: ANN001, ANN201
        if lifetime is not None:
            self.lifetimes.update(lifetime(r) for r in records if start <= r.time < end)
        return super().run(records, select=select, start=start, end=end)


def test_walk_forward_tries_each_entry_lifetime(base_signal: Signal) -> None:
    records, outcomes = [], {}
    for week in range(52):
        when = utc(2024, 1, 2, 8) + dt.timedelta(weeks=week)
        signal = dataclasses.replace(
            make_signal(base_signal, time=when, number=week),
            score=90.0,
            bias=dataclasses.replace(base_signal.bias, bias=Bias.LONG),
        )
        records.append(record(signal))
        outcomes[signal.id] = trade(base_signal, 2.0, when=when, number=week)
    stub = LifetimeStub(outcomes)
    result = walk_forward(
        stub,  # type: ignore[arg-type]
        records,
        config=WalkForwardConfig(
            min_train_trades=5,
            grid=WalkForwardGrid(
                auto_trade_threshold=(65,), min_rr=(2.0,), entry_expiry_bars=(6, 24)
            ),
        ),
        start=utc(2024, 1, 1),
        end=utc(2025, 1, 1),
        defaults=Combo(65.0, 2.0, 6),
        neutral_extra=5.0,
        initial_balance=10_000.0,
        bar=dt.timedelta(minutes=5),
    )
    assert stub.lifetimes == {dt.timedelta(minutes=30), dt.timedelta(hours=2)}
    assert {fold.chosen.entry_expiry_bars for fold in result.folds} <= {6, 24}
    assert "entries wait" in result.folds[0].chosen.describe()


def test_walk_forward_falls_back_to_defaults_without_enough_trades(base_signal: Signal) -> None:
    when = utc(2024, 2, 1, 8)
    signal = make_signal(base_signal, time=when)
    outcomes = {signal.id: trade(base_signal, 1.0, when=when)}
    result = walk_forward(
        StubSimulator(outcomes),  # type: ignore[arg-type]
        [record(signal)],
        config=WalkForwardConfig(),
        start=utc(2024, 1, 1),
        end=utc(2024, 9, 1),
        defaults=Combo(75.0, 2.0),
        neutral_extra=5.0,
        initial_balance=10_000.0,
    )
    assert result.folds and all(fold.used_defaults for fold in result.folds)
    assert any("the configured defaults were traded" in w for w in result.warnings)


def test_walk_forward_on_too_short_a_period(base_signal: Signal) -> None:
    result = walk_forward(
        StubSimulator({}),  # type: ignore[arg-type]
        [],
        config=WalkForwardConfig(),
        start=utc(2024, 1, 1),
        end=utc(2024, 3, 1),
        defaults=Combo(75.0, 2.0),
        neutral_extra=5.0,
        initial_balance=10_000.0,
    )
    assert result.folds == [] and result.oos_metrics.trades == 0
    assert "no walk-forward test was possible" in result.warnings[0]


# --------------------------------------------------------------------------- #
# Overfitting warnings
# --------------------------------------------------------------------------- #


def fold_result(base: Signal, chosen: Combo, train_r: Sequence[float]) -> FoldResult:
    window = Fold(utc(2024, 1, 1), utc(2024, 7, 1), utc(2024, 7, 1), utc(2024, 9, 1))
    train = summarize(series(base, train_r), 10_000.0)
    return FoldResult(window, chosen, False, train, summarize([], 10_000.0), 1)


def test_edge_that_vanishes_out_of_sample_is_called_overfitted(base_signal: Signal) -> None:
    folds = [fold_result(base_signal, Combo(85, 2), [2.0, 1.0])]
    oos = summarize(series(base_signal, [-1.0] * 40), 10_000.0)
    (warning,) = overfitting_warnings(folds, oos, 1.5, WalkForwardConfig())
    assert "did not survive" in warning and "+1.50 R" in warning and "-1.00 R" in warning


def test_low_walk_forward_efficiency(base_signal: Signal) -> None:
    folds = [fold_result(base_signal, Combo(85, 2), [2.0])]
    oos = summarize(series(base_signal, [2.0, -1.0, -1.0, -1.0] * 10), 10_000.0)  # -0.25 R
    weak = summarize(series(base_signal, [2.0, -1.0, -1.0] * 14), 10_000.0)  # 0 R
    better = summarize(series(base_signal, [2.0, -1.0] * 20), 10_000.0)  # +0.5 R
    assert "did not survive" in overfitting_warnings(folds, oos, 2.0, WalkForwardConfig())[0]
    assert "did not survive" in overfitting_warnings(folds, weak, 2.0, WalkForwardConfig())[0]
    (warning,) = overfitting_warnings(folds, better, 2.0, WalkForwardConfig())
    assert "keeps only 25%" in warning
    assert overfitting_warnings(folds, better, 0.8, WalkForwardConfig()) == []


def test_unstable_choices_are_flagged(base_signal: Signal) -> None:
    choices = [Combo(65, 2), Combo(85, 2), Combo(65, 2), Combo(75, 3)]
    folds = [fold_result(base_signal, combo, [1.0]) for combo in choices]
    oos = summarize(series(base_signal, [1.0, -0.5] * 20), 10_000.0)
    (warning,) = overfitting_warnings(folds, oos, 0.4, WalkForwardConfig())
    assert "changed in 3 of 3" in warning

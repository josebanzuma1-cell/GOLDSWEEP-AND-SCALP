"""Walk-forward testing (spec 9.3): optimise on 6 months, test on the next 2, roll on.

For each window the parameter sets of the grid are tried on the *training*
months only, and the best (highest expectancy in R, with enough trades) is then
traded on the *test* months that follow, which it has never seen. Joining the
test months gives an out-of-sample result: the honest estimate of how the
choosing process performs on new data.

Only parameters that do not change the engine's signals are optimised (the
auto-trade score threshold, a minimum R:R, and how many bars an entry order
waits), so the signal engine runs once and each trial is a fast re-simulation. A
large gap between in-sample and out-of-sample results is the classic sign of
overfitting, and is reported as a warning.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import product
from typing import TYPE_CHECKING

from goldsweep.analysis.bias import Bias
from goldsweep.backtest.metrics import Metrics, summarize
from goldsweep.backtest.simulator import SignalRecord, SimulationResult, Simulator
from goldsweep.core.clock import ensure_utc

if TYPE_CHECKING:
    from goldsweep.core.settings import WalkForwardConfig, WalkForwardGrid

MIN_OUT_OF_SAMPLE_TRADES = 30
_EPSILON = 1e-9

Selector = Callable[[SignalRecord], str | None]


@dataclass(frozen=True, slots=True)
class Combo:
    """One parameter set of the grid."""

    auto_trade_threshold: float
    min_rr: float
    #: Bars an unfilled entry waits; ``None`` leaves the simulator's own setting.
    entry_expiry_bars: int | None = None

    def describe(self) -> str:
        text = f"score >= {self.auto_trade_threshold:g}, R:R >= {self.min_rr:g}"
        if self.entry_expiry_bars is not None:
            text += f", entries wait {self.entry_expiry_bars} bars"
        return text


@dataclass(frozen=True, slots=True)
class Fold:
    train_start: dt.datetime
    train_end: dt.datetime
    test_start: dt.datetime
    test_end: dt.datetime


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: Fold
    chosen: Combo
    used_defaults: bool  # no grid entry had enough training trades
    train: Metrics
    test: Metrics
    eligible: int  # grid entries with enough training trades


@dataclass
class WalkForwardResult:
    folds: list[FoldResult]
    out_of_sample: SimulationResult
    oos_metrics: Metrics
    in_sample_expectancy_r: float | None
    warnings: list[str]
    grid: list[Combo]


def grid_combos(grid: WalkForwardGrid) -> list[Combo]:
    return [
        Combo(t, rr, bars)
        for t, rr, bars in product(grid.auto_trade_threshold, grid.min_rr, grid.entry_expiry_bars)
    ]


def _run(
    simulator: Simulator,
    records: Sequence[SignalRecord],
    combo: Combo,
    neutral_extra: float,
    start: dt.datetime,
    end: dt.datetime,
    bar: dt.timedelta | None,
) -> SimulationResult:
    """One trial: ``combo``'s selection, and its entry lifetime when the bar length is known."""
    select = make_selector(combo.auto_trade_threshold, combo.min_rr, neutral_extra)
    if bar is None or combo.entry_expiry_bars is None:
        return simulator.run(records, select=select, start=start, end=end)
    span = bar * combo.entry_expiry_bars
    return simulator.run(records, select=select, start=start, end=end, lifetime=lambda _r: span)


def make_selector(threshold: float, min_rr: float, neutral_extra: float) -> Selector:
    """Trade a signal when its score and R:R clear these levels.

    A NEUTRAL bias raises the score needed by ``neutral_extra``, as in the engine.
    """

    def select(record: SignalRecord) -> str | None:
        signal = record.signal
        extra = neutral_extra if signal.bias.bias is Bias.NEUTRAL else 0.0
        needed = min(100.0, threshold + extra)
        if signal.score < needed - _EPSILON:
            return f"score {signal.score:g} is below {needed:g}"
        if signal.risk_reward_1 < min_rr - _EPSILON:
            return f"R:R {signal.risk_reward_1:.2f} is below {min_rr:g}"
        return None

    return select


def add_months(moment: dt.datetime, months: int) -> dt.datetime:
    """``moment`` moved by whole calendar months (the day is clamped to the month's end)."""
    total = moment.month - 1 + months
    year, month = moment.year + total // 12, total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def make_folds(
    start: dt.datetime, end: dt.datetime, train_months: int, test_months: int
) -> list[Fold]:
    """Rolling windows; the last test window is cut short at ``end`` if needed."""
    start, end = ensure_utc(start), ensure_utc(end)
    folds: list[Fold] = []
    offset = 0
    while True:
        train_start = add_months(start, offset)
        train_end = add_months(train_start, train_months)
        if train_end >= end:
            break
        test_end = min(add_months(train_end, test_months), end)
        folds.append(Fold(train_start, train_end, train_end, test_end))
        if test_end >= end:
            break
        offset += test_months
    return folds


def walk_forward(
    simulator: Simulator,
    records: Sequence[SignalRecord],
    *,
    config: WalkForwardConfig,
    start: dt.datetime,
    end: dt.datetime,
    defaults: Combo,
    neutral_extra: float,
    initial_balance: float,
    bar: dt.timedelta | None = None,
) -> WalkForwardResult:
    """Run the whole walk-forward test.

    ``bar`` is the trigger timeframe's length; with it, the grid's entry lifetimes
    are tried too. Without it every trial keeps the simulator's own lifetime.
    """
    grid = grid_combos(config.grid)
    folds = make_folds(start, end, config.train_months, config.test_months)
    results: list[FoldResult] = []
    for fold in folds:
        trials: list[tuple[Combo, Metrics]] = []
        for combo in grid:
            run = _run(
                simulator, records, combo, neutral_extra, fold.train_start, fold.train_end, bar
            )
            trials.append((combo, summarize(run.trades, initial_balance)))
        eligible = [
            (combo, metrics)
            for combo, metrics in trials
            if metrics.trades >= config.min_train_trades and metrics.expectancy_r is not None
        ]
        if eligible:
            chosen, train = max(
                eligible,
                key=lambda item: (item[1].expectancy_r, item[1].total_r, -grid.index(item[0])),
            )
            used_defaults = False
        else:
            chosen, used_defaults = defaults, True
            train = next(
                (metrics for combo, metrics in trials if combo == defaults),
                summarize(
                    _run(
                        simulator,
                        records,
                        defaults,
                        neutral_extra,
                        fold.train_start,
                        fold.train_end,
                        bar,
                    ).trades,
                    initial_balance,
                ),
            )
        test_run = _run(
            simulator, records, chosen, neutral_extra, fold.test_start, fold.test_end, bar
        )
        results.append(
            FoldResult(
                fold=fold,
                chosen=chosen,
                used_defaults=used_defaults,
                train=train,
                test=summarize(test_run.trades, initial_balance),
                eligible=len(eligible),
            )
        )

    if results:
        selectors = [
            (
                result.fold,
                make_selector(
                    result.chosen.auto_trade_threshold, result.chosen.min_rr, neutral_extra
                ),
            )
            for result in results
        ]

        def by_fold(record: SignalRecord) -> str | None:
            for fold, select in selectors:
                if fold.test_start <= record.time < fold.test_end:
                    return select(record)
            return "outside the test windows"

        oos_start, oos_end = results[0].fold.test_start, results[-1].fold.test_end
        if bar is None or any(r.chosen.entry_expiry_bars is None for r in results):
            oos = simulator.run(records, select=by_fold, start=oos_start, end=oos_end)
        else:
            step = bar

            def lifetime(record: SignalRecord) -> dt.timedelta | None:
                for result in results:
                    fold = result.fold
                    if fold.test_start <= record.time < fold.test_end:
                        return step * (result.chosen.entry_expiry_bars or 0)
                return None

            oos = simulator.run(
                records, select=by_fold, start=oos_start, end=oos_end, lifetime=lifetime
            )
    else:
        oos = SimulationResult(final_equity=initial_balance)
    oos_metrics = summarize(oos.trades, initial_balance)
    in_sample = [r.train.expectancy_r for r in results if r.train.expectancy_r is not None]
    in_sample_expectancy = sum(in_sample) / len(in_sample) if in_sample else None
    warnings = overfitting_warnings(results, oos_metrics, in_sample_expectancy, config)
    return WalkForwardResult(results, oos, oos_metrics, in_sample_expectancy, warnings, grid)


def overfitting_warnings(
    folds: Sequence[FoldResult],
    oos: Metrics,
    in_sample_expectancy: float | None,
    config: WalkForwardConfig,
) -> list[str]:
    """Plain-language warnings about how far the optimised results can be trusted."""
    warnings: list[str] = []
    if not folds:
        warnings.append(
            f"The period is shorter than {config.train_months} training months plus a test "
            "window: no walk-forward test was possible, so nothing here is out-of-sample."
        )
        return warnings
    if oos.trades < MIN_OUT_OF_SAMPLE_TRADES:
        warnings.append(
            f"Only {oos.trades} out-of-sample trades: far too few to tell skill from luck. "
            f"Treat every figure as unproven until there are at least {MIN_OUT_OF_SAMPLE_TRADES}."
        )
    if in_sample_expectancy is not None and in_sample_expectancy > 0:
        oos_expectancy = oos.expectancy_r
        if oos_expectancy is None or oos_expectancy <= 0:
            warnings.append(
                f"In-sample the chosen settings earned {in_sample_expectancy:+.2f} R per trade, "
                f"out-of-sample {oos_expectancy or 0.0:+.2f} R. The edge did not survive outside "
                "the data it was tuned on: treat the optimised settings as overfitted."
            )
        elif oos_expectancy < 0.5 * in_sample_expectancy:
            warnings.append(
                f"Out-of-sample keeps only {oos_expectancy / in_sample_expectancy:.0%} of the "
                f"in-sample edge ({oos_expectancy:+.2f} R against {in_sample_expectancy:+.2f} R "
                "per trade). Below 50% usually means the settings are partly fitted to noise."
            )
    if len(folds) >= 3:
        changes = sum(1 for a, b in zip(folds, folds[1:], strict=False) if a.chosen != b.chosen)
        if changes / (len(folds) - 1) > 0.5:
            warnings.append(
                f"The best settings changed in {changes} of {len(folds) - 1} window changes. "
                "An optimum that keeps moving is usually noise being fitted, not a real edge."
            )
    defaulted = sum(1 for fold in folds if fold.used_defaults)
    if defaulted:
        warnings.append(
            f"{defaulted} of {len(folds)} training windows had fewer than "
            f"{config.min_train_trades} trades for every setting; the configured defaults "
            "were traded there instead of an optimised choice."
        )
    return warnings

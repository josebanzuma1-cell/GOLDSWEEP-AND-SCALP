"""Gold-silver SMT divergence (Phase 9 stage 3, D85).

Gold and silver usually move together. When gold sweeps a low (it makes a lower
low than the bars before) while silver, over the same bars, holds above its own
low of that period, the two "disagree": the sweep in gold looks like a raid on
stops rather than real selling. That disagreement is the smart-money-technique
(SMT) divergence. Shorts mirror it with highs.

It is recorded as the ``smt`` tag on a signal and reported by the backtester; it
does not change the score. Only closed bars are read, and both frames are matched
by bar open time, so a missing silver bar means no verdict, never a guess.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING

import pandas as pd

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ensure_utc

if TYPE_CHECKING:
    from goldsweep.analysis.signals import Signal

SMT_TAG = "smt"


def _window(frame: pd.DataFrame, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    """Bars opening in ``[start, end]``."""
    index = frame.index
    first = index.searchsorted(pd.Timestamp(ensure_utc(start)))
    last = index.searchsorted(pd.Timestamp(ensure_utc(end)), side="right")
    return frame.iloc[first:last]


def smt_divergence(
    direction: Direction,
    gold: pd.DataFrame,
    silver: pd.DataFrame,
    *,
    sweep_start: dt.datetime,
    sweep_end: dt.datetime,
    lookback_bars: int,
    bar: dt.timedelta,
) -> bool | None:
    """Did silver hold while gold swept? ``None`` when either has too few bars to say.

    Args:
        direction: the trade the sweep points to (LONG after a low was swept).
        gold, silver: closed bars of the same timeframe.
        sweep_start, sweep_end: open times of the first and last bar of the sweep
            (from the bar that pierced the level to the bar that closed back inside).
        lookback_bars: bars before the sweep that set each metal's level.
        bar: the timeframe's length.
    """
    reference_start = ensure_utc(sweep_start) - bar * lookback_bars
    reference_end = ensure_utc(sweep_start) - bar
    gold_ref = _window(gold, reference_start, reference_end)
    silver_ref = _window(silver, reference_start, reference_end)
    gold_now = _window(gold, sweep_start, sweep_end)
    silver_now = _window(silver, sweep_start, sweep_end)
    if (
        min(len(gold_ref), len(silver_ref)) < lookback_bars // 2
        or gold_now.empty
        or silver_now.empty
    ):
        return None
    if direction is Direction.LONG:
        gold_broke = float(gold_now["low"].min()) < float(gold_ref["low"].min())
        silver_held = float(silver_now["low"].min()) > float(silver_ref["low"].min())
    else:
        gold_broke = float(gold_now["high"].max()) > float(gold_ref["high"].max())
        silver_held = float(silver_now["high"].max()) < float(silver_ref["high"].max())
    return gold_broke and silver_held


def with_smt(
    signal: Signal,
    gold: pd.DataFrame,
    silver: pd.DataFrame,
    *,
    lookback_bars: int,
    bar: dt.timedelta,
) -> Signal:
    """``signal`` with the ``smt`` tag added when the divergence was there."""
    found = smt_divergence(
        signal.direction,
        gold,
        silver,
        sweep_start=signal.sweep.pierce_time,
        sweep_end=signal.sweep.reclaim_time,
        lookback_bars=lookback_bars,
        bar=bar,
    )
    if not found or SMT_TAG in signal.tags:
        return signal
    return dataclasses.replace(signal, tags=(*signal.tags, SMT_TAG))

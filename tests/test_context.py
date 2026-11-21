"""Trend, structure and regime context tags (Phase 10, D86), and the breakout model."""

from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np
import pandas as pd

from goldsweep.analysis.context import ContextFrames, context_tags, with_context
from goldsweep.analysis.grades import meets
from goldsweep.analysis.signals import MODELS, Signal, SignalEngine
from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import DayBoundaryConfig, PlaybookRule, Settings
from tests.helpers import utc

START = utc(2026, 8, 3)  # a Monday


def rising_hours(hours: int = 1500, *, spike: int = 0) -> pd.DataFrame:
    """H1 bars rising 0.1 an hour with a 5-day wave of +-20, so the daily and
    four-hour charts make higher highs and higher lows; ``spike`` very wide bars at
    the end raise the volatility."""
    t = np.arange(hours + 1, dtype="float64")
    mid = 2000.0 + 0.1 * t + 20.0 * np.sin(2 * np.pi * t / 120.0)
    opens, closes = mid[:-1], mid[1:]
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    if spike:
        highs[-spike:] += 15.0
        lows[-spike:] -= 15.0
    index = pd.date_range(START, periods=hours, freq="1h", name="time")
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "tick_volume": 100,
            "spread": 20,
        },
        index=index,
    )


def at_end(base: Signal, frame: pd.DataFrame, direction: Direction, entry: float) -> Signal:
    when = frame.index[-1].to_pydatetime() + dt.timedelta(hours=1)
    return dataclasses.replace(base, time=when, direction=direction, entry=entry, tags=())


def test_trend_tags_follow_the_daily_and_four_hour_structure(base_signal: Signal) -> None:
    frame = rising_hours()
    frames = ContextFrames.from_hourly(frame, DayBoundaryConfig())
    price = float(frame["close"].iloc[-1])
    long = context_tags(at_end(base_signal, frame, Direction.LONG, price), frames)
    assert {"d1_trend", "h4_trend", "ema_trend"} <= set(long)
    short = context_tags(at_end(base_signal, frame, Direction.SHORT, price), frames)
    assert not {"d1_trend", "h4_trend", "ema_trend"} & set(short)


def test_weekly_value_and_high_volatility(base_signal: Signal) -> None:
    frame = rising_hours()
    frames = ContextFrames.from_hourly(frame, DayBoundaryConfig())
    cheap = at_end(base_signal, frame, Direction.LONG, 1900.0)  # far below last week
    dear = at_end(base_signal, frame, Direction.LONG, 2400.0)
    assert "weekly_value" in context_tags(cheap, frames)
    assert "weekly_value" not in context_tags(dear, frames)
    assert "weekly_value" in context_tags(
        at_end(base_signal, frame, Direction.SHORT, 2400.0), frames
    )
    assert "high_vol" not in context_tags(dear, frames)
    wild = rising_hours(spike=6)
    wild_frames = ContextFrames.from_hourly(wild, DayBoundaryConfig())
    assert "high_vol" in context_tags(
        at_end(base_signal, wild, Direction.LONG, 2400.0), wild_frames
    )


def test_only_closed_bars_count(base_signal: Signal) -> None:
    frame = rising_hours()
    frames = ContextFrames.from_hourly(frame, DayBoundaryConfig())
    early = dataclasses.replace(
        at_end(base_signal, frame, Direction.LONG, 2000.0), time=START + dt.timedelta(hours=3)
    )
    assert context_tags(early, frames) == ()  # nothing has closed yet: no context


def test_tags_all_makes_a_trend_a_condition(base_signal: Signal) -> None:
    frame = rising_hours()
    frames = ContextFrames.from_hourly(frame, DayBoundaryConfig())
    tagged = with_context(
        at_end(base_signal, frame, Direction.LONG, float(frame["close"].iloc[-1])), frames
    )
    tagged = dataclasses.replace(tagged, score=91.1)
    assert meets(tagged, PlaybookRule(tags_all=("d1_trend",)), neutral_extra=5)
    against = dataclasses.replace(tagged, tags=())
    assert not meets(against, PlaybookRule(tags_all=("d1_trend",)), neutral_extra=5)
    # min_tags counts the confluence tags only, not the context tags.
    context_only = dataclasses.replace(tagged, tags=("d1_trend", "h4_trend", "ema_trend"))
    assert not meets(context_only, PlaybookRule(min_tags=1), neutral_extra=5)


def test_the_breakout_model_follows_breaks_not_sweeps(settings: Settings) -> None:
    from tests.test_backtest_run import FOLLOW_UP
    from tests.test_signals import POINT, feed, with_raid

    config = settings.config
    engine = SignalEngine(
        config.strategy,
        config.timeframes,
        SessionCalculator(config.sessions),
        point=POINT,
        model=MODELS["breakout"],
    )
    run = feed(engine, with_raid() + FOLLOW_UP)
    reasons = {r.reason for r in run.rejections}
    # The Asian-low raid closed back inside: the sweep model's trade, not a breakout.
    assert "swept back inside: not a breakout" in reasons
    # The previous-day high broke and held, but nothing was left above to target.
    assert "no external liquidity pool to target" in reasons
    assert run.signals == []

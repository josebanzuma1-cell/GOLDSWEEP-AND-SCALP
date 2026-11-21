"""The scalper's step-trail line and rolling ATR (S1, prompt section 4.1)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from goldsweep.analysis.indicators import atr_values
from goldsweep.scalping.trail import DOWN, NOT_READY, UP, RollingAtr, StepTrail, step_trail
from tests.helpers import bars, utc


def random_rows(count: int, seed: int = 7) -> list[tuple[float, float, float, float]]:
    """A random walk of candles (closes drift, wicks of random size)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = 2000.0
    for _ in range(count):
        open_ = price
        close = open_ + float(rng.normal(0, 0.8))
        high = max(open_, close) + float(rng.uniform(0, 0.6))
        low = min(open_, close) - float(rng.uniform(0, 0.6))
        rows.append((open_, high, low, close))
        price = close
    return rows


def test_rolling_atr_equals_the_simple_average_atr() -> None:
    frame = bars(random_rows(200), utc(2026, 11, 17), minutes=1)
    high, low, close = (frame[c].to_numpy() for c in ("high", "low", "close"))
    expected = atr_values(high, low, close, 5, "sma")
    rolling = RollingAtr(5)
    values = [rolling.update(h, lo, c) for h, lo, c in zip(high, low, close, strict=True)]
    assert values[:4] == [None] * 4
    assert values[4:] == pytest.approx(list(expected[4:]))
    with pytest.raises(ValueError, match="at least 1"):
        RollingAtr(0)


def test_the_line_moves_only_with_the_trend_and_flips_on_a_close_through_it() -> None:
    rows = random_rows(600, seed=11)
    frame = step_trail(bars(rows, utc(2026, 11, 17), minutes=1), atr_period=10, multiplier=2.0)
    line, trend, flipped = (frame[c].to_numpy() for c in ("line", "trend", "flipped"))
    closes = [row[3] for row in rows]
    assert np.isnan(line[:9]).all() and (trend[:9] == NOT_READY).all()
    flips = 0
    for i in range(10, len(rows)):
        crossed = (trend[i - 1] == UP and closes[i] < line[i - 1]) or (
            trend[i - 1] == DOWN and closes[i] > line[i - 1]
        )
        assert flipped[i] == crossed, i
        if flipped[i]:
            flips += 1
            assert trend[i] == -trend[i - 1]
        elif trend[i] == UP:
            assert line[i] >= line[i - 1] - 1e-12, i  # the stairs only go up
        else:
            assert line[i] <= line[i - 1] + 1e-12, i  # ... or only down
        assert (line[i] < closes[i]) if trend[i] == UP else (line[i] > closes[i])
    assert flips > 5  # the walk does change direction


def test_a_trail_point_reports_the_line_in_force_during_the_bar() -> None:
    trail = StepTrail(atr_period=2, multiplier=1.0)
    first = trail.update(10.0, 9.0, 9.5)
    assert first.trend == NOT_READY and math.isnan(first.line)
    second = trail.update(10.0, 9.0, 9.2)  # ATR 1.0, hl2 9.5: down, line 10.5
    assert (second.trend, second.line) == (DOWN, pytest.approx(10.5))
    third = trail.update(11.2, 10.4, 11.0)  # closes above 10.5: flips up
    assert third.flipped and third.trend == UP
    assert third.previous_line == pytest.approx(10.5)
    with pytest.raises(ValueError, match="positive"):
        StepTrail(atr_period=3, multiplier=0.0)

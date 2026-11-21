"""Candle Range Theory (CRT).

A higher-timeframe candle is treated as a range: its high (CRH) and its low
(CRL) hold liquidity. The pattern takes three candles:

1. **Range**: candle 1 sets the high and the low.
2. **Manipulation**: candle 2 trades beyond one side of that range and closes
   back inside it. The liquidity on that side has been taken.
3. **Distribution**: price is then expected to travel to the other side of the
   range.

So a candle that takes candle 1's low and closes back above it is a bullish CRT
with candle 1's high as its target, and the mirror image is bearish.

GoldSweep uses CRT in two ways:

* The last closed candle's high and low become liquidity pools, so a raid of
  either side during the *forming* candle is caught on the trigger timeframe by
  the normal sweep, market-structure-shift and entry-zone logic. That is the
  usual CRT entry.
* A CRT confirmed on the higher timeframe gives a directional lean for the next
  candle or two, which adds to the score of signals that agree with it.

The higher-timeframe candles are anchored to the 17:00 New York close, so the
4-hour candles open at 17:00, 21:00, 01:00, 05:00, 09:00 and 13:00 New York time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from goldsweep.analysis.liquidity import Pool, PoolKind, Side, make_pool
from goldsweep.analysis.periods import aggregate_anchored, closed_as_of
from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ensure_utc
from goldsweep.core.types import Timeframe

if TYPE_CHECKING:
    from goldsweep.core.settings import DayBoundaryConfig


@dataclass(frozen=True, slots=True)
class CrtSetup:
    """A confirmed Candle Range Theory pattern (candles 1 and 2 have closed)."""

    timeframe: Timeframe
    direction: Direction
    range_time: dt.datetime  # open of candle 1, the range
    sweep_time: dt.datetime  # open of candle 2, the manipulation
    confirmed_at: dt.datetime  # close of candle 2: when the pattern is known
    range_high: float
    range_low: float
    sweep_extreme: float  # how far candle 2 went beyond the range

    @property
    def target(self) -> float:
        """The opposite side of the range."""
        return self.range_high if self.direction is Direction.LONG else self.range_low

    @property
    def invalidation(self) -> float:
        """The pattern has failed once price trades beyond candle 2's extreme."""
        return self.sweep_extreme

    def describe(self) -> str:
        side = "low" if self.direction is Direction.LONG else "high"
        return (
            f"{self.timeframe.value} CRT: the {self.sweep_time:%H:%M} UTC candle took the range "
            f"{side} and closed back inside; target {self.target:g}"
        )


def range_candles(
    candles: pd.DataFrame, timeframe: Timeframe, boundary: DayBoundaryConfig, now: dt.datetime
) -> pd.DataFrame:
    """Closed, New-York-anchored ``timeframe`` candles built from lower-timeframe bars."""
    return closed_as_of(aggregate_anchored(candles, timeframe, boundary), timeframe, now)


def detect_crt(ranges: pd.DataFrame, timeframe: Timeframe) -> list[CrtSetup]:
    """Every CRT in a frame of closed higher-timeframe candles, oldest first.

    A candle that takes *both* sides of the previous one says nothing about
    direction and is skipped, and so is one that fails to close back inside.
    """
    setups: list[CrtSetup] = []
    high = ranges["high"].to_numpy(dtype="float64")
    low = ranges["low"].to_numpy(dtype="float64")
    close = ranges["close"].to_numpy(dtype="float64")
    for i in range(1, len(ranges)):
        took_low, took_high = low[i] < low[i - 1], high[i] > high[i - 1]
        if took_low and not took_high and close[i] > low[i - 1]:
            direction, extreme = Direction.LONG, low[i]
        elif took_high and not took_low and close[i] < high[i - 1]:
            direction, extreme = Direction.SHORT, high[i]
        else:
            continue
        opened = ranges.index[i].to_pydatetime()
        setups.append(
            CrtSetup(
                timeframe=timeframe,
                direction=direction,
                range_time=ranges.index[i - 1].to_pydatetime(),
                sweep_time=opened,
                confirmed_at=opened + timeframe.duration,
                range_high=float(high[i - 1]),
                range_low=float(low[i - 1]),
                sweep_extreme=float(extreme),
            )
        )
    return setups


def crt_pools(ranges: pd.DataFrame, timeframe: Timeframe) -> list[Pool]:
    """The last closed candle's high and low: the range the forming candle may raid.

    They exist from that candle's close until the next candle closes, when a new
    range takes over.
    """
    if ranges.empty:
        return []
    closed_at = ranges.index[-1].to_pydatetime() + timeframe.duration
    expires = closed_at + timeframe.duration
    label = f"{timeframe.value} CRT range"
    return [
        make_pool(
            PoolKind.CRT_HIGH,
            Side.BUY_SIDE,
            float(ranges["high"].iloc[-1]),
            closed_at,
            label=f"{label} high",
            expires=expires,
        ),
        make_pool(
            PoolKind.CRT_LOW,
            Side.SELL_SIDE,
            float(ranges["low"].iloc[-1]),
            closed_at,
            label=f"{label} low",
            expires=expires,
        ),
    ]


def active_crt(
    setups: Sequence[CrtSetup],
    lower: pd.DataFrame,
    now: dt.datetime,
    *,
    max_age_candles: int,
) -> CrtSetup | None:
    """The most recent confirmed CRT that is still in play at ``now``.

    A CRT stops being in play when it is older than ``max_age_candles`` candles,
    when price has reached its target, or when price has traded beyond its
    invalidation level. ``lower`` holds lower-timeframe bars to judge that by.
    """
    instant = ensure_utc(now)
    for setup in reversed(setups):
        if setup.confirmed_at > instant:
            continue
        if instant - setup.confirmed_at > setup.timeframe.duration * max_age_candles:
            return None  # every earlier one is older still
        since = lower.loc[lower.index >= pd.Timestamp(setup.confirmed_at)]
        if not since.empty:
            highest, lowest = float(since["high"].max()), float(since["low"].min())
            if setup.direction is Direction.LONG:
                done, failed = highest >= setup.target, lowest < setup.invalidation
            else:
                done, failed = lowest <= setup.target, highest > setup.invalidation
            if done or failed:
                return None  # the latest pattern has played out or failed
        return setup
    return None

"""Market structure: swings, trend, break of structure, market-structure shift.

Definitions (they follow the market-structure guide):

* **Swing high / low**: a fractal pivot, a bar whose high (low) stands above
  (below) ``lookback`` bars on each side. A swing is only *known* once the
  ``lookback`` bars after it have closed, and nothing here uses it earlier.
* **Uptrend**: higher highs and higher lows. **Downtrend**: lower lows and lower
  highs. Anything else is a **range**.
* **Break of structure (BOS)**: a close beyond the last swing in the direction of
  the trend. The trend continues.
* **Market-structure shift (MSS)**: a close beyond the level that was holding the
  trend: below the last higher low of an uptrend, or above the last lower high of
  a downtrend. The trend has changed hands.
* **Displacement**: a candle that moves with force. Its body is at least
  ``atr_multiple`` ATRs, or it is at least an average-sized candle that is mostly
  body.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

if TYPE_CHECKING:
    from goldsweep.core.settings import StrategyConfig


class Direction(StrEnum):
    """Trade direction. Also used for the direction of a structure break."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Trend(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"


class SwingKind(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class StructureEventKind(StrEnum):
    BOS = "BOS"
    MSS = "MSS"


@dataclass(frozen=True, slots=True)
class Swing:
    """A confirmed fractal pivot."""

    kind: SwingKind
    index: int  # position of the pivot bar in the frame
    time: dt.datetime  # open time of the pivot bar (UTC)
    price: float
    confirmed_index: int  # first bar at whose close the swing is known


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """A close through a swing level: continuation (BOS) or reversal (MSS)."""

    kind: StructureEventKind
    direction: Direction  # LONG = bullish break, SHORT = bearish break
    index: int
    time: dt.datetime
    level: float
    swing_index: int


@dataclass(frozen=True, slots=True)
class DisplacementRule:
    """When a candle counts as displacement (spec 5.4)."""

    atr_multiple: float = 1.2
    body_ratio: float = 0.70
    min_range_atr: float = 1.0

    @classmethod
    def from_config(cls, config: StrategyConfig) -> DisplacementRule:
        return cls(
            atr_multiple=config.displacement_atr,
            body_ratio=config.displacement_body_ratio,
            min_range_atr=config.displacement_min_range_atr,
        )


@dataclass(frozen=True, slots=True)
class MssEvent:
    """The market-structure shift that confirms a sweep."""

    direction: Direction
    index: int  # bar that closed through the level
    time: dt.datetime
    level: float  # price of the swing that was closed through
    swing_index: int
    displacement_index: int  # strongest displacement candle of the leg
    displacement_atr: float  # its body, in ATRs


# --------------------------------------------------------------------------- #
# Swings and trend
# --------------------------------------------------------------------------- #


def swing_indices(
    high: np.ndarray, low: np.ndarray, lookback: int
) -> tuple[np.ndarray, np.ndarray]:
    """Positions of fractal swing highs and lows.

    A swing high must be strictly above the ``lookback`` bars before it and at
    least as high as the ``lookback`` bars after it, so two equal highs give one
    pivot (the first) rather than two or none.
    """
    if lookback < 1:
        raise ValueError("lookback must be at least 1")
    width = 2 * lookback + 1
    if len(high) < width:
        empty = np.array([], dtype="int64")
        return empty, empty
    centre_high = high[lookback : len(high) - lookback]
    centre_low = low[lookback : len(low) - lookback]
    windows_high = sliding_window_view(high, width)
    windows_low = sliding_window_view(low, width)
    is_high = (centre_high > windows_high[:, :lookback].max(axis=1)) & (
        centre_high >= windows_high[:, lookback + 1 :].max(axis=1)
    )
    is_low = (centre_low < windows_low[:, :lookback].min(axis=1)) & (
        centre_low <= windows_low[:, lookback + 1 :].min(axis=1)
    )
    return np.flatnonzero(is_high) + lookback, np.flatnonzero(is_low) + lookback


def find_swings(candles: pd.DataFrame, lookback: int) -> list[Swing]:
    """Every confirmed swing in ``candles``, oldest first.

    The last ``lookback`` bars cannot hold a confirmed swing yet, which is
    exactly what prevents look-ahead.
    """
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    highs, lows = swing_indices(high, low, lookback)
    high_times = candles.index[highs].to_pydatetime()
    low_times = candles.index[lows].to_pydatetime()
    swings = [
        Swing(SwingKind.HIGH, int(i), time, float(high[i]), int(i) + lookback)
        for i, time in zip(highs, high_times, strict=True)
    ]
    swings += [
        Swing(SwingKind.LOW, int(i), time, float(low[i]), int(i) + lookback)
        for i, time in zip(lows, low_times, strict=True)
    ]
    return sorted(swings, key=lambda swing: (swing.index, swing.kind.value))


def classify_trend(swings: Sequence[Swing]) -> Trend:
    """Trend from the last two swing highs and the last two swing lows."""
    highs = [swing for swing in swings if swing.kind is SwingKind.HIGH][-2:]
    lows = [swing for swing in swings if swing.kind is SwingKind.LOW][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return Trend.RANGE
    higher_high = highs[1].price > highs[0].price
    higher_low = lows[1].price > lows[0].price
    lower_high = highs[1].price < highs[0].price
    lower_low = lows[1].price < lows[0].price
    if higher_high and higher_low:
        return Trend.UP
    if lower_high and lower_low:
        return Trend.DOWN
    return Trend.RANGE


def structure_events(candles: pd.DataFrame, lookback: int) -> list[StructureEvent]:
    """Label every close through a swing level as a BOS or an MSS.

    The walk keeps one "last swing high" and one "last swing low". A close above
    the last swing high is bullish: a BOS if the trend was already up, an MSS if
    it was down. A close below the last swing low is the mirror image. The very
    first break has no trend to continue or reverse and is recorded as a BOS.
    """
    swings = find_swings(candles, lookback)
    by_confirmation: dict[int, list[Swing]] = {}
    for swing in swings:
        by_confirmation.setdefault(swing.confirmed_index, []).append(swing)

    close = candles["close"].to_numpy(dtype="float64")
    times = candles.index
    events: list[StructureEvent] = []
    trend: Trend | None = None
    last_high: Swing | None = None
    last_low: Swing | None = None
    for i in range(len(close)):
        for swing in by_confirmation.get(i, ()):
            if swing.kind is SwingKind.HIGH:
                last_high = swing
            else:
                last_low = swing
        if last_high is not None and close[i] > last_high.price:
            kind = StructureEventKind.MSS if trend is Trend.DOWN else StructureEventKind.BOS
            events.append(
                StructureEvent(
                    kind,
                    Direction.LONG,
                    i,
                    times[i].to_pydatetime(),
                    last_high.price,
                    last_high.index,
                )
            )
            trend, last_high = Trend.UP, None
        elif last_low is not None and close[i] < last_low.price:
            kind = StructureEventKind.MSS if trend is Trend.UP else StructureEventKind.BOS
            events.append(
                StructureEvent(
                    kind,
                    Direction.SHORT,
                    i,
                    times[i].to_pydatetime(),
                    last_low.price,
                    last_low.index,
                )
            )
            trend, last_low = Trend.DOWN, None
    return events


# --------------------------------------------------------------------------- #
# Displacement and the MSS that follows a sweep
# --------------------------------------------------------------------------- #


def displacement_atr(
    open_: float,
    high: float,
    low: float,
    close: float,
    atr_before: float,
    direction: Direction,
    rule: DisplacementRule,
) -> float | None:
    """Body size in ATRs if the candle is a displacement candle in ``direction``.

    ``atr_before`` is the ATR of the bar *before* this candle, so a large candle
    is measured against the volatility that preceded it, not against itself.
    Returns ``None`` when the candle does not qualify.
    """
    body = (close - open_) * direction.sign
    if body <= 0 or not np.isfinite(atr_before) or atr_before <= 0:
        return None
    candle_range = high - low
    if body >= rule.atr_multiple * atr_before:
        return body / atr_before
    if (
        candle_range > 0
        and body >= rule.body_ratio * candle_range
        and candle_range >= rule.min_range_atr * atr_before
    ):
        return body / atr_before
    return None


def strongest_displacement(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    direction: Direction,
    first: int,
    last: int,
    rule: DisplacementRule,
) -> tuple[int, float] | None:
    """The displacement candle with the largest body (in ATRs) in ``[first, last]``."""
    best: tuple[int, float] | None = None
    for i in range(max(first, 1), last + 1):
        size = displacement_atr(open_[i], high[i], low[i], close[i], atr[i - 1], direction, rule)
        if size is not None and (best is None or size > best[1]):
            best = (i, size)
    return best


def latest_swing(
    swings: Sequence[Swing], kind: SwingKind, *, known_at: int, earliest: int
) -> Swing | None:
    """The most recent swing of ``kind`` that is confirmed by bar ``known_at``.

    Swings whose pivot lies before ``earliest`` are too old to matter and are skipped.
    """
    for swing in reversed(swings):
        if (
            swing.kind is kind
            and swing.confirmed_index <= known_at
            and earliest <= swing.index < known_at
        ):
            return swing
    return None


def find_mss(
    candles: pd.DataFrame,
    atr: np.ndarray,
    swings: Sequence[Swing],
    direction: Direction,
    *,
    extreme_index: int,
    reclaim_index: int,
    earliest_swing_index: int,
    window: int,
    rule: DisplacementRule,
) -> MssEvent | None:
    """Look for the market-structure shift that confirms a sweep (spec 5.4).

    For a long (after sell-side liquidity was swept) this is the first bar, no
    later than ``window`` bars after the sweep closed back inside, that closes
    above the most recent confirmed swing high, provided the leg up from the
    sweep extreme contains a displacement candle. Shorts mirror it.

    Args:
        candles: closed trigger-timeframe bars; the search ends at the last one.
        atr: ATR per bar of ``candles``.
        swings: confirmed swings of ``candles``.
        direction: the trade direction the sweep points to.
        extreme_index: bar of the sweep's extreme (start of the leg).
        reclaim_index: bar that closed back inside the swept level.
        earliest_swing_index: swings before this bar are ignored.
        window: maximum bars between ``reclaim_index`` and the shift.
        rule: what counts as displacement.
    """
    open_ = candles["open"].to_numpy(dtype="float64")
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    close = candles["close"].to_numpy(dtype="float64")
    kind = SwingKind.HIGH if direction is Direction.LONG else SwingKind.LOW
    last = min(reclaim_index + window, len(close) - 1)
    for i in range(reclaim_index, last + 1):
        swing = latest_swing(swings, kind, known_at=i, earliest=earliest_swing_index)
        if swing is None:
            continue
        if (close[i] - swing.price) * direction.sign <= 0:
            continue
        best = strongest_displacement(
            open_, high, low, close, atr, direction, extreme_index, i, rule
        )
        if best is None:
            continue
        return MssEvent(
            direction=direction,
            index=i,
            time=candles.index[i].to_pydatetime(),
            level=swing.price,
            swing_index=swing.index,
            displacement_index=best[0],
            displacement_atr=best[1],
        )
    return None

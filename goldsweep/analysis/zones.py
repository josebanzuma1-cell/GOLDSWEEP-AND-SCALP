"""Entry zones: fair value gaps, inversion fair value gaps and order blocks.

Definitions (they follow the fair-value-gap guide and spec 5.5):

* **Fair value gap (FVG)**: three candles where the first and the third do not
  overlap. Bullish: candle 3's low is above candle 1's high, and the gap is
  everything between those two prices. Bearish: candle 3's high is below candle
  1's low. The middle candle should be a displacement candle; a gap left by a
  weak candle is not worth trading.
* **Inversion FVG (IFVG)**: a gap that failed. Price closed straight through it
  instead of respecting it, so it now works for the other side: a bearish gap
  that price closed above becomes support.
* **Order block**: the last candle of the opposite colour before the
  displacement. It is the fallback when the move left no usable gap.

A zone is only usable from ``known_index`` on: the bar at whose close it exists.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import pandas as pd

from goldsweep.analysis.structure import Direction, DisplacementRule, displacement_atr


class ZoneKind(StrEnum):
    FVG = "fvg"
    IFVG = "ifvg"
    ORDER_BLOCK = "order_block"
    BREAKER = "breaker"  # a confluence (Unicorn), not an entry zone (D85)


@dataclass(frozen=True, slots=True)
class Zone:
    """A price band to enter from. ``direction`` is the trade it supports."""

    kind: ZoneKind
    direction: Direction
    top: float
    bottom: float
    origin_index: int  # middle candle of a gap, or the order-block candle
    known_index: int  # first bar at whose close the zone exists
    time: dt.datetime  # open time of the origin candle (UTC)
    displacement_atr: float | None = None  # body of the middle candle in ATRs, for gaps

    @property
    def mid(self) -> float:
        """The 50% level: the "mean threshold" the guide recommends for entries."""
        return (self.top + self.bottom) / 2.0

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def near(self) -> float:
        """The edge a pullback reaches first: the top for a long, the bottom for a short."""
        return self.top if self.direction is Direction.LONG else self.bottom

    @property
    def far(self) -> float:
        """The edge a pullback reaches last (the deepest entry inside the zone)."""
        return self.bottom if self.direction is Direction.LONG else self.top

    def price_for(self, entry_mode: str) -> float:
        """Entry price inside the zone for ``fvg_top`` / ``fvg_mid`` / ``fvg_bottom`` /
        ``fvg_near`` / ``fvg_far``. (``ote`` needs the swing: see :func:`ote_price`.)"""
        if entry_mode == "fvg_top":
            return self.top
        if entry_mode == "fvg_bottom":
            return self.bottom
        if entry_mode == "fvg_mid":
            return self.mid
        if entry_mode == "fvg_near":
            return self.near
        if entry_mode == "fvg_far":
            return self.far
        raise ValueError(f"entry mode {entry_mode!r} does not name a price inside a zone")


def ote_price(zone: Zone, extreme: float, leg_end: float, level: float) -> float:
    """The optimal-trade-entry price: ``level`` of the way back from ``leg_end`` toward
    ``extreme`` (the sweep's wick), kept inside ``zone``.

    For a long that swept a low at 2020 and rallied to 2030, level 0.705 gives
    2030 - 0.705 x 10 = 2022.95, then clamped to the zone's bottom..top.
    """
    price = leg_end - level * (leg_end - extreme)
    return min(max(price, zone.bottom), zone.top)


def _arrays(candles: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        candles["open"].to_numpy(dtype="float64"),
        candles["high"].to_numpy(dtype="float64"),
        candles["low"].to_numpy(dtype="float64"),
        candles["close"].to_numpy(dtype="float64"),
    )


def find_fvgs(
    candles: pd.DataFrame,
    atr: np.ndarray,
    direction: Direction,
    *,
    first: int = 1,
    last: int | None = None,
    rule: DisplacementRule | None = None,
    min_size_atr: float = 0.0,
) -> list[Zone]:
    """Fair value gaps whose middle candle lies in ``[first, last]``, oldest first.

    Only gaps whose third candle has closed are returned. With ``rule`` given,
    the middle candle must be a displacement candle. ``min_size_atr`` drops gaps
    too small to matter (measured against the ATR before the middle candle).
    """
    open_, high, low, close = _arrays(candles)
    end = len(close) - 2 if last is None else min(last, len(close) - 2)
    zones: list[Zone] = []
    for i in range(max(first, 1), end + 1):
        if direction is Direction.LONG:
            bottom, top = high[i - 1], low[i + 1]
        else:
            bottom, top = high[i + 1], low[i - 1]
        if top <= bottom:
            continue
        atr_before = atr[i - 1]
        if (
            min_size_atr > 0
            and np.isfinite(atr_before)
            and top - bottom < min_size_atr * atr_before
        ):
            continue
        strength = displacement_atr(
            open_[i], high[i], low[i], close[i], atr_before, direction, rule or DisplacementRule()
        )
        if rule is not None and strength is None:
            continue
        zones.append(
            Zone(
                kind=ZoneKind.FVG,
                direction=direction,
                top=float(top),
                bottom=float(bottom),
                origin_index=i,
                known_index=i + 1,
                time=candles.index[i].to_pydatetime(),
                displacement_atr=strength,
            )
        )
    return zones


def open_gaps(
    frame: pd.DataFrame, *, atr_period: int = 14, min_size_atr: float = 0.05, keep: int = 8
) -> list[Zone]:
    """Fair value gaps on these bars that price has not traded back through, newest last
    (for the dashboard chart and the MT5 panel).

    A bullish gap is closed once a later low reaches its bottom; a bearish gap
    once a later high reaches its top.
    """
    from goldsweep.analysis.indicators import atr_values

    if len(frame) < 3:
        return []
    atr = atr_values(
        frame["high"].to_numpy(), frame["low"].to_numpy(), frame["close"].to_numpy(), atr_period
    )
    highs, lows = frame["high"].to_numpy(), frame["low"].to_numpy()
    found: list[Zone] = []
    for direction in (Direction.LONG, Direction.SHORT):
        for zone in find_fvgs(frame, atr, direction, min_size_atr=min_size_atr):
            later = slice(zone.known_index + 1, None)
            if direction is Direction.LONG:
                closed = bool(np.any(lows[later] <= zone.bottom))
            else:
                closed = bool(np.any(highs[later] >= zone.top))
            if not closed:
                found.append(zone)
    found.sort(key=lambda z: z.time)
    return found[-keep:]


def first_touch(candles: pd.DataFrame, zone: Zone, price: float, *, start: int) -> int | None:
    """First bar at or after ``start`` that trades back to ``price``.

    For a long zone that means a low at or below ``price``; for a short zone a
    high at or above it. ``None`` means the level is still untouched.
    """
    if zone.direction is Direction.LONG:
        hits = np.flatnonzero(candles["low"].to_numpy(dtype="float64")[start:] <= price)
    else:
        hits = np.flatnonzero(candles["high"].to_numpy(dtype="float64")[start:] >= price)
    return int(hits[0]) + start if len(hits) else None


def find_inversions(
    candles: pd.DataFrame,
    atr: np.ndarray,
    direction: Direction,
    *,
    first: int,
    last: int,
    after: int,
    min_size_atr: float = 0.0,
) -> list[Zone]:
    """Inversion FVGs that now support a trade in ``direction``, oldest first.

    They start life as gaps of the *opposite* direction, with their middle
    candle in ``[first, last]``. One becomes an inversion when a later bar
    closes beyond its far side. Only inversions that happen at or after bar
    ``after`` count: a gap price already closed through earlier is old news.
    """
    close = candles["close"].to_numpy(dtype="float64")
    zones: list[Zone] = []
    for gap in find_fvgs(
        candles, atr, direction.opposite, first=first, last=last, min_size_atr=min_size_atr
    ):
        if direction is Direction.LONG:
            closed_through = np.flatnonzero(close[gap.known_index + 1 :] > gap.top)
        else:
            closed_through = np.flatnonzero(close[gap.known_index + 1 :] < gap.bottom)
        if len(closed_through) == 0:
            continue
        inverted_at = int(closed_through[0]) + gap.known_index + 1
        if inverted_at < after:
            continue
        zones.append(
            Zone(
                kind=ZoneKind.IFVG,
                direction=direction,
                top=gap.top,
                bottom=gap.bottom,
                origin_index=gap.origin_index,
                known_index=inverted_at,
                time=gap.time,
            )
        )
    return zones


def find_order_block(
    candles: pd.DataFrame,
    direction: Direction,
    *,
    displacement_index: int,
    earliest: int,
) -> Zone | None:
    """The last opposite-coloured candle before the displacement candle.

    The search runs backwards from the bar before ``displacement_index`` and
    stops at ``earliest``. The zone is that candle's full range.
    """
    open_, high, low, close = _arrays(candles)
    for i in range(displacement_index - 1, max(earliest, 0) - 1, -1):
        if (close[i] - open_[i]) * direction.sign < 0:
            return Zone(
                kind=ZoneKind.ORDER_BLOCK,
                direction=direction,
                top=float(high[i]),
                bottom=float(low[i]),
                origin_index=i,
                known_index=displacement_index,
                time=candles.index[i].to_pydatetime(),
            )
    return None


def find_breaker(
    candles: pd.DataFrame,
    direction: Direction,
    *,
    extreme_index: int,
    earliest: int,
    upto: int,
) -> Zone | None:
    """The breaker block of a sweep (Phase 9, D85).

    For a long: the last up-closing candle before the drop that swept the low,
    which price has since closed above (so the sellers it stood for failed). The
    zone is that candle's full range, known from the bar that closed above it.
    Shorts mirror it. ``None`` when there is no such candle, or price has not yet
    closed through it by bar ``upto``.
    """
    open_, high, low, close = _arrays(candles)
    sign = direction.sign
    for i in range(extreme_index - 1, max(earliest, 0) - 1, -1):
        if (close[i] - open_[i]) * sign <= 0:
            continue
        edge = high[i] if direction is Direction.LONG else low[i]
        for j in range(extreme_index + 1, min(upto, len(close) - 1) + 1):
            if (close[j] - edge) * sign > 0:
                return Zone(
                    kind=ZoneKind.BREAKER,
                    direction=direction,
                    top=float(high[i]),
                    bottom=float(low[i]),
                    origin_index=i,
                    known_index=j,
                    time=candles.index[i].to_pydatetime(),
                )
        return None
    return None


def overlaps(first: Zone, second: Zone) -> bool:
    """True when the two price bands share some range."""
    return first.bottom < second.top and second.bottom < first.top

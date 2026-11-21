"""Liquidity pools and liquidity sweeps (spec 5.2 and 5.3, plus the liquidity guide).

Liquidity is where orders rest: stop losses and breakout orders cluster above
highs (**buy-side** liquidity) and below lows (**sell-side** liquidity). Price
tends to travel from one pool to the next.

Pools found here:

==================  ==========================================================
Asian range         high and low of the Asia session
London / New York   high and low of the earlier sessions of the trading day
Previous day/week   high and low of the last complete trading day and week
Equal highs / lows  two or more swing points at almost the same price
Swing points        unswept swing highs and lows (major ones count as external)
Trendline           three or more rising lows, or falling highs, on one line
CRT range           high and low of the last closed higher-timeframe candle
Round numbers       psychological levels; the 50 and 00 levels count as major
==================  ==========================================================

**External** liquidity sits beyond the major structure and is what a move
targets. **Internal** liquidity sits inside the range; price may react there on
the way. Take-profit targets are chosen from external pools only.

A **sweep** (raid, stop hunt) is price trading beyond a pool and then closing
back inside within a few bars. A sweep of sell-side liquidity points to a long,
a sweep of buy-side liquidity to a short.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from goldsweep.analysis.periods import (
    trading_day_end,
    trading_day_of,
    trading_day_start,
    trading_days,
)
from goldsweep.analysis.structure import Direction, Swing, SwingKind, find_swings
from goldsweep.core.clock import ASIA, LONDON, NEW_YORK, SessionCalculator, ensure_utc

if TYPE_CHECKING:
    from goldsweep.core.settings import DayBoundaryConfig, LiquidityConfig


class Side(StrEnum):
    """Which side of price the liquidity rests on."""

    BUY_SIDE = "buy_side"  # above price: buy stops over highs
    SELL_SIDE = "sell_side"  # below price: sell stops under lows

    @property
    def trade_direction(self) -> Direction:
        """The trade a sweep of this side points to."""
        return Direction.LONG if self is Side.SELL_SIDE else Direction.SHORT

    @property
    def sign(self) -> int:
        """+1 when the pool is above price, -1 when below."""
        return 1 if self is Side.BUY_SIDE else -1


class Scope(StrEnum):
    EXTERNAL = "external"
    INTERNAL = "internal"


class PoolKind(StrEnum):
    ASIAN_HIGH = "asian_high"
    ASIAN_LOW = "asian_low"
    LONDON_HIGH = "london_high"
    LONDON_LOW = "london_low"
    NEWYORK_HIGH = "newyork_high"
    NEWYORK_LOW = "newyork_low"
    PREVIOUS_DAY_HIGH = "pdh"
    PREVIOUS_DAY_LOW = "pdl"
    PREVIOUS_WEEK_HIGH = "pwh"
    PREVIOUS_WEEK_LOW = "pwl"
    EQUAL_HIGHS = "equal_highs"
    EQUAL_LOWS = "equal_lows"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    TRENDLINE_HIGH = "trendline_high"
    TRENDLINE_LOW = "trendline_low"
    CRT_HIGH = "crt_high"
    CRT_LOW = "crt_low"
    ROUND_NUMBER = "round_number"


#: How much a pool of each kind matters, 0..1. Used for sweep quality and to
#: decide which sweeps are worth a setup at all.
POOL_STRENGTH: dict[PoolKind, float] = {
    PoolKind.PREVIOUS_WEEK_HIGH: 1.0,
    PoolKind.PREVIOUS_WEEK_LOW: 1.0,
    PoolKind.PREVIOUS_DAY_HIGH: 0.9,
    PoolKind.PREVIOUS_DAY_LOW: 0.9,
    PoolKind.ASIAN_HIGH: 0.9,
    PoolKind.ASIAN_LOW: 0.9,
    PoolKind.LONDON_HIGH: 0.8,
    PoolKind.LONDON_LOW: 0.8,
    PoolKind.NEWYORK_HIGH: 0.8,
    PoolKind.NEWYORK_LOW: 0.8,
    PoolKind.EQUAL_HIGHS: 0.8,
    PoolKind.EQUAL_LOWS: 0.8,
    PoolKind.CRT_HIGH: 0.7,
    PoolKind.CRT_LOW: 0.7,
    PoolKind.TRENDLINE_HIGH: 0.7,
    PoolKind.TRENDLINE_LOW: 0.7,
    PoolKind.SWING_HIGH: 0.6,
    PoolKind.SWING_LOW: 0.6,
    PoolKind.ROUND_NUMBER: 0.5,
}
MINOR_SWING_STRENGTH = 0.4

#: Prices are decimal numbers stored as binary floats: 107.5 - 107.45 is a hair
#: under 0.05. Threshold comparisons allow for that.
_EPSILON = 1e-9
MINOR_ROUND_NUMBER_STRENGTH = 0.3

_SECONDS_PER_DAY = 86_400
_EPOCH_DATE = dt.date(1970, 1, 1)
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _utc_from_us(microseconds: int) -> dt.datetime:
    """An aware UTC datetime from epoch microseconds."""
    return _EPOCH + dt.timedelta(microseconds=microseconds)


_LABELS: dict[PoolKind, str] = {
    PoolKind.ASIAN_HIGH: "Asian high",
    PoolKind.ASIAN_LOW: "Asian low",
    PoolKind.LONDON_HIGH: "London high",
    PoolKind.LONDON_LOW: "London low",
    PoolKind.NEWYORK_HIGH: "New York high",
    PoolKind.NEWYORK_LOW: "New York low",
    PoolKind.PREVIOUS_DAY_HIGH: "previous day high",
    PoolKind.PREVIOUS_DAY_LOW: "previous day low",
    PoolKind.PREVIOUS_WEEK_HIGH: "previous week high",
    PoolKind.PREVIOUS_WEEK_LOW: "previous week low",
    PoolKind.EQUAL_HIGHS: "equal highs",
    PoolKind.EQUAL_LOWS: "equal lows",
    PoolKind.SWING_HIGH: "swing high",
    PoolKind.SWING_LOW: "swing low",
    PoolKind.TRENDLINE_HIGH: "trendline highs",
    PoolKind.TRENDLINE_LOW: "trendline lows",
    PoolKind.CRT_HIGH: "CRT range high",
    PoolKind.CRT_LOW: "CRT range low",
    PoolKind.ROUND_NUMBER: "round number",
}


@dataclass(frozen=True, slots=True)
class Pool:
    """One liquidity pool: a price level with resting orders beyond it."""

    kind: PoolKind
    side: Side
    price: float
    created: dt.datetime  # UTC; only bars that open at or after this can sweep it
    scope: Scope
    strength: float  # 0..1
    label: str
    expires: dt.datetime | None = None
    swept: bool = False
    sources: tuple[PoolKind, ...] = ()  # every kind that coincides at this level

    @property
    def key(self) -> tuple[str, str, float]:
        """Identity of the level, stable across rebuilds."""
        return (self.kind.value, self.side.value, round(self.price, 4))

    @property
    def is_external(self) -> bool:
        return self.scope is Scope.EXTERNAL

    def as_swept(self) -> Pool:
        return replace(self, swept=True)


def make_pool(
    kind: PoolKind,
    side: Side,
    price: float,
    created: dt.datetime,
    *,
    scope: Scope = Scope.EXTERNAL,
    strength: float | None = None,
    label: str | None = None,
    expires: dt.datetime | None = None,
) -> Pool:
    """Build a pool with the default strength and label of its kind."""
    return Pool(
        kind=kind,
        side=side,
        price=float(price),
        created=ensure_utc(created),
        scope=scope,
        strength=POOL_STRENGTH[kind] if strength is None else strength,
        label=label or _LABELS[kind],
        expires=expires,
        sources=(kind,),
    )


# --------------------------------------------------------------------------- #
# Previous day and week
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DayLevels:
    """Reference prices for one trading day."""

    day: dt.date
    day_start: dt.datetime
    day_end: dt.datetime
    week_start: dt.datetime
    week_end: dt.datetime
    previous_high: float | None = None
    previous_low: float | None = None
    previous_week_high: float | None = None
    previous_week_low: float | None = None

    @property
    def previous_mid(self) -> float | None:
        """Midpoint of the previous day's range: above it is premium, below is discount."""
        if self.previous_high is None or self.previous_low is None:
            return None
        return (self.previous_high + self.previous_low) / 2.0


def day_levels(candles: pd.DataFrame, now: dt.datetime, boundary: DayBoundaryConfig) -> DayLevels:
    """Previous-day and previous-week highs and lows as of ``now``.

    ``candles`` are closed bars of any intraday timeframe (H1 is enough). The
    previous day is the most recent *complete* trading day that has bars, so a
    Monday looks back to Friday.
    """
    today = trading_day_of(now, boundary)
    monday = today - dt.timedelta(days=today.weekday())
    levels = DayLevels(
        day=today,
        day_start=trading_day_start(today, boundary),
        day_end=trading_day_end(today, boundary),
        week_start=trading_day_start(monday, boundary),
        week_end=trading_day_end(monday + dt.timedelta(days=4), boundary),
    )
    if candles.empty:
        return levels
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    # Trading days as whole days since 1970-01-01 (a Thursday), and the Monday of each.
    # pandas may store times in any unit: name the unit before doing arithmetic.
    days = trading_days(candles.index, boundary).as_unit("s").asi8 // _SECONDS_PER_DAY
    mondays = days - (days + 3) % 7
    today_number = (today - _EPOCH_DATE).days
    earlier = days < today_number
    if earlier.any():
        previous = days == days[earlier].max()
        levels = replace(
            levels,
            previous_high=float(high[previous].max()),
            previous_low=float(low[previous].min()),
        )
    before = mondays < today_number - today.weekday()
    if before.any():
        week = mondays == mondays[before].max()
        levels = replace(
            levels,
            previous_week_high=float(high[week].max()),
            previous_week_low=float(low[week].min()),
        )
    return levels


def day_week_pools(levels: DayLevels) -> list[Pool]:
    """PDH / PDL for the trading day and PWH / PWL for the trading week."""
    pools: list[Pool] = []
    daily = (
        (PoolKind.PREVIOUS_DAY_HIGH, Side.BUY_SIDE, levels.previous_high),
        (PoolKind.PREVIOUS_DAY_LOW, Side.SELL_SIDE, levels.previous_low),
    )
    for kind, side, price in daily:
        if price is not None:
            pools.append(make_pool(kind, side, price, levels.day_start, expires=levels.day_end))
    weekly = (
        (PoolKind.PREVIOUS_WEEK_HIGH, Side.BUY_SIDE, levels.previous_week_high),
        (PoolKind.PREVIOUS_WEEK_LOW, Side.SELL_SIDE, levels.previous_week_low),
    )
    for kind, side, price in weekly:
        if price is not None:
            pools.append(make_pool(kind, side, price, levels.week_start, expires=levels.week_end))
    return pools


# --------------------------------------------------------------------------- #
# Session ranges
# --------------------------------------------------------------------------- #


def _range_pools(
    candles: pd.DataFrame,
    start: dt.datetime,
    end: dt.datetime,
    high_kind: PoolKind,
    low_kind: PoolKind,
    expires: dt.datetime,
) -> list[Pool]:
    """High and low of the bars that open in ``[start, end)``, known from ``end`` on."""
    index = candles.index
    if candles.empty or index[0] > pd.Timestamp(start):
        return []  # the frame does not reach back to the start of the window
    first = int(index.searchsorted(pd.Timestamp(start), side="left"))
    stop = int(index.searchsorted(pd.Timestamp(end), side="left"))
    if stop <= first:
        return []
    high = float(candles["high"].to_numpy(dtype="float64")[first:stop].max())
    low = float(candles["low"].to_numpy(dtype="float64")[first:stop].min())
    return [
        make_pool(high_kind, Side.BUY_SIDE, high, end, expires=expires),
        make_pool(low_kind, Side.SELL_SIDE, low, end, expires=expires),
    ]


def session_pools(
    candles: pd.DataFrame,
    sessions: SessionCalculator,
    now: dt.datetime,
    boundary: DayBoundaryConfig,
) -> list[Pool]:
    """Highs and lows of the sessions already finished in this trading day.

    * Asian range: the Asia session window.
    * London range: from the London kill-zone open until the New York kill-zone
      opens, so it is ready as a target for New York.
    * New York range: the New York kill-zone window.

    A range becomes a pool only once its window has ended, and it lasts until
    the trading day ends.
    """
    instant = ensure_utc(now)
    today = trading_day_of(instant, boundary)
    day_start = trading_day_start(today, boundary)
    day_end = trading_day_end(today, boundary)
    pools: list[Pool] = []
    for day in (today - dt.timedelta(days=1), today):
        asia = sessions.span(ASIA, day)
        london_start = sessions.span(LONDON, day).start
        new_york = sessions.span(NEW_YORK, day)
        windows = (
            (asia.start, asia.end, PoolKind.ASIAN_HIGH, PoolKind.ASIAN_LOW),
            (london_start, new_york.start, PoolKind.LONDON_HIGH, PoolKind.LONDON_LOW),
            (new_york.start, new_york.end, PoolKind.NEWYORK_HIGH, PoolKind.NEWYORK_LOW),
        )
        for start, end, high_kind, low_kind in windows:
            if day_start <= start < end <= instant:
                pools += _range_pools(candles, start, end, high_kind, low_kind, day_end)
    return pools


# --------------------------------------------------------------------------- #
# Swing points, equal highs/lows and trendlines
# --------------------------------------------------------------------------- #


def _is_major(values: np.ndarray, index: int, lookback: int, kind: SwingKind) -> bool:
    """True when the pivot also stands out over the wider ``lookback`` on both sides."""
    if index + lookback > len(values) - 1 or index - lookback < 0:
        return False
    window = values[index - lookback : index + lookback + 1]
    return bool(
        values[index] >= window.max() if kind is SwingKind.HIGH else values[index] <= window.min()
    )


def _trendline(
    swings: Sequence[Swing], values: np.ndarray, kind: SwingKind, touches: int, tolerance: float
) -> Swing | None:
    """The latest touch of a trendline through the last ``touches`` swings, if there is one.

    For lows the swings must rise (for highs, fall), lie on one straight line
    within ``tolerance``, and no bar between the first and last touch may have
    traded through the line by more than ``tolerance``.
    """
    points = [swing for swing in swings if swing.kind is kind][-touches:]
    if len(points) < touches:
        return None
    sign = 1 if kind is SwingKind.LOW else -1
    prices = [point.price for point in points]
    if any(
        (later - earlier) * sign <= 0 for earlier, later in zip(prices, prices[1:], strict=False)
    ):
        return None
    first, last = points[0], points[-1]
    slope = (last.price - first.price) / (last.index - first.index)
    for point in points[1:-1]:
        if abs(point.price - (first.price + slope * (point.index - first.index))) > tolerance:
            return None
    positions = np.arange(first.index, last.index + 1)
    line = first.price + slope * (positions - first.index)
    breach = (line - values[first.index : last.index + 1]) * sign
    if (breach > tolerance).any():
        return None
    return last


def swing_pools(
    candles: pd.DataFrame,
    now: dt.datetime,
    config: LiquidityConfig,
    *,
    lookback: int,
    tolerance: float,
    bar_minutes: int,
) -> list[Pool]:
    """Pools from swing points: equal highs/lows, trendlines and single swings.

    A swing point still holds liquidity while no later bar has traded beyond it
    by more than ``tolerance``. Two or more such points within ``tolerance`` of
    each other are equal highs (or lows): the pool sits at the outermost one.
    """
    if candles.empty:
        return []
    swings = find_swings(candles, lookback)
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    bar = dt.timedelta(minutes=bar_minutes)
    stamps = candles.index.as_unit("us").asi8
    oldest = ensure_utc(now) - dt.timedelta(hours=config.max_pool_age_hours)
    pools: list[Pool] = []

    for kind, side, values in (
        (SwingKind.HIGH, Side.BUY_SIDE, high),
        (SwingKind.LOW, Side.SELL_SIDE, low),
    ):
        sign = side.sign
        alive: list[Swing] = []
        for swing in swings:
            if swing.kind is not kind or swing.time < oldest:
                continue
            later = values[swing.index + 1 :]
            if len(later) and ((later - swing.price) * sign > tolerance).any():
                continue  # price has already run through this level
            alive.append(swing)

        def known_at(swing: Swing) -> dt.datetime:
            return _utc_from_us(int(stamps[swing.confirmed_index])) + bar

        used: set[int] = set()
        equal_kind = PoolKind.EQUAL_HIGHS if kind is SwingKind.HIGH else PoolKind.EQUAL_LOWS
        by_price = sorted(alive, key=lambda swing: swing.price * -sign)  # outermost first
        position = 0
        while position < len(by_price):
            group = [by_price[position]]
            while (
                position + len(group) < len(by_price)
                and abs(by_price[position + len(group)].price - group[0].price) <= tolerance
            ):
                group.append(by_price[position + len(group)])
            if len(group) >= 2:
                pools.append(
                    make_pool(
                        equal_kind,
                        side,
                        group[0].price,
                        max(known_at(swing) for swing in group),
                        label=f"{_LABELS[equal_kind]} ({len(group)} touches)",
                    )
                )
                used.update(swing.index for swing in group)
            position += len(group)

        if config.trendlines:
            line_kind = (
                PoolKind.TRENDLINE_HIGH if kind is SwingKind.HIGH else PoolKind.TRENDLINE_LOW
            )
            touch = _trendline(swings, values, kind, config.trendline_min_touches, tolerance)
            if touch is not None and touch.index not in used and touch in alive:
                pools.append(
                    make_pool(
                        line_kind,
                        side,
                        touch.price,
                        known_at(touch),
                        label=f"{_LABELS[line_kind]} ({config.trendline_min_touches} touches)",
                    )
                )
                used.add(touch.index)

        if config.swing_points:
            single_kind = PoolKind.SWING_HIGH if kind is SwingKind.HIGH else PoolKind.SWING_LOW
            for swing in alive:
                if swing.index in used:
                    continue
                major = _is_major(values, swing.index, config.major_swing_lookback, kind)
                pools.append(
                    make_pool(
                        single_kind,
                        side,
                        swing.price,
                        known_at(swing),
                        scope=Scope.EXTERNAL if major else Scope.INTERNAL,
                        strength=POOL_STRENGTH[single_kind] if major else MINOR_SWING_STRENGTH,
                        label=("major " if major else "") + _LABELS[single_kind],
                    )
                )
    return pools


# --------------------------------------------------------------------------- #
# Round numbers
# --------------------------------------------------------------------------- #


def round_number_pools(price: float, now: dt.datetime, config: LiquidityConfig) -> list[Pool]:
    """The nearest round number above and below ``price``, plus the nearest major ones."""
    pools: dict[tuple[Side, float], Pool] = {}
    for step in (config.round_number_step, config.round_number_major_step):
        above = math.floor(price / step) * step + step
        below = math.ceil(price / step) * step - step
        for side, level in ((Side.BUY_SIDE, above), (Side.SELL_SIDE, below)):
            ratio = level / config.round_number_major_step
            major = abs(ratio - round(ratio)) < 1e-9
            pools[(side, round(level, 6))] = make_pool(
                PoolKind.ROUND_NUMBER,
                side,
                level,
                now,
                scope=Scope.INTERNAL,
                strength=POOL_STRENGTH[PoolKind.ROUND_NUMBER]
                if major
                else MINOR_ROUND_NUMBER_STRENGTH,
                label=f"round number {level:g}",
            )
    return list(pools.values())


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def merge_pools(pools: Iterable[Pool], tolerance: float) -> list[Pool]:
    """Combine pools of one side that sit within ``tolerance`` of each other.

    Levels that coincide (say the previous day's low and equal lows) hold more
    liquidity than either alone: the merged pool takes the strongest kind, sits
    at the outermost price, and gains a little strength per extra level.
    """
    merged: list[Pool] = []
    for side in (Side.BUY_SIDE, Side.SELL_SIDE):
        ordered = sorted((pool for pool in pools if pool.side is side), key=lambda pool: pool.price)
        group: list[Pool] = []
        for pool in [*ordered, None]:
            if pool is not None and group and pool.price - group[0].price <= tolerance:
                group.append(pool)
                continue
            if group:
                merged.append(_merge_group(group, side))
            group = [pool] if pool is not None else []
    return merged


def _merge_group(group: Sequence[Pool], side: Side) -> Pool:
    if len(group) == 1:
        return group[0]
    base = max(group, key=lambda pool: (pool.strength, pool.is_external))
    price = (
        max(pool.price for pool in group)
        if side is Side.BUY_SIDE
        else min(pool.price for pool in group)
    )
    labels = [base.label, *(pool.label for pool in group if pool is not base)]
    kinds = tuple(dict.fromkeys(kind for pool in group for kind in pool.sources))
    return replace(
        base,
        price=price,
        created=max(pool.created for pool in group),
        scope=Scope.EXTERNAL if any(pool.is_external for pool in group) else Scope.INTERNAL,
        strength=min(1.0, base.strength + 0.1 * (len(group) - 1)),
        label=" + ".join(labels),
        sources=kinds,
    )


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #


class SweepStatus(StrEnum):
    NONE = "none"  # price has not traded beyond the pool
    PENDING = "pending"  # beyond the pool; waiting to see whether it closes back inside
    FAILED = "failed"  # stayed beyond the pool: the level broke, it was not swept
    SWEPT = "swept"  # traded beyond and closed back inside in time


@dataclass(frozen=True, slots=True)
class SweepEvent:
    """A completed sweep of one pool."""

    pool: Pool
    direction: Direction  # the trade the sweep points to
    pierce_index: int
    pierce_time: dt.datetime
    reclaim_index: int
    reclaim_time: dt.datetime
    extreme: float  # the wick low (or high) of the sweep
    extreme_index: int
    depth: float  # how far beyond the pool the extreme went, in price
    bars_to_reclaim: int


@dataclass(frozen=True, slots=True)
class SweepResult:
    status: SweepStatus
    event: SweepEvent | None = None
    pierce_index: int | None = None


def detect_sweep(
    candles: pd.DataFrame,
    pool: Pool,
    *,
    start: int = 0,
    min_sweep: float,
    max_bars_back_inside: int,
) -> SweepResult:
    """Has ``pool`` been swept by the bars from ``start`` on? (spec 5.3)

    A sweep needs price to trade beyond the pool by at least ``min_sweep`` and
    then a candle to *close* back inside, on the piercing bar itself or within
    ``max_bars_back_inside`` bars after it.
    """
    high = candles["high"].to_numpy(dtype="float64")
    low = candles["low"].to_numpy(dtype="float64")
    close = candles["close"].to_numpy(dtype="float64")
    sign = pool.side.sign
    beyond = (high if sign > 0 else low) - pool.price  # positive = above the level
    pierced = np.flatnonzero(beyond[start:] * sign >= min_sweep - _EPSILON)
    if len(pierced) == 0:
        return SweepResult(SweepStatus.NONE)
    pierce = int(pierced[0]) + start
    last = len(close) - 1
    reclaim: int | None = None
    for i in range(pierce, min(pierce + max_bars_back_inside, last) + 1):
        if (close[i] - pool.price) * sign < 0:
            reclaim = i
            break
    if reclaim is None:
        waiting = pierce + max_bars_back_inside > last
        return SweepResult(
            SweepStatus.PENDING if waiting else SweepStatus.FAILED, pierce_index=pierce
        )
    leg = (high if sign > 0 else low)[pierce : reclaim + 1]
    offset = int(np.argmax(leg) if sign > 0 else np.argmin(leg))
    extreme = float(leg[offset])
    return SweepResult(
        SweepStatus.SWEPT,
        SweepEvent(
            pool=pool.as_swept(),
            direction=pool.side.trade_direction,
            pierce_index=pierce,
            pierce_time=candles.index[pierce].to_pydatetime(),
            reclaim_index=reclaim,
            reclaim_time=candles.index[reclaim].to_pydatetime(),
            extreme=extreme,
            extreme_index=pierce + offset,
            depth=abs(extreme - pool.price),
            bars_to_reclaim=reclaim - pierce,
        ),
        pierce_index=pierce,
    )

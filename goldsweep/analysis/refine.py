"""M1 refinement: "sniper" entries for an M5 setup (Phase 9 stage 2, D83).

The M5 engine finds the setup: the sweep, the shift, the gap, a stop beyond the
sweep's wick and the liquidity targets. Refinement then waits, on closed
one-minute bars, for price to come back into that gap and turn there:

1. **Touch**: an M1 bar reaches the gap's near edge (the top for a long, on the
   ask; the bottom for a short).
2. **Turn**: after the touch, an M1 close through the last M1 swing high made
   before the pullback's low (a change of character, ``choch``), or through the
   open of the last run of down-closing candles into that low (a change in the
   state of delivery, ``cisd``). Shorts mirror both.
3. **Entry**: at the close of the turning bar (``market``) or as a limit at the
   middle of the M1 gap the turn left (``fvg_mid``; market when it left none).
   The stop goes beyond the pullback's extreme by a fraction of the M1 ATR plus
   the spread, never closer than a floor of spreads and M1 ATRs. The M5 targets
   stay, so the reward-to-risk grows; the M5 stop is never exceeded.

Nothing is traded when the setup's entry window ends first, when price reaches TP1
before it touches the gap, or when it trades through the M5 stop (the setup failed).

:func:`refine` is a pure function of the setup and the closed M1 bars: called
again with more bars it gives the same answer for the same past, so a live run,
a restart and a backtest agree. It never sees a forming bar; a swing counts only
once its confirming bars have closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from goldsweep.analysis.indicators import atr_values
from goldsweep.analysis.structure import Direction, SwingKind, find_swings
from goldsweep.analysis.zones import find_fvgs
from goldsweep.core.clock import ensure_utc

if TYPE_CHECKING:
    from goldsweep.analysis.signals import Signal
    from goldsweep.core.settings import AtrMethod, RefinementConfig

#: M1 bars before the setup that the caller should include, so swings and the ATR
#: have context (the pullback often starts before the M5 bar closes).
CONTEXT_BARS = 90
M1 = dt.timedelta(minutes=1)
_EPSILON = 1e-9


class RefineStatus(StrEnum):
    WAITING = "waiting"  # no decision yet: keep watching
    READY = "ready"  # the turn came: trade the refined entry
    EXPIRED = "expired"  # the setup's entry window ended first
    TP1_FIRST = "tp1_first"  # price reached TP1 before touching the gap
    FAILED = "failed"  # price traded through the M5 stop


@dataclass(frozen=True, slots=True)
class RefineSetup:
    """What refinement needs from an M5 signal; small enough to save between restarts."""

    id: str
    time: dt.datetime  # close of the M5 bar that completed the setup (UTC)
    direction: Direction
    session: str | None
    zone_top: float
    zone_bottom: float
    stop_loss: float  # the M5 stop
    take_profit_1: float
    take_profit_2: float | None
    expires_at: dt.datetime  # end of the entry window
    score: float
    spread: float  # in price, when the setup formed

    @property
    def near(self) -> float:
        """The gap's edge a pullback reaches first."""
        return self.zone_top if self.direction is Direction.LONG else self.zone_bottom

    @classmethod
    def from_signal(cls, signal: Signal, *, expires_at: dt.datetime | None = None) -> RefineSetup:
        """The setup of an M5 signal. Without a zone (a market-mode signal) the gap is
        the entry itself."""
        zone = signal.zone
        top = zone.top if zone is not None else signal.entry
        bottom = zone.bottom if zone is not None else signal.entry
        return cls(
            id=signal.id,
            time=signal.time,
            direction=signal.direction,
            session=signal.session,
            zone_top=top,
            zone_bottom=bottom,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            expires_at=expires_at or signal.expires_at,
            score=signal.score,
            spread=signal.spread,
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "time": self.time.isoformat(),
            "direction": self.direction.value,
            "session": self.session,
            "zone_top": self.zone_top,
            "zone_bottom": self.zone_bottom,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "expires_at": self.expires_at.isoformat(),
            "score": self.score,
            "spread": self.spread,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> RefineSetup:
        values = dict(data)
        values["time"] = dt.datetime.fromisoformat(values["time"])
        values["expires_at"] = dt.datetime.fromisoformat(values["expires_at"])
        values["direction"] = Direction(values["direction"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RefineResult:
    """Where a setup stands, and the refined order once it is READY."""

    status: RefineStatus
    detail: str = ""
    time: dt.datetime | None = None  # close of the bar that decided it
    entry_mode: str = "market"  # "market" or "fvg_mid"
    entry: float | None = None
    stop_loss: float | None = None
    order_expires_at: dt.datetime | None = None  # for a limit entry

    @property
    def done(self) -> bool:
        return self.status is not RefineStatus.WAITING


@dataclass(frozen=True)
class M1Refinement:
    """Refinement over a stored M1 history (the backtester's view)."""

    config: RefinementConfig
    bars: pd.DataFrame  # closed M1 bars, the candle-frame format
    atr_period: int = 14
    atr_method: AtrMethod = "sma"
    min_rr: float = 2.0

    def decide(self, setup: RefineSetup) -> RefineResult:
        """Run the setup over its own window of the history, to its end."""
        start = pd.Timestamp(ensure_utc(setup.time)) - CONTEXT_BARS * M1
        end = pd.Timestamp(ensure_utc(setup.expires_at))
        index = self.bars.index
        window = self.bars.iloc[index.searchsorted(start) : index.searchsorted(end, side="right")]
        result = refine(
            setup,
            window,
            self.config,
            atr_period=self.atr_period,
            atr_method=self.atr_method,
            min_rr=self.min_rr,
        )
        if result.status is RefineStatus.WAITING:
            # The window ran out of bars before the setup ended: nothing traded.
            return RefineResult(RefineStatus.EXPIRED, "no M1 turn inside the gap in time")
        return result


def _close_time(index: pd.DatetimeIndex, i: int) -> dt.datetime:
    return index[i].to_pydatetime() + M1


def refine(
    setup: RefineSetup,
    bars: pd.DataFrame,
    config: RefinementConfig,
    *,
    atr_period: int = 14,
    atr_method: AtrMethod = "sma",
    min_rr: float = 2.0,
) -> RefineResult:
    """Decide on ``setup`` from closed M1 ``bars`` (include :data:`CONTEXT_BARS` before it)."""
    if bars.empty:
        return RefineResult(RefineStatus.WAITING, "no one-minute bars yet")
    index = bars.index
    open_ = bars["open"].to_numpy(dtype="float64")
    high = bars["high"].to_numpy(dtype="float64")
    low = bars["low"].to_numpy(dtype="float64")
    close = bars["close"].to_numpy(dtype="float64")
    atr = atr_values(high, low, close, atr_period, atr_method)
    long = setup.direction is Direction.LONG
    sign = setup.direction.sign
    spread = setup.spread
    start = int(index.searchsorted(pd.Timestamp(ensure_utc(setup.time))))
    expires = ensure_utc(setup.expires_at)
    swings = find_swings(bars, config.swing_lookback)
    touched: int | None = None
    extreme_at = -1

    for i in range(start, len(bars)):
        opened = index[i].to_pydatetime()
        if opened >= expires:
            return RefineResult(RefineStatus.EXPIRED, "no M1 turn inside the gap in time", opened)
        bid_low, ask_high = low[i], high[i] + spread
        ask_low, bid_high = low[i] + spread, high[i]
        if touched is None:
            reached_tp1 = (
                bid_high >= setup.take_profit_1 - _EPSILON
                if long
                else ask_low <= setup.take_profit_1 + _EPSILON
            )
            if reached_tp1:
                return RefineResult(
                    RefineStatus.TP1_FIRST,
                    "price reached TP1 before coming back to the gap",
                    _close_time(index, i),
                )
            if (ask_low <= setup.near + _EPSILON) if long else (bid_high >= setup.near - _EPSILON):
                touched, extreme_at = i, i
            else:
                continue
        # Inside the pullback: follow its extreme; through the M5 stop the setup failed.
        through_stop = (
            bid_low <= setup.stop_loss + _EPSILON
            if long
            else ask_high >= setup.stop_loss - _EPSILON
        )
        if through_stop:
            return RefineResult(
                RefineStatus.FAILED, "price traded through the M5 stop", _close_time(index, i)
            )
        if (low[i] < low[extreme_at]) if long else (high[i] > high[extreme_at]):
            extreme_at = i
        if i == extreme_at:
            continue  # the turn must close after the pullback's extreme
        level = _turn_level(config.confirm, setup.direction, extreme_at, swings, open_, close, i)
        if level is None or (close[i] - level) * sign <= _EPSILON:
            continue
        return _entry(setup, bars, atr, i, extreme_at, config, min_rr)

    return RefineResult(
        RefineStatus.WAITING,
        "waiting for price to come back to the gap" if touched is None else "waiting for the turn",
    )


def _turn_level(
    confirm: str,
    direction: Direction,
    extreme_at: int,
    swings: list[Any],
    open_: np.ndarray,
    close: np.ndarray,
    now: int,
) -> float | None:
    """The price a close must clear to confirm the turn, as known at bar ``now``."""
    long = direction is Direction.LONG
    if confirm == "cisd":
        # The last run of candles closing against the trade, ending at the extreme.
        against = (close < open_) if long else (close > open_)
        end = extreme_at
        while end >= 0 and not against[end]:
            end -= 1
        if end < 0:
            return None
        first = end
        while first > 0 and against[first - 1]:
            first -= 1
        return float(open_[first])
    wanted = SwingKind.HIGH if long else SwingKind.LOW
    levels = [
        swing.price
        for swing in swings
        if swing.kind is wanted and swing.index < extreme_at and swing.confirmed_index <= now
    ]
    return levels[-1] if levels else None


def _entry(
    setup: RefineSetup,
    bars: pd.DataFrame,
    atr: np.ndarray,
    i: int,
    extreme_at: int,
    config: RefinementConfig,
    min_rr: float,
) -> RefineResult:
    """The refined order at the close of the turning bar ``i``."""
    long = setup.direction is Direction.LONG
    sign = setup.direction.sign
    spread = setup.spread
    when = _close_time(bars.index, i)
    close = float(bars["close"].iloc[i])
    unit = float(atr[i]) if np.isfinite(atr[i]) and atr[i] > 0 else 0.0
    mode, entry = "market", close + spread if long else close
    order_expires: dt.datetime | None = None
    if config.entry == "fvg_mid":
        gaps = find_fvgs(bars, atr, setup.direction, first=extreme_at + 1, last=i - 1)
        usable = [g for g in gaps if (entry - g.mid) * sign > _EPSILON]
        if usable:
            mode, entry = "fvg_mid", usable[-1].mid
            order_expires = min(ensure_utc(setup.expires_at), when + M1 * config.entry_wait_bars)
    pullback = float(bars["low"].iloc[extreme_at] if long else bars["high"].iloc[extreme_at])
    stop = pullback - sign * (config.sl_buffer_atr * unit + spread)
    floor = max(config.min_stop_spreads * spread, config.min_stop_atr * unit)
    if (entry - stop) * sign < floor:
        stop = entry - sign * floor
    if (stop - setup.stop_loss) * sign < 0:
        stop = setup.stop_loss  # never wider than the M5 setup's own stop
    risk = (entry - stop) * sign
    reward = (setup.take_profit_1 - entry) * sign
    if risk <= _EPSILON or reward <= _EPSILON:
        return RefineResult(RefineStatus.FAILED, "the turn came beyond TP1", when)
    if reward / risk < min_rr - _EPSILON:
        return RefineResult(
            RefineStatus.FAILED, f"refined R:R {reward / risk:.2f} is below {min_rr:g}", when
        )
    return RefineResult(
        RefineStatus.READY,
        f"M1 turn at {when:%H:%M} UTC",
        when,
        mode,
        entry,
        stop,
        order_expires,
    )

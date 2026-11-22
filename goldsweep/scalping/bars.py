"""Higher timeframes built from the M1 feed, one closed bar at a time (S6, D112).

The scalper's modes are all fed closed M1 bars (live by the runner, in backtests by a
replay of the stored M1 history), so each mode builds its own signal timeframe (M5,
M15) and the H1 trend from them. Live and backtest therefore see the same bars.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass

from goldsweep.analysis.structure import Direction


@dataclass(frozen=True, slots=True)
class Candle:
    """A finished bar of a higher timeframe."""

    opened: dt.datetime  # UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    minutes: int

    @property
    def closed(self) -> dt.datetime:
        return self.opened + dt.timedelta(minutes=self.minutes)


class BarAggregator:
    """Closed M1 bars in, closed ``minutes`` bars out (``minutes`` divides 60).

    A bar is finished by its last minute, or, when that minute had no trades, by the
    first M1 bar of a later bucket (then it comes out one minute late).
    """

    def __init__(self, minutes: int) -> None:
        if minutes <= 0 or 60 % minutes:
            raise ValueError("minutes must divide 60")
        self.minutes = minutes
        self._bucket: dt.datetime | None = None
        self._values: list[float] = []  # open, high, low, close, volume

    def _start(self, opened: dt.datetime) -> dt.datetime:
        return opened.replace(
            minute=opened.minute - opened.minute % self.minutes, second=0, microsecond=0
        )

    def _emit(self) -> Candle:
        assert self._bucket is not None
        o, h, low, c, v = self._values
        candle = Candle(self._bucket, o, h, low, c, v, self.minutes)
        self._bucket, self._values = None, []
        return candle

    def add(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[Candle]:
        """Add one closed M1 bar (``opened`` is its open time); returns finished bars."""
        bucket = self._start(opened)
        finished: list[Candle] = []
        if self._bucket is not None and bucket != self._bucket:
            finished.append(self._emit())
        if self._bucket is None:
            self._bucket = bucket
            self._values = [open_, high, low, close, volume]
        else:
            values = self._values
            values[1] = max(values[1], high)
            values[2] = min(values[2], low)
            values[3] = close
            values[4] += volume
        if opened >= bucket + dt.timedelta(minutes=self.minutes - 1):
            finished.append(self._emit())
        return finished


class HourlyTrend:
    """The H1 trend (D112): the last H1 close beyond an EMA that moved that way.

    LONG when the close is above an ``ema_hours`` EMA that is higher than it was
    ``slope_hours`` hours ago, SHORT mirrored, otherwise ``None`` (flat or not ready).
    """

    def __init__(self, ema_hours: int, slope_hours: int) -> None:
        self._hours = BarAggregator(60)
        self._period = ema_hours
        self._alpha = 2.0 / (ema_hours + 1.0)
        self._ema = math.nan
        self._count = 0
        self._emas: deque[float] = deque(maxlen=slope_hours + 1)
        self._close = math.nan

    def add(self, opened: dt.datetime, open_: float, high: float, low: float, close: float) -> None:
        """Add one closed M1 bar."""
        for candle in self._hours.add(opened, open_, high, low, close):
            self._count += 1
            if self._count == 1:
                self._ema = candle.close
            else:
                self._ema += self._alpha * (candle.close - self._ema)
            self._emas.append(self._ema)
            self._close = candle.close

    @property
    def ema(self) -> float:
        return self._ema if self._count >= self._period else math.nan

    @property
    def direction(self) -> Direction | None:
        if self._count < self._period or len(self._emas) < self._emas.maxlen:  # type: ignore[operator]
            return None
        now, before = self._emas[-1], self._emas[0]
        if self._close > now and now > before:
            return Direction.LONG
        if self._close < now and now < before:
            return Direction.SHORT
        return None

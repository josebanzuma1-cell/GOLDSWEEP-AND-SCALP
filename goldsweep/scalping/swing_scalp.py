"""The swing-scalping mode's entries (S6, D112): closed M15 bars, with the H1 trend.

Fed one finished M15 bar at a time (built from the M1 feed, :mod:`~goldsweep.scalping.bars`)
together with the H1 trend at that moment; a setup needs the trend (LONG or SHORT).
For a LONG (a SHORT mirrors it):

``ema_pullback``
    a bar's low reached the M15 ``ema_bars`` EMA within the last ``pullback_bars``
    bars (this one included); this bar is bullish, closes above the EMA and above the
    previous bar's high. The stop goes beyond the lowest low of those bars. Each touch
    gives at most one setup.

``range_breakout``
    once the day's Asian (Tokyo) or London window has ended, the first close above
    its range by ``breakout_buffer_atr`` x ATR (the previous close was not above it);
    the stop goes beyond the range's middle. One per range and direction a day.

Positions trail the stop behind the lowest low (highest high) of the last
``channel_bars`` M15 bars (:meth:`SwingScalpEngine.line_for`).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass, field

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ASIA, LONDON, SessionCalculator
from goldsweep.core.settings import ScalperConfig, SwingScalpConfig
from goldsweep.scalping.bars import Candle
from goldsweep.scalping.trail import RollingAtr

EMA_PULLBACK, RANGE_BREAKOUT = "ema_pullback", "range_breakout"
RULES = (EMA_PULLBACK, RANGE_BREAKOUT)
SIGNAL_MINUTES = 15
#: A range needs at least this share of its window's bars (a gap is no range).
_MIN_RANGE_COVER = 0.5


@dataclass(frozen=True, slots=True)
class SwingScalpSetup:
    """A swing-scalping entry found on a closed M15 bar."""

    time: dt.datetime  # the bar's close (UTC): the order goes in from here
    direction: Direction
    rule: str
    reference: float  # the bar's close: the planned (market) entry
    anchor: float  # what the stop goes beyond
    atr: float  # the M15 ATR
    detail: str
    #: The level the setup broke, which a retest comes back to (S11): the EMA, the
    #: range edge.
    level: float | None = None


@dataclass
class _Range:
    high: float = -math.inf
    low: float = math.inf
    bars: int = 0

    def add(self, high: float, low: float) -> None:
        self.high, self.low, self.bars = max(self.high, high), min(self.low, low), self.bars + 1

    @property
    def middle(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class _Day:
    ranges: dict[str, _Range] = field(default_factory=lambda: {ASIA: _Range(), LONDON: _Range()})
    done: set[str] = field(default_factory=set)


class SwingScalpEngine:
    """M15 bars in, swing-scalping setups out; also the trail's channel line."""

    def __init__(
        self,
        config: ScalperConfig,
        sessions: SessionCalculator,
        *,
        rules: tuple[str, ...] | None = None,
    ) -> None:
        self._cfg: SwingScalpConfig = config.swing_scalp
        self._sessions = sessions
        self._rules = rules if rules is not None else self._cfg.rules
        unknown = set(self._rules) - set(RULES)
        if unknown:
            raise ValueError(f"unknown swing-scalping rules: {sorted(unknown)}")
        cfg = self._cfg
        self._atr = RollingAtr(config.atr_period)
        self._alpha = 2.0 / (cfg.ema_bars + 1.0)
        self._ema = math.nan
        self._count = 0
        self._lows: deque[float] = deque(maxlen=cfg.channel_bars)
        self._highs: deque[float] = deque(maxlen=cfg.channel_bars)
        self._touch_low: int | None = None
        self._touch_high: int | None = None
        self._previous: tuple[float, float, float] | None = None  # high, low, close
        self._index = -1
        self._day: dt.date | None = None
        self._today = _Day()
        self._spans: dict[tuple[str, dt.date], tuple[dt.datetime, dt.datetime]] = {}
        self.atr: float | None = None

    @property
    def ema(self) -> float:
        return self._ema if self._count >= self._cfg.ema_bars else math.nan

    # -- the trail ----------------------------------------------------------------- #

    def line_for(self, close_ns: int, long: bool) -> tuple[float, float]:
        """The channel the stop trails: the lowest low (long) or highest high (short) of
        the last ``channel_bars`` finished M15 bars, and the ATR; NaN until ready."""
        if len(self._lows) < self._cfg.channel_bars or self.atr is None:
            return math.nan, math.nan
        return (min(self._lows) if long else max(self._highs)), self.atr

    def line_at(self, close_ns: int) -> tuple[float, float]:
        return self.line_for(close_ns, True)

    def ranges(self) -> list[tuple[str, dt.datetime, dt.datetime, float, float]]:
        """Today's Asian and London ranges so far, for the chart:
        (name, window start, window end, high, low)."""
        if self._day is None:
            return []
        found: list[tuple[str, dt.datetime, dt.datetime, float, float]] = []
        for name, label in ((ASIA, "Asian range"), (LONDON, "London range")):
            box = self._today.ranges[name]
            if box.bars:
                start, end = self._span(name, self._day)
                found.append((label, start, end, box.high, box.low))
        return found

    # -- each bar ---------------------------------------------------------------------- #

    def _span(self, name: str, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
        key = (name, day)
        if key not in self._spans:
            span = self._sessions.span(name, day)
            self._spans[key] = (span.start, span.end)
            if len(self._spans) > 32:
                self._spans.pop(next(iter(self._spans)))
        return self._spans[key]

    def on_bar(self, candle: Candle, trend: Direction | None) -> list[SwingScalpSetup]:
        """Apply one finished M15 bar, with the H1 trend at its close."""
        self._index += 1
        day = candle.opened.date()
        if day != self._day:
            self._day, self._today = day, _Day()
        high, low, close = candle.high, candle.low, candle.close
        atr = self._atr.update(high, low, close)
        self.atr = atr
        before = self.ema  # the EMA price came back to
        self._count += 1
        self._ema = close if self._count == 1 else self._ema + self._alpha * (close - self._ema)
        if not math.isnan(before):
            if low <= before:
                self._touch_low = self._index
            if high >= before:
                self._touch_high = self._index
        found: list[SwingScalpSetup] = []
        if atr is not None and trend is not None and not math.isnan(self.ema):
            if EMA_PULLBACK in self._rules:
                found += self._pullback(candle, trend, atr)
            if RANGE_BREAKOUT in self._rules:
                found += self._breakout(candle, trend, atr)
        self._track_ranges(candle)
        self._lows.append(low)
        self._highs.append(high)
        self._previous = (high, low, close)
        return found

    def _track_ranges(self, candle: Candle) -> None:
        for name in (ASIA, LONDON):
            start, end = self._span(name, candle.opened.date())
            if start <= candle.opened < end:
                self._today.ranges[name].add(candle.high, candle.low)

    # -- the rules ------------------------------------------------------------------- #

    def _pullback(self, candle: Candle, trend: Direction, atr: float) -> list[SwingScalpSetup]:
        if self._previous is None:
            return []
        previous_high, previous_low, _ = self._previous
        ema, cfg = self.ema, self._cfg
        earlier = max(0, len(self._lows) - (cfg.pullback_bars - 1))  # the window's other bars
        lows = [*list(self._lows)[earlier:], candle.low]
        highs = [*list(self._highs)[earlier:], candle.high]
        if trend is Direction.LONG:
            touch = self._touch_low
            if touch is None or self._index - touch >= cfg.pullback_bars:
                return []
            if not (
                candle.close > ema and candle.close > candle.open and candle.close > previous_high
            ):
                return []
            self._touch_low = None  # one setup per touch
            anchor = min(lows)
            detail = f"pulled back to the M15 EMA {ema:.2f} and closed above it, H1 trend up"
        else:
            touch = self._touch_high
            if touch is None or self._index - touch >= cfg.pullback_bars:
                return []
            if not (
                candle.close < ema and candle.close < candle.open and candle.close < previous_low
            ):
                return []
            self._touch_high = None
            anchor = max(highs)
            detail = f"pulled back to the M15 EMA {ema:.2f} and closed below it, H1 trend down"
        return [
            SwingScalpSetup(
                candle.closed, trend, EMA_PULLBACK, candle.close, anchor, atr, detail, ema
            )
        ]

    def _breakout(self, candle: Candle, trend: Direction, atr: float) -> list[SwingScalpSetup]:
        if self._previous is None:
            return []
        previous_close = self._previous[2]
        buffer = self._cfg.breakout_buffer_atr * atr
        found: list[SwingScalpSetup] = []
        for name in (ASIA, LONDON):
            key = f"{name}:{trend.value}"
            start, end = self._span(name, candle.opened.date())
            box = self._today.ranges[name]
            expected = (end - start) / dt.timedelta(minutes=SIGNAL_MINUTES)
            if (
                key in self._today.done
                or candle.opened < end
                or box.bars < _MIN_RANGE_COVER * expected
            ):
                continue
            if trend is Direction.LONG:
                level = box.high + buffer
                if not (candle.close > level >= previous_close):
                    continue
                side = "above"
            else:
                level = box.low - buffer
                if not (candle.close < level <= previous_close):
                    continue
                side = "below"
            self._today.done.add(key)
            label = "Asian" if name == ASIA else "London"
            found.append(
                SwingScalpSetup(
                    candle.closed,
                    trend,
                    RANGE_BREAKOUT,
                    candle.close,
                    box.middle,
                    atr,
                    f"closed {side} the {label} range {box.low:.2f}-{box.high:.2f} with the "
                    f"H1 trend; stop beyond its middle {box.middle:.2f}",
                    box.high if trend is Direction.LONG else box.low,
                )
            )
            break  # one setup a bar
        return found

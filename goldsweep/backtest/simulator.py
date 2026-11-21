"""Trade simulation: the backtest's walk through the bars.

The trading rules themselves (fills, stops, targets, management, costs and the
risk limits) live in :mod:`goldsweep.execution.book`, which paper trading uses
too. The simulator feeds that book one bar at a time, exactly as the live bot
would see them:

1. pending entry orders are filled, expire, or are cancelled because price
   reached TP1 first (spec 5.5);
2. open positions are checked against their stop-loss and take-profits;
3. signals that completed at this bar's close are risk-checked, sized and turned
   into entry orders, which can fill from the *next* bar on.

Prices
------
Bars are **bid** prices, as MetaTrader 5 charts them; the ask is the bid plus
the spread. A long is bought at the ask and sold at the bid; a short is sold at
the bid and bought back at the ask. So a buy limit fills when the ask (bar low +
spread) reaches it, and a short's stop triggers when the ask reaches it.

When the order of events inside a bar is unknown, the worse case is assumed:

* a stop and a target touched in the same bar: the stop was hit first;
* the bar that fills an entry can stop it out but cannot reach its targets;
* a breakeven or trailing move takes effect from the next bar;
* a limit order fills at its own price, never at a better gap price;
* stop exits (and market entries) suffer ``slippage_points`` against the trade;
  a gap through the stop exits at the open, not at the stop.

One-minute bars, where stored, settle all of this minute by minute instead of
every five minutes (``backtest.use_m1_for_fills``).

Management (spec sections 6 and 7b): the position is split over TP1, TP2 and
TP3 (``partial_close_at_tp1_pct`` / ``partial_close_at_tp2_pct`` / the rest), the
stop moves to breakeven at +``breakeven_at_r`` R or at TP1 and then trails behind
confirmed M5 swing points (optional). As a high-impact blackout starts, a trade
``breakeven_before_news_r`` R up moves its stop to breakeven.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import heapq
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from goldsweep.analysis.refine import M1Refinement, RefineSetup, RefineStatus
from goldsweep.analysis.structure import swing_indices
from goldsweep.core.clock import SessionCalculator, ensure_utc
from goldsweep.core.types import Timeframe
from goldsweep.execution.book import (
    Bar,
    BookEvent,
    EventKind,
    ExitReason,
    Leg,
    Outcome,
    Trade,
    TradeBook,
    TradePlan,
    refined_plan,
    to_ns,
)
from goldsweep.risk.risk_manager import HIGH_IMPACT, NewsEvent

if TYPE_CHECKING:
    from goldsweep.analysis.pressure import PressureReading
    from goldsweep.analysis.signals import Signal
    from goldsweep.core.settings import (
        BacktestConfig,
        DayBoundaryConfig,
        NewsConfig,
        RiskConfig,
    )
    from goldsweep.data.broker import SymbolSpec

#: Names other modules import from here (they now live in the trade book).
__all__ = [
    "ExitReason",
    "Leg",
    "Outcome",
    "PriceSeries",
    "SignalOutcome",
    "SignalRecord",
    "SimulationResult",
    "Simulator",
    "Trade",
    "TrailLevels",
    "combine_series",
]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """A signal as the backtester saw it, with the market conditions of that moment."""

    signal: Signal
    spread_points: float  # spread used for the signal (after the cost model)
    median_spread_points: float | None  # the usual spread at that time
    pressure: PressureReading | None = None

    @property
    def time(self) -> dt.datetime:
        return self.signal.time


@dataclass(frozen=True)
class PriceSeries:
    """Bid bars as arrays, plus the spread to apply on each, in price units."""

    open_ns: np.ndarray  # bar open times, int64 nanoseconds since the epoch (UTC)
    close_ns: np.ndarray  # bar close times
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    spread: np.ndarray  # price units

    def __len__(self) -> int:
        return len(self.open)

    @classmethod
    def from_candles(
        cls,
        candles: pd.DataFrame,
        timeframe: Timeframe,
        *,
        point: float,
        spread_points: np.ndarray | float | None = None,
    ) -> PriceSeries:
        """Arrays from a candle frame; ``spread_points`` overrides the bars' own spread."""
        open_ns = candles.index.as_unit("ns").asi8.astype("int64")
        if spread_points is None:
            points = candles["spread"].to_numpy(dtype="float64")
        else:
            points = np.broadcast_to(np.asarray(spread_points, dtype="float64"), len(candles))
        return cls(
            open_ns=open_ns,
            close_ns=open_ns + int(timeframe.duration.total_seconds() * 1e9),
            open=candles["open"].to_numpy(dtype="float64"),
            high=candles["high"].to_numpy(dtype="float64"),
            low=candles["low"].to_numpy(dtype="float64"),
            close=candles["close"].to_numpy(dtype="float64"),
            spread=np.array(points, dtype="float64") * point,
        )


def combine_series(coarse: PriceSeries, fine: PriceSeries) -> PriceSeries:
    """Fine (M1) bars where they exist, coarse (M5) bars for the gaps between them.

    A coarse bar is replaced when any fine bar falls inside it.
    """
    if len(fine) == 0:
        return coarse
    covered = np.zeros(len(coarse), dtype=bool)
    slot = np.searchsorted(coarse.open_ns, fine.open_ns, side="right") - 1
    inside = (slot >= 0) & (fine.open_ns < coarse.close_ns[np.clip(slot, 0, None)])
    covered[slot[inside]] = True
    keep = ~covered
    parts = [
        np.concatenate((getattr(coarse, name)[keep], getattr(fine, name)))
        for name in ("open_ns", "close_ns", "open", "high", "low", "close", "spread")
    ]
    order = np.argsort(parts[0], kind="stable")
    return PriceSeries(*(part[order] for part in parts))


@dataclass(frozen=True)
class TrailLevels:
    """The latest confirmed M5 swing low and high at every M5 bar close."""

    close_ns: np.ndarray
    swing_low: np.ndarray  # NaN until the first swing low is confirmed
    swing_high: np.ndarray

    @classmethod
    def from_candles(
        cls, candles: pd.DataFrame, timeframe: Timeframe, lookback: int
    ) -> TrailLevels:
        high = candles["high"].to_numpy(dtype="float64")
        low = candles["low"].to_numpy(dtype="float64")
        highs, lows = swing_indices(high, low, lookback)
        swing_high = np.full(len(candles), np.nan)
        swing_low = np.full(len(candles), np.nan)
        swing_high[highs + lookback] = high[highs]  # known once confirmed
        swing_low[lows + lookback] = low[lows]
        open_ns = candles.index.as_unit("ns").asi8.astype("int64")
        return cls(
            close_ns=open_ns + int(timeframe.duration.total_seconds() * 1e9),
            swing_low=pd.Series(swing_low).ffill().to_numpy(),
            swing_high=pd.Series(swing_high).ffill().to_numpy(),
        )

    def at(self, close_ns: int) -> tuple[float, float]:
        """(swing low, swing high) known at the instant ``close_ns``."""
        position = int(np.searchsorted(self.close_ns, close_ns, side="right")) - 1
        if position < 0:
            return math.nan, math.nan
        return float(self.swing_low[position]), float(self.swing_high[position])


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    """What happened to one signal."""

    record: SignalRecord
    outcome: Outcome
    reason: str = ""


@dataclass
class SimulationResult:
    trades: list[Trade] = field(default_factory=list)
    outcomes: list[SignalOutcome] = field(default_factory=list)
    equity: list[tuple[dt.datetime, float]] = field(default_factory=list)
    final_equity: float = 0.0


# --------------------------------------------------------------------------- #
# The walk through the bars
# --------------------------------------------------------------------------- #

_OUTCOME_EVENTS = (EventKind.FILLED, EventKind.EXPIRED, EventKind.TP1_FIRST, EventKind.CANCELLED)


class Simulator:
    """Plays signals against prices under the risk rules. One instance per run.

    The trading itself (fills, stops, targets, management, costs, risk rules) is
    :class:`~goldsweep.execution.book.TradeBook`, the same code paper trading uses.
    """

    def __init__(
        self,
        series: PriceSeries,
        spec: SymbolSpec,
        risk: RiskConfig,
        news: NewsConfig,
        backtest: BacktestConfig,
        boundary: DayBoundaryConfig,
        *,
        trail: TrailLevels | None = None,
        events: Sequence[NewsEvent] = (),
        sessions: SessionCalculator | None = None,
        entry_lifetime: dt.timedelta | None = None,
        refinement: M1Refinement | None = None,
    ) -> None:
        """``sessions``: when given, a waiting entry is withdrawn before the next news
        blackout, rollover block, Friday cutoff or weekend (``entry_deadline``), as in
        paper and live trading.

        ``entry_lifetime``: how long a limit entry waits, measured from the signal
        (``entry_expiry_bars`` x the trigger bar). ``None`` keeps each signal's own
        ``expires_at``; cached signals may have been made under another setting.

        ``refinement``: when given, a selected setup is not traded as it is: the
        order is the M1-refined one, placed when the turn inside the gap closed
        (D83); a setup whose turn never comes is not traded (``no_trigger``)."""
        self._refinement = refinement
        self._series = series
        self._spec = spec
        self._risk_cfg = risk
        self._news = news
        self._cfg = backtest
        self._boundary = boundary
        self._trail = trail if backtest.trail_after_tp1 else None
        self._events = tuple(events)
        self._sessions = sessions
        self._lifetime = entry_lifetime
        # Spec 5.6: where each high-impact blackout starts, open trades that are
        # far enough in profit move their stop to breakeven.
        before = dt.timedelta(minutes=news.blackout_before_min)
        self._blackout_starts = np.array(
            sorted(to_ns(e.time - before) for e in self._events if e.impact == HIGH_IMPACT),
            dtype=np.int64,
        )

    def _bar(self, i: int) -> Bar:
        s = self._series
        return Bar(
            int(s.open_ns[i]),
            int(s.close_ns[i]),
            float(s.open[i]),
            float(s.high[i]),
            float(s.low[i]),
            float(s.close[i]),
            float(s.spread[i]),
        )

    def run(
        self,
        records: Sequence[SignalRecord],
        *,
        select: Callable[[SignalRecord], str | None],
        start: dt.datetime,
        end: dt.datetime,
        lifetime: Callable[[SignalRecord], dt.timedelta | None] | None = None,
    ) -> SimulationResult:
        """Trade the signals in ``[start, end)`` that ``select`` accepts.

        ``select`` returns ``None`` to trade a signal, or the reason it is passed over.
        ``lifetime`` overrides the entry lifetime per signal (the walk-forward's trials).
        Trades opened in the window are followed to their exit even past ``end``.
        """
        costs = self._cfg.costs
        book = TradeBook(
            self._spec,
            self._risk_cfg,
            self._news,
            self._boundary,
            equity=self._cfg.initial_balance,
            slippage_points=costs.slippage_points,
            commission_per_lot=costs.commission_per_lot,
            trail_after_tp1=self._cfg.trail_after_tp1,
        )
        result = SimulationResult(equity=[(ensure_utc(start), book.manager.equity)])
        queue = sorted(
            (r for r in records if ensure_utc(start) <= r.time < ensure_utc(end)),
            key=lambda r: r.time,
        )
        series = self._series
        if not queue or len(series) == 0:
            result.final_equity = book.manager.equity
            return result

        next_signal = 0
        # M1 refinement (D83): orders whose turn has been found, placed when it closed.
        refined: list[tuple[int, int, SignalRecord, TradePlan]] = []
        i = int(np.searchsorted(series.close_ns, to_ns(queue[0].time), side="left"))
        while i < len(series):
            if book.idle:
                upcoming = [to_ns(queue[next_signal].time)] if next_signal < len(queue) else []
                if refined:
                    upcoming.append(refined[0][0])
                if not upcoming:
                    break
                # Nothing to manage: jump to the bar at whose close the next one arrives.
                i = max(i, int(np.searchsorted(series.close_ns, min(upcoming))))
                if i >= len(series):
                    break
            bar = self._bar(i)
            self._collect(book.on_bar(bar, self._trail), result)
            self._protect_before_news(book, bar)
            close_ns = int(series.close_ns[i])
            while next_signal < len(queue) and to_ns(queue[next_signal].time) <= close_ns:
                record = queue[next_signal]
                next_signal += 1
                passed_over = select(record)
                if passed_over is not None:
                    result.outcomes.append(SignalOutcome(record, Outcome.NOT_SELECTED, passed_over))
                    continue
                plan = self._plan(record, lifetime)
                if self._refinement is None:
                    self._submit(book, record, plan, result)
                    continue
                setup = RefineSetup.from_signal(record.signal, expires_at=plan.expires_at)
                decision = self._refinement.decide(setup)
                if decision.status is not RefineStatus.READY:
                    outcome = (
                        Outcome.TP1_FIRST
                        if decision.status is RefineStatus.TP1_FIRST
                        else (Outcome.NO_TRIGGER)
                    )
                    result.outcomes.append(SignalOutcome(record, outcome, decision.detail))
                    continue
                order = self._with_deadline(
                    refined_plan(setup, decision, tp3_r=self._risk_cfg.tp3_r).with_exit_targets(
                        self._risk_cfg
                    )
                )
                heapq.heappush(refined, (to_ns(order.time), next_signal, record, order))
            while refined and refined[0][0] <= close_ns:
                _when, _n, record, order = heapq.heappop(refined)
                self._submit(book, record, order, result)
            i += 1
        for _when, _n, record, _order in refined:
            result.outcomes.append(
                SignalOutcome(record, Outcome.EXPIRED, "the data ended before the order")
            )

        last = self._bar(len(series) - 1)
        self._collect(
            book.close_everything(
                last.close_time,
                last.close,
                last.close + last.spread,
                ExitReason.END_OF_DATA,
                "the data ended before a fill",
                order_outcome=Outcome.EXPIRED,
            ),
            result,
        )
        result.trades.sort(key=lambda trade: trade.fill_time)
        result.final_equity = book.manager.equity
        return result

    def _with_deadline(self, plan: TradePlan) -> TradePlan:
        if self._sessions is None:
            return plan
        return plan.with_deadline(
            sessions=self._sessions,
            events=self._events,
            blackout_before_min=self._news.blackout_before_min,
        )

    def _plan(
        self,
        record: SignalRecord,
        lifetime: Callable[[SignalRecord], dt.timedelta | None] | None,
    ) -> TradePlan:
        """The signal's order: its configured targets and lifetime, cut before any
        blocked time."""
        plan = TradePlan.from_signal(record.signal, tp3_r=self._risk_cfg.tp3_r).with_exit_targets(
            self._risk_cfg
        )
        span = lifetime(record) if lifetime is not None else self._lifetime
        if span is not None and plan.entry_mode != "market":
            plan = dataclasses.replace(plan, expires_at=plan.time + span)
        return self._with_deadline(plan)

    def _submit(
        self, book: TradeBook, record: SignalRecord, plan: TradePlan, result: SimulationResult
    ) -> None:
        event = book.submit(
            plan,
            record,
            spread_points=record.spread_points,
            median_spread_points=record.median_spread_points,
            events=self._events,
        )
        if event.kind is EventKind.REJECTED and event.outcome is not None:
            result.outcomes.append(SignalOutcome(record, event.outcome, event.detail))

    def _protect_before_news(self, book: TradeBook, bar: Bar) -> None:
        min_r = self._news.breakeven_before_news_r
        starts = self._blackout_starts
        if min_r <= 0 or len(starts) == 0 or book.idle:
            return
        first = int(np.searchsorted(starts, bar.open_ns, side="right"))
        last = int(np.searchsorted(starts, bar.close_ns, side="right"))
        if last > first:  # a blackout started during this bar
            book.protect_before_news(
                bar.close_time, bar.close, bar.close + bar.spread, min_r, "before news"
            )

    @staticmethod
    def _collect(events: Sequence[BookEvent], result: SimulationResult) -> None:
        for event in events:
            if event.kind in _OUTCOME_EVENTS and event.outcome is not None:
                result.outcomes.append(SignalOutcome(event.payload, event.outcome, event.detail))
            elif event.kind is EventKind.CLOSED and event.trade is not None:
                result.trades.append(event.trade)
                result.equity.append((event.trade.exit_time, float(event.equity or 0.0)))

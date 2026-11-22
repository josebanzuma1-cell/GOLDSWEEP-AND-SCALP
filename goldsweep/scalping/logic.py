"""The scalper's three modes as one interface, fed closed M1 bars (S5-S6, D111-D112).

Each mode builds its own signal timeframe from the M1 feed and turns its setups into
:class:`Candidate` entries; :meth:`ModeLogic.gate` holds its own filters and
:meth:`ModeLogic.plan` makes the order (always with a stop-loss and take-profits).
The live runner (:mod:`~goldsweep.scalping.runner`) and the mode backtest
(:mod:`~goldsweep.scalping.mode_backtest`) use the same objects, so a backtest sees
what the bot would have seen.

========  ======  ==========================================  =========================
Mode      Bars    Entries                                     Exits (managed on M1)
========  ======  ==========================================  =========================
hft       M1      sweep + reclaim, step-trail rejection;      0.5/1/1.5 R, breakeven+,
                  active sessions, activity filter            stop to TP1, step trail,
                                                              15-minute time stop
intraday  M5      sweep + reclaim (micro and session          1/2/3 R, breakeven+, stop
                  levels), NY opening range, VWAP trend;      to TP1; flat by the New
                  not against the H1 trend                    York rollover
swing     M15     EMA pullback, Asian/London range            1/2/3 R, breakeven+, stop
                  breakout; only with the H1 trend            to TP1, 20-bar channel
                                                              trail, swap; flat before
                                                              the weekend, 2 days at most
========  ======  ==========================================  =========================
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ROLLOVER, SessionCalculator, local_to_utc
from goldsweep.core.settings import AppConfig, ScalperConfig, WeekendConfig
from goldsweep.execution.book import Management, StepLineSource, TradePlan
from goldsweep.scalping.bars import BarAggregator, HourlyTrend
from goldsweep.scalping.engine import SWEEP_RECLAIM, TRAIL_REJECTION, ScalpEngine
from goldsweep.scalping.intraday import IntradayEngine
from goldsweep.scalping.plan import LadderCosts, ScalpPlan, scalp_management, scalp_plan
from goldsweep.scalping.swing_scalp import SwingScalpEngine

HFT, INTRADAY, SWING = "hft", "intraday", "swing"
MINUTE = dt.timedelta(minutes=1)
FIVE = dt.timedelta(minutes=5)
HFT_RULES = (SWEEP_RECLAIM, TRAIL_REJECTION)
_NEW_YORK = ZoneInfo("America/New_York")
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class Candidate:
    """A possible scalp: a mode's setup, before the gates."""

    time: dt.datetime  # the signal bar's close (UTC)
    direction: Direction
    rule: str
    reference: float  # the planned (market) entry: the signal bar's close
    anchor: float  # what the stop goes beyond
    atr: float  # the signal timeframe's ATR
    reason: str
    close_by: dt.datetime | None = None
    source: Any = None  # the engine's own setup


#: Step-line points kept for the chart (three hours of M1 bars).
CHART_POINTS = 180


@dataclass(frozen=True, slots=True)
class ChartMarks:
    """A mode's picture for the chart panel: its trend, ATR, line and sweeps."""

    timeframe: str  # the mode's signal bars
    trend: str  # UP / DOWN / - (HFT: the step trail; intraday and swing: the H1 trend)
    atr: float | None  # on the signal bars
    line: tuple[tuple[dt.datetime, float, int], ...] = ()  # (bar open, value, trend)
    sweeps: tuple[tuple[dt.datetime, float, str], ...] = ()  # (bar open, extreme, side)


def _trend_name(direction: Direction | None) -> str:
    return "-" if direction is None else ("UP" if direction is Direction.LONG else "DOWN")


@dataclass(frozen=True, slots=True)
class Gate:
    """The session the candidate falls in, or why it may not trade."""

    session: str | None
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.reason is None


def mode_management(mode: str, config: ScalperConfig, breakeven_offset: float) -> Management:
    """How ``mode`` manages a position (the same rules on paper, in backtests and live)."""
    if mode == HFT:
        return scalp_management(config, breakeven_offset)
    if mode == INTRADAY:
        return Management(
            split=config.split,
            breakeven_offset=breakeven_offset,
            stop_to_tp1_after_tp2=True,
            step_trail=False,
            trail_buffer_atr=0.0,
            time_stop=None,
        )
    if mode == SWING:
        return Management(
            split=config.split,
            breakeven_offset=breakeven_offset,
            stop_to_tp1_after_tp2=True,
            step_trail=True,
            trail_buffer_atr=config.swing_scalp.sl_buffer_atr,
            time_stop=None,
            swap=True,
        )
    raise ValueError(f"unknown scalper mode {mode!r}")


def anchored_plan(
    candidate: Candidate,
    costs: LadderCosts,
    *,
    ladder: tuple[float, ...],
    sl_buffer_atr: float,
    sl_min_atr: float,
    sl_max_atr: float,
    cost_multiple: float,
    plan_id: str,
    session: str | None,
) -> ScalpPlan:
    """The market order: the stop beyond the anchor, targets ``ladder`` R away."""
    sign = candidate.direction.sign
    buffer = sl_buffer_atr * candidate.atr
    if candidate.direction is Direction.LONG:
        stop = candidate.anchor - buffer
    else:
        stop = candidate.anchor + buffer + costs.spread  # a short is stopped on the ask
    risk = (candidate.reference - stop) * sign
    if risk <= 0:
        return ScalpPlan(None, "the stop is on the wrong side")
    multiple = risk / candidate.atr
    if not sl_min_atr - _EPSILON <= multiple <= sl_max_atr + _EPSILON:
        return ScalpPlan(None, f"no stop within {sl_min_atr:g}-{sl_max_atr:g} ATR")
    if ladder[0] * risk < cost_multiple * costs.round_trip - _EPSILON:
        return ScalpPlan(None, "TP1 does not clear the costs")
    targets: list[float | None] = [candidate.reference + sign * step * risk for step in ladder]
    targets += [None] * (3 - len(targets))
    first = targets[0]
    assert first is not None
    plan = TradePlan(
        id=plan_id,
        time=candidate.time,
        direction=candidate.direction,
        session=session,
        entry_mode="market",
        entry=candidate.reference,
        stop_loss=stop,
        take_profit_1=first,
        take_profit_2=targets[1],
        take_profit_3=targets[2],
        expires_at=candidate.time + MINUTE,
        score=60.0,
        risk_reward_1=ladder[0],
        close_by=candidate.close_by,
    )
    return ScalpPlan(plan, "", ladder[0])


def next_rollover(sessions: SessionCalculator, moment: dt.datetime) -> dt.datetime:
    """The next start of the New York rollover block after ``moment``."""
    day = moment.astimezone(_NEW_YORK).date()
    for offset in range(3):
        start = sessions.span(ROLLOVER, day + dt.timedelta(days=offset)).start
        if start > moment:
            return start
    raise ValueError("no rollover ahead")  # pragma: no cover - there is one every day


def before_weekend(
    weekend: WeekendConfig, moment: dt.datetime, buffer: dt.timedelta
) -> dt.datetime:
    """``buffer`` before the next Friday close of the gold market."""
    zone = ZoneInfo(weekend.tz)
    local = moment.astimezone(zone)
    friday = local.date() + dt.timedelta(days=(4 - local.weekday()) % 7)
    close = local_to_utc(friday, weekend.close, zone) - buffer
    if close <= moment:
        close = local_to_utc(friday + dt.timedelta(days=7), weekend.close, zone) - buffer
    return close


# --------------------------------------------------------------------------- #
# The modes
# --------------------------------------------------------------------------- #


class ModeLogic(ABC):
    """One scalper mode: closed M1 bars in, candidates out, and how to trade them."""

    name: str
    prefix: str  # in plan ids: <date>-<prefix>-<n>

    def __init__(
        self, config: AppConfig, sessions: SessionCalculator, breakeven_offset: float
    ) -> None:
        self._config = config
        self._cfg = config.scalper
        self._sessions = sessions
        self.management = mode_management(self.name, config.scalper, breakeven_offset)

    @property
    def line(self) -> StepLineSource | None:
        """The line a trailing stop follows (``None``: no step trail)."""
        return None

    def chart(self) -> ChartMarks:
        """What the chart panel draws for this mode (S7, D113)."""
        return ChartMarks("M1", "-", None)

    @abstractmethod
    def on_bar(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[Candidate]:
        """Apply one closed M1 bar (``opened`` is its open time, UTC)."""

    def gate(self, candidate: Candidate) -> Gate:
        """The mode's own filters; the active sessions for every mode."""
        status = self._sessions.status(candidate.time)
        if not status.allowed or status.session not in self._cfg.sessions:
            return Gate(None, "outside the active sessions")
        return Gate(status.session)

    @abstractmethod
    def plan(
        self, candidate: Candidate, costs: LadderCosts, *, plan_id: str, session: str | None
    ) -> ScalpPlan:
        """The order for ``candidate``, or why there is none."""


class _EngineLine:
    """The HFT step line and ATR after the last bar fed to its engine."""

    def __init__(self, logic: HftLogic) -> None:
        self._logic = logic

    def line_at(self, close_ns: int) -> tuple[float, float]:
        engine = self._logic.engine
        atr = engine.atr
        return engine.line, (atr if atr is not None else math.nan)


class HftLogic(ModeLogic):
    """HFT on M1 (S5, D111): S1's two rules, the activity filter, the step trail."""

    name, prefix = HFT, "HFT"

    def __init__(
        self, config: AppConfig, sessions: SessionCalculator, breakeven_offset: float
    ) -> None:
        super().__init__(config, sessions, breakeven_offset)
        self.engine = ScalpEngine(self._cfg, bar=MINUTE, sessions=sessions, modes=HFT_RULES)
        self._atrs: deque[float] = deque(maxlen=self._cfg.activity_bars)
        self._line = _EngineLine(self)
        #: The step line after each M1 bar, for the chart: (bar open, line, trend).
        self.points: deque[tuple[dt.datetime, float, int]] = deque(maxlen=CHART_POINTS)

    @property
    def line(self) -> StepLineSource:
        return self._line

    def on_bar(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[Candidate]:
        setups = self.engine.on_bar(opened, open_, high, low, close)
        if self.engine.atr is not None:
            self._atrs.append(self.engine.atr)
        point = getattr(self.engine, "point", None)
        if point is not None and not math.isnan(point.line):
            self.points.append((opened, point.line, point.trend))
        return [
            Candidate(s.time, s.direction, s.mode, s.reference, s.extreme, s.atr, s.reason, None, s)
            for s in setups
        ]

    def active(self) -> bool:
        """The activity filter: the M1 ATR at least ``activity_min`` x its median."""
        if self._cfg.activity_min <= 0 or len(self._atrs) < 20:
            return True
        return self._atrs[-1] >= self._cfg.activity_min * statistics.median(self._atrs)

    def gate(self, candidate: Candidate) -> Gate:
        gate = super().gate(candidate)
        if gate.passed and not self.active():
            return Gate(None, "a quiet market")
        return gate

    def chart(self) -> ChartMarks:
        point = getattr(self.engine, "point", None)
        trend = point.trend if point is not None else 0
        return ChartMarks(
            "M1",
            "UP" if trend > 0 else "DOWN" if trend < 0 else "-",
            self.engine.atr,
            tuple(self.points),
            tuple(getattr(self.engine, "sweeps", ())),
        )

    def plan(
        self, candidate: Candidate, costs: LadderCosts, *, plan_id: str, session: str | None
    ) -> ScalpPlan:
        return scalp_plan(
            candidate.source, self._cfg, costs, plan_id=plan_id, session=session, bar=MINUTE
        )


class IntradayLogic(ModeLogic):
    """Intraday on M5 (S6, D112), entries and management on M1, flat by the rollover."""

    name, prefix = INTRADAY, "INT"

    def __init__(
        self, config: AppConfig, sessions: SessionCalculator, breakeven_offset: float
    ) -> None:
        super().__init__(config, sessions, breakeven_offset)
        rules = self._cfg.intraday.mode_rules
        trend = self._cfg.h1_trend
        self.trend = HourlyTrend(trend.ema_hours, trend.slope_hours)
        self._m5 = BarAggregator(5)
        self._sweeps = (
            ScalpEngine(self._cfg, bar=FIVE, sessions=sessions, modes=(SWEEP_RECLAIM,))
            if SWEEP_RECLAIM in rules
            else None
        )
        others = tuple(rule for rule in rules if rule != SWEEP_RECLAIM)
        self._rules = (
            IntradayEngine(self._cfg, sessions, bar=FIVE, rules=others) if others else None
        )

    def on_bar(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[Candidate]:
        self.trend.add(opened, open_, high, low, close)
        found: list[Candidate] = []
        for bar in self._m5.add(opened, open_, high, low, close, volume):
            if self._sweeps is not None:
                for s in self._sweeps.on_bar(bar.opened, bar.open, bar.high, bar.low, bar.close):
                    found.append(
                        Candidate(
                            s.time,
                            s.direction,
                            SWEEP_RECLAIM,
                            s.reference,
                            s.extreme,
                            s.atr,
                            f"M5: {s.reason}",
                            next_rollover(self._sessions, s.time),
                            s,
                        )
                    )
            if self._rules is not None:
                for r in self._rules.on_bar(
                    bar.opened, bar.open, bar.high, bar.low, bar.close, bar.volume
                ):
                    found.append(
                        Candidate(
                            r.time,
                            r.direction,
                            r.rule,
                            r.reference,
                            r.anchor,
                            r.atr,
                            f"M5: {r.detail}",
                            r.close_by,
                            r,
                        )
                    )
        return found

    def chart(self) -> ChartMarks:
        engine = self._sweeps or self._rules
        atr = engine.atr if engine is not None else None
        sweeps = tuple(self._sweeps.sweeps) if self._sweeps is not None else ()
        return ChartMarks("M5", _trend_name(self.trend.direction), atr, (), sweeps)

    def gate(self, candidate: Candidate) -> Gate:
        gate = super().gate(candidate)
        if not gate.passed:
            return gate
        trend = self.trend.direction
        if self._cfg.intraday.h1_filter and trend is not None and trend is not candidate.direction:
            return Gate(None, "against the H1 trend")
        if candidate.close_by is not None and candidate.close_by <= candidate.time + FIVE:
            return Gate(None, "too close to the rollover")
        return gate

    def plan(
        self, candidate: Candidate, costs: LadderCosts, *, plan_id: str, session: str | None
    ) -> ScalpPlan:
        cfg = self._cfg.intraday
        return anchored_plan(
            candidate,
            costs,
            ladder=cfg.mode_ladder,
            sl_buffer_atr=cfg.sl_buffer_atr,
            sl_min_atr=cfg.sl_min_atr,
            sl_max_atr=cfg.sl_max_atr,
            cost_multiple=self._cfg.risk.cost_multiple,
            plan_id=plan_id,
            session=session,
        )


class SwingLogic(ModeLogic):
    """Swing scalping on M15 (S6, D112), with the H1 trend, flat before the weekend."""

    name, prefix = SWING, "SWS"

    def __init__(
        self, config: AppConfig, sessions: SessionCalculator, breakeven_offset: float
    ) -> None:
        super().__init__(config, sessions, breakeven_offset)
        trend = self._cfg.h1_trend
        self.trend = HourlyTrend(trend.ema_hours, trend.slope_hours)
        self._m15 = BarAggregator(15)
        self.engine = SwingScalpEngine(self._cfg, sessions)
        self._weekend = config.sessions.weekend

    @property
    def line(self) -> StepLineSource:
        return self.engine

    def chart(self) -> ChartMarks:
        return ChartMarks("M15", _trend_name(self.trend.direction), self.engine.atr)

    def _close_by(self, moment: dt.datetime) -> dt.datetime:
        cfg = self._cfg.swing_scalp
        latest = moment + dt.timedelta(hours=cfg.max_hold_hours)
        weekend = before_weekend(
            self._weekend, moment, dt.timedelta(minutes=cfg.weekend_buffer_min)
        )
        return min(latest, weekend)

    def on_bar(
        self,
        opened: dt.datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> list[Candidate]:
        self.trend.add(opened, open_, high, low, close)
        found: list[Candidate] = []
        for bar in self._m15.add(opened, open_, high, low, close, volume):
            for s in self.engine.on_bar(bar, self.trend.direction):
                found.append(
                    Candidate(
                        s.time,
                        s.direction,
                        s.rule,
                        s.reference,
                        s.anchor,
                        s.atr,
                        f"M15: {s.detail}",
                        self._close_by(s.time),
                        s,
                    )
                )
        return found

    def gate(self, candidate: Candidate) -> Gate:
        gate = super().gate(candidate)
        if gate.passed and candidate.close_by is not None:
            if candidate.close_by <= candidate.time + dt.timedelta(hours=1):
                return Gate(None, "too close to the weekend")
        return gate

    def plan(
        self, candidate: Candidate, costs: LadderCosts, *, plan_id: str, session: str | None
    ) -> ScalpPlan:
        cfg = self._cfg.swing_scalp
        return anchored_plan(
            candidate,
            costs,
            ladder=cfg.ladder,
            sl_buffer_atr=cfg.sl_buffer_atr,
            sl_min_atr=cfg.sl_min_atr,
            sl_max_atr=cfg.sl_max_atr,
            cost_multiple=self._cfg.risk.cost_multiple,
            plan_id=plan_id,
            session=session,
        )


LOGICS: dict[str, type[ModeLogic]] = {HFT: HftLogic, INTRADAY: IntradayLogic, SWING: SwingLogic}


def make_logic(
    mode: str, config: AppConfig, sessions: SessionCalculator, breakeven_offset: float
) -> ModeLogic:
    return LOGICS[mode](config, sessions, breakeven_offset)

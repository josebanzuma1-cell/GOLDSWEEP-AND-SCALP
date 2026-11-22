"""The scalper running live (S5, D111): the HFT mode on closed M1 bars.

Called once a minute by the bot (under its lock). Each call:

1. reads the newest closed M1 bars from the broker (the first call warms the engine
   up on the last ``WARMUP_BARS`` bars without trading them);
2. feeds each new bar to the HFT engine (:class:`~goldsweep.scalping.engine.ScalpEngine`
   with both ``sweep_reclaim`` and ``trail_rejection``), then to the scalper's
   executors, which manage the open scalps with the shared rules (breakeven+ at TP1,
   the stop to TP1 after TP2, the step trail, the 15-minute time stop);
3. for a setup on the newest bar, runs the gates: the mode is selected and may open
   trades; one scalp at a time; an active session; the activity filter; the cooldown
   after a stop-out; a stop and ladder that pass the cost gate; the account guard
   (when the scalp shares the account with the main strategy); then the executor's
   own risk checks (limits, spread, news blackout). The order goes in at market.

Scalps go to the **paper** executor (its own notional book) unless the mode was
approved for the **demo** account (``/approve scalper hft``), whose executor sends
them to MetaTrader 5 with the scalper's own magic number. Messages are quiet (D97):
one when a scalp is placed, one when it closes.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from goldsweep.alerts import messages
from goldsweep.analysis.structure import Direction
from goldsweep.backtest.engine import spread_points
from goldsweep.core.clock import Clock, SessionCalculator, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError, SymbolSpec
from goldsweep.execution.book import Bar, BookEvent, EventKind, ExitReason, to_ns
from goldsweep.execution.executor import Executor
from goldsweep.risk.account_guard import AccountGuard, Exposure
from goldsweep.risk.risk_manager import NewsEvent
from goldsweep.scalping.engine import SWEEP_RECLAIM, TRAIL_REJECTION, ScalpEngine, ScalpSetup
from goldsweep.scalping.plan import LadderCosts, scalp_plan
from goldsweep.storage.db import Database

log = get_logger(__name__)

HFT = "hft"
HFT_RULES = (SWEEP_RECLAIM, TRAIL_REJECTION)
#: M1 bars read on the first call to warm the engine up (no trades from them).
WARMUP_BARS = 600
#: Later calls read the minutes since the last bar plus this margin.
FETCH_MARGIN = 5
PAPER_STATE = "scalper.paper.book"
DEMO_STATE = "scalper.live.book"
IDS_STATE = "scalper.ids"
MEDIAN_SPREAD_DAYS = 20
MINUTE = dt.timedelta(minutes=1)
#: A setup older than this when the job sees it (the bot was busy) is not traded.
STALE_AFTER = dt.timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class ScalpContext:
    """What the bot tells the runner each minute."""

    mode: str  # off / hft / intraday / swing
    may_open: bool  # /mode allows the scalper, trading is active, the mode is built
    demo: bool  # orders go to the demo account (the mode was approved there)
    events: Sequence[NewsEvent] = ()  # releases, for the blackout
    #: The main strategy's open trades and waiting orders in the scalp's account
    #: (``None``: the scalp does not share an account with it, e.g. paper beside live).
    main_exposures: Sequence[Exposure] | None = None
    equity: float = 0.0  # that account's equity, for the guard


class _Line:
    """The step line and ATR after the bar just fed to the runner's engine."""

    def __init__(self, runner: ScalperRunner) -> None:
        self._runner = runner

    def line_at(self, close_ns: int) -> tuple[float, float]:
        engine = self._runner.engine
        atr = engine.atr
        return engine.line, (atr if atr is not None else float("nan"))


class ScalperRunner:
    """Runs the selected scalper mode on paper or on the demo account."""

    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        db: Database,
        spec: SymbolSpec,
        clock: Clock,
        *,
        paper: Executor,
        demo: Executor | None = None,
        guard: AccountGuard | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.config
        self._cfg = settings.config.scalper
        self._broker = broker
        self._db = db
        self.spec = spec
        self._clock = clock
        self.paper = paper
        self.demo = demo
        self._guard = guard
        self._sessions = SessionCalculator(settings.config.sessions)
        self._stopped_out: dict[Direction, dt.datetime] = {}
        self._line = _Line(self)
        self.engine = ScalpEngine(self._cfg, bar=MINUTE, sessions=self._sessions, modes=HFT_RULES)
        self._atrs: deque[float] = deque(maxlen=self._cfg.activity_bars)
        self._last: dt.datetime | None = None

    def _cold(self) -> None:
        """A fresh engine: the next call warms it up on recent bars."""
        self.engine = ScalpEngine(self._cfg, bar=MINUTE, sessions=self._sessions, modes=HFT_RULES)
        self._atrs.clear()
        self._last = None

    @property
    def warm(self) -> bool:
        return self._last is not None

    # -- what is open ------------------------------------------------------------- #

    @property
    def executors(self) -> list[Executor]:
        return [self.paper] + ([self.demo] if self.demo is not None else [])

    def target(self, demo: bool) -> Executor:
        return self.demo if demo and self.demo is not None else self.paper

    @staticmethod
    def exposures(executor: Executor) -> list[Exposure]:
        found = [
            Exposure(Direction(p["direction"]), float(p.get("risk_amount", 0.0)))
            for p in executor.open_positions
        ]
        found += [Exposure(plan.direction, 0.0) for plan in executor.pending_orders]
        return found

    @staticmethod
    def busy(executor: Executor) -> bool:
        return bool(executor.open_positions or executor.pending_orders)

    # -- each minute ----------------------------------------------------------------- #

    def step(self, context: ScalpContext) -> list[str]:
        """Process the M1 bars closed since the last call; returns the messages to send."""
        now = ensure_utc(self._clock.now())
        texts: list[str] = []
        if self.demo is not None:  # fills and exits at the broker (TP, SL) since last time
            texts += self._report(self.demo, self.demo.sync(now))
        symbol = self.spec.name
        gap = None if self._last is None else int((now - self._last).total_seconds() // 60)
        if gap is not None and gap > WARMUP_BARS:
            self._cold()  # bars were missed (the PC slept): warm up again
            gap = None
        warmup = gap is None
        count = WARMUP_BARS if gap is None else min(WARMUP_BARS, gap + FETCH_MARGIN)
        bars = self._broker.get_rates(symbol, Timeframe.M1, count)
        if self._last is not None and not bars.empty:
            bars = bars.loc[bars.index > pd.Timestamp(self._last)]
        if bars.empty:
            return texts
        spreads = spread_points(bars, self._config.backtest.costs) * self.spec.point
        newest = len(bars) - 1
        for i, (opened, row) in enumerate(bars.iterrows()):
            start = opened.to_pydatetime()
            setups = self.engine.on_bar(
                start,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
            )
            if self.engine.atr is not None:
                self._atrs.append(self.engine.atr)
            stamp = to_ns(start)
            bar = Bar(
                stamp,
                stamp + 60 * 10**9,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(spreads[i]),
            )
            if not warmup or i == newest:  # a (re)start replays no history into the books
                for executor in self.executors:
                    texts += self._report(executor, executor.on_bar(bar, line=self._line))
            if not warmup and i == newest:
                for setup in setups:
                    text = self._consider(setup, context, now)
                    if text:
                        texts.append(text)
                        break  # one scalp at a time
        self._last = bars.index[-1].to_pydatetime()
        return texts

    def _report(self, executor: Executor, events: Sequence[BookEvent]) -> list[str]:
        where = self._where(executor)
        texts: list[str] = []
        for event in events:
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trade = event.trade
                if trade.exit_reason is ExitReason.STOP:
                    self._stopped_out[trade.direction] = trade.exit_time
                texts.append(messages.scalp_closed(trade, HFT, where))
        return texts

    def _where(self, executor: Executor) -> str:
        return "demo" if executor is self.demo else "paper"

    def _consider(self, setup: ScalpSetup, context: ScalpContext, now: dt.datetime) -> str | None:
        """Every gate, then the order. Returns the message for a placed scalp."""
        if context.mode != HFT or not context.may_open:
            return None
        if now - setup.time > STALE_AFTER:  # the job ran late: the moment has passed
            log.info("scalp skipped: the setup is stale", age=str(now - setup.time))
            return None
        executor = self.target(context.demo)
        if self.busy(executor):
            return None
        status = self._sessions.status(setup.time)
        if not status.allowed or status.session not in self._cfg.sessions:
            return None
        if not self._active():
            log.info("scalp skipped: a quiet market", id=setup.time.isoformat())
            return None
        stopped = self._stopped_out.get(setup.direction)
        cooldown = dt.timedelta(seconds=self._cfg.risk.cooldown_sec)
        if stopped is not None and setup.time - stopped < cooldown:
            return None
        try:
            spread = float(self._broker.spread_points(self.spec.name))
        except BrokerError as exc:
            log.warning("scalp skipped: no spread", error=str(exc))
            return None
        median = self._db.median_spread(
            self.spec.name,
            since=now - dt.timedelta(days=MEDIAN_SPREAD_DAYS),
            min_samples=30,
        )
        costs = LadderCosts.of(self.spec, self._config.backtest.costs, spread)
        made = scalp_plan(
            setup,
            self._cfg,
            costs,
            plan_id=self._next_id(now),
            session=status.session,
            bar=MINUTE,
        )
        if made.plan is None:
            log.info("scalp skipped", reason=made.reason)
            return None
        plan = made.plan
        if self._guard is not None and context.main_exposures is not None:
            risk = context.equity * self._cfg.risk.risk_per_scalp_pct / 100.0
            decision = self._guard.check(
                now,
                equity=context.equity,
                direction=plan.direction,
                new_risk=risk,
                exposures=[*context.main_exposures, *self.exposures(executor)],
            )
            if not decision.allowed:
                log.info("scalp refused by the account guard", reasons=list(decision.reasons))
                return None
        event = executor.submit(
            plan, spread_points=spread, median_spread_points=median, events=context.events
        )
        if event.kind is not EventKind.PLACED:
            log.info("scalp not placed", id=plan.id, detail=event.detail)
            return None
        self._count_id(plan.id)
        log.info("scalp placed", id=plan.id, where=self._where(executor), reason=setup.reason)
        return messages.scalp_placed(plan, event, HFT, self._where(executor), setup.reason)

    def _active(self) -> bool:
        """The activity filter: the M1 ATR at least ``activity_min`` x its median."""
        if self._cfg.activity_min <= 0 or len(self._atrs) < 20:
            return True
        return self._atrs[-1] >= self._cfg.activity_min * statistics.median(self._atrs)

    def _next_id(self, now: dt.datetime) -> str:
        """The next scalp's id, ``<date>-HFT-<n>`` (counted once the scalp is placed)."""
        day = f"{now:%Y-%m-%d}"
        counts = dict(self._db.get_state(IDS_STATE, {}))
        return f"{day}-HFT-{int(counts.get(day, 0)) + 1:03d}"

    def _count_id(self, plan_id: str) -> None:
        day, _, number = plan_id.rpartition("-HFT-")
        self._db.set_state(IDS_STATE, {day: int(number)})

    # -- the main strategy, the kill switch, the confirmation ------------------------- #

    def yield_to_main(self, direction: Direction, executor: Executor) -> list[str]:
        """The main strategy is about to take ``direction``: an opposite scalp in the same
        account is closed first (no hedging; the main strategy comes first, D92)."""
        if not any(e.direction is not direction for e in self.exposures(executor)):
            return []
        try:
            tick = self._broker.get_tick(self.spec.name)
        except BrokerError as exc:
            log.error("opposite scalp not closed: no quote", error=str(exc))
            return []
        events = executor.close_everything(
            ensure_utc(self._clock.now()),
            tick.bid,
            tick.ask,
            "the main strategy takes the other side",
        )
        return self._report(executor, events)

    def close_everything(self, detail: str) -> list[str]:
        """The kill switch: every scalp, on paper and on the demo account (every position
        with the scalper's magic number). Returns one message per closed scalp."""
        texts: list[str] = []
        try:
            tick = self._broker.get_tick(self.spec.name)
        except BrokerError as exc:
            log.error("scalps not closed: no quote", error=str(exc))
            return texts
        now = ensure_utc(self._clock.now())
        for executor in self.executors:
            if self.busy(executor) or executor is self.demo:
                texts += self._report(
                    executor, executor.close_everything(now, tick.bid, tick.ask, detail)
                )
        return texts

    def arm(self) -> list[str]:
        return self.demo.arm() if self.demo is not None else []

    def today(self, now: dt.datetime) -> tuple[int, float]:
        """Scalps closed in the last 24 hours on both books, and their total R."""
        rows = []
        for executor in self.executors:
            rows += executor.trades_between(now - dt.timedelta(hours=24), now)
        return len(rows), sum(row.r_multiple for row in rows)

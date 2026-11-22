"""The scalper running live (S5-S6, D111-D112): its three modes on the M1 feed.

Called once a minute by the bot (under its lock). Each call:

1. reads the M1 bars closed since the last call from the broker (the first call warms
   every mode up on the last ``WARMUP_BARS`` bars without trading them);
2. feeds each bar to every mode's logic (:mod:`~goldsweep.scalping.logic`: HFT on M1,
   intraday on M5, swing scalping on M15, each building its own bars), then to that
   mode's executors, which manage its open scalps with the shared rules. Every mode
   is fed, selected or not, so a switch takes effect at once and a mode's open scalps
   are managed to the end by their own rules;
3. for a candidate of the **selected** mode on the newest bar, runs the gates: the
   mode may open trades; the setup is fresh; that mode has no scalp open; its own
   filters (sessions, activity, H1 trend, flat-by time); the cooldown after a
   stop-out; a stop and ladder that pass the cost gate; the account guard (when the
   scalp shares the demo account with the main strategy); then the executor's own
   risk checks (limits, spread, news blackout). The order goes in at market.

Each mode has a **paper** book (its own notional balance) and, on a live bot, a
**demo** executor on the same MT5 account with the mode's own magic number; an
approved mode (``/approve scalper <mode>``) sends its scalps there. Messages are
quiet (D97): one when a scalp is placed, one when it closes.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
from goldsweep.execution.executor import (
    SCALP_LIVE_TABLE,
    SCALP_PAPER_TABLE,
    Executor,
    PaperExecutor,
    TradeRow,
    trades_between,
)
from goldsweep.risk.account_guard import AccountGuard, Exposure
from goldsweep.risk.risk_manager import NewsEvent
from goldsweep.scalping.logic import HFT, INTRADAY, SWING, Candidate, ModeLogic, make_logic
from goldsweep.scalping.modes import REGISTRY
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.storage.db import Database

log = get_logger(__name__)

#: M1 bars read on the first call to warm the modes up (no trades from them): enough
#: for the H1 trend (55 hours) and the day's ranges.
WARMUP_BARS = 6000
#: Later calls read the minutes since the last bar plus this margin.
FETCH_MARGIN = 5
#: Each mode's books (D111, D112): paper state, demo state, order comment.
PAPER_STATES = {
    HFT: "scalper.paper.book",
    INTRADAY: "scalper.intraday.paper.book",
    SWING: "scalper.swing.paper.book",
}
DEMO_STATES = {
    HFT: "scalper.live.book",
    INTRADAY: "scalper.intraday.live.book",
    SWING: "scalper.swing.live.book",
}
COMMENTS = {HFT: "GS-SC", INTRADAY: "GS-SI", SWING: "GS-SW"}
#: Kept for callers of S5.
PAPER_STATE, DEMO_STATE = PAPER_STATES[HFT], DEMO_STATES[HFT]
IDS_STATE = "scalper.ids"
MEDIAN_SPREAD_DAYS = 20
MINUTE = dt.timedelta(minutes=1)
#: A setup older than this when the job sees it (the bot was busy) is not traded.
STALE_AFTER = dt.timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class ScalpContext:
    """What the bot tells the runner each minute."""

    mode: str  # the selected mode: off / hft / intraday / swing
    may_open: bool  # /mode allows the scalper, trading is active, the mode is built
    demo: bool  # orders go to the demo account (the mode was approved there)
    events: Sequence[NewsEvent] = ()  # releases, for the blackout
    #: The main strategy's open trades and waiting orders in the scalp's account
    #: (``None``: the scalp does not share an account with it, e.g. paper beside live).
    main_exposures: Sequence[Exposure] | None = None
    equity: float = 0.0  # that account's equity, for the guard


@dataclass
class ModeSlot:
    """One mode: its logic, its paper book and (live) its demo executor."""

    logic: ModeLogic
    paper: Executor
    demo: Executor | None = None
    stopped_out: dict[Direction, dt.datetime] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.logic.name

    @property
    def executors(self) -> list[Executor]:
        return [self.paper] + ([self.demo] if self.demo is not None else [])

    def target(self, demo: bool) -> Executor:
        return self.demo if demo and self.demo is not None else self.paper

    def where(self, executor: Executor) -> str:
        return "demo" if executor is self.demo else "paper"


def breakeven_offset(settings: Settings, spec: SymbolSpec) -> float:
    """How far past the entry a breakeven+ stop sits: the costs beyond the spread."""
    return LadderCosts.of(spec, settings.config.backtest.costs, 0.0).breakeven_offset


def built_modes() -> list[str]:
    return [name for name, info in REGISTRY.items() if info.built]


def demo_executor(
    mode: str,
    settings: Settings,
    broker: BrokerClient,
    spec: SymbolSpec,
    db: Database,
    clock: Clock,
    *,
    demo: bool,
    armed: bool,
) -> Executor:
    """``mode``'s executor on the live MT5 account: its own magic number, state, rules."""
    from goldsweep.execution.live_executor import MT5Executor, order_logger
    from goldsweep.execution.mt5_gateway import MT5Gateway
    from goldsweep.scalping.logic import mode_management

    config = settings.config
    gateway = MT5Gateway(
        broker,  # type: ignore[arg-type] - the MT5 client on a live bot
        spec,
        magic=config.scalper.magic_for(mode),
        deviation_points=config.execution.deviation_points,
        order_log=order_logger(db, clock),
    )
    return MT5Executor(
        settings,
        gateway,
        db,
        clock=clock,
        demo=demo,
        armed=armed,
        state_key=DEMO_STATES[mode],
        table=SCALP_LIVE_TABLE,
        risk=scalp_risk(config),
        management=mode_management(mode, config.scalper, breakeven_offset(settings, spec)),
        comment_prefix=COMMENTS[mode],
    )


class ScalperRunner:
    """Runs the scalper's modes on paper or on the demo account."""

    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        db: Database,
        spec: SymbolSpec,
        clock: Clock,
        *,
        slots: Mapping[str, ModeSlot],
        guard: AccountGuard | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.config
        self._broker = broker
        self._db = db
        self.spec = spec
        self._clock = clock
        self.slots = dict(slots)
        self._guard = guard
        self._last: dt.datetime | None = None
        #: The last M1 close seen (for the chart's current R).
        self.last_close: float | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        broker: BrokerClient,
        db: Database,
        spec: SymbolSpec,
        clock: Clock,
        *,
        demos: Mapping[str, Executor] | None = None,
        guard: AccountGuard | None = None,
        modes: Sequence[str] | None = None,
    ) -> ScalperRunner:
        """Every built mode with its logic and paper book (and demo executor, if given)."""
        config = settings.config
        sessions = SessionCalculator(config.sessions)
        offset = breakeven_offset(settings, spec)
        slots: dict[str, ModeSlot] = {}
        for mode in modes if modes is not None else built_modes():
            logic = make_logic(mode, config, sessions, offset)
            paper = PaperExecutor(
                settings,
                spec,
                db,
                starting_equity=config.scalper.paper_balance,
                state_key=PAPER_STATES[mode],
                table=SCALP_PAPER_TABLE,
                risk=scalp_risk(config),
                management=logic.management,
            )
            slots[mode] = ModeSlot(logic, paper, (demos or {}).get(mode))
        return cls(settings, broker, db, spec, clock, slots=slots, guard=guard)

    @property
    def warm(self) -> bool:
        return self._last is not None

    def _cold(self) -> None:
        """Fresh logic for every mode: the next call warms them up on recent bars."""
        sessions = SessionCalculator(self._config.sessions)
        offset = breakeven_offset(self._settings, self.spec)
        for slot in self.slots.values():
            slot.logic = make_logic(slot.name, self._config, sessions, offset)
        self._last = None

    # -- what is open ------------------------------------------------------------- #

    @property
    def executors(self) -> list[Executor]:
        return [e for slot in self.slots.values() for e in slot.executors]

    @property
    def demo_executors(self) -> list[Executor]:
        return [slot.demo for slot in self.slots.values() if slot.demo is not None]

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
        for slot in self.slots.values():  # fills and exits at the broker since last time
            if slot.demo is not None:
                texts += self._report(slot, slot.demo, slot.demo.sync(now))
        gap = None if self._last is None else int((now - self._last).total_seconds() // 60)
        if gap is not None and gap > WARMUP_BARS:
            self._cold()  # bars were missed (the PC slept): warm up again
            gap = None
        warmup = gap is None
        count = WARMUP_BARS if gap is None else min(WARMUP_BARS, gap + FETCH_MARGIN)
        bars = self._broker.get_rates(self.spec.name, Timeframe.M1, count)
        if self._last is not None and not bars.empty:
            bars = bars.loc[bars.index > pd.Timestamp(self._last)]
        if bars.empty:
            return texts
        spreads = spread_points(bars, self._config.backtest.costs) * self.spec.point
        columns = [bars[c].to_numpy(dtype="float64") for c in ("open", "high", "low", "close")]
        volumes = (
            bars["tick_volume"].to_numpy(dtype="float64")
            if "tick_volume" in bars
            else [1.0] * len(bars)
        )
        times = bars.index.to_pydatetime()
        newest = len(bars) - 1
        for i in range(len(bars)):
            start = ensure_utc(times[i])
            o, h, low, c = (float(column[i]) for column in columns)
            stamp = to_ns(start)
            bar = Bar(stamp, stamp + 60 * 10**9, o, h, low, c, float(spreads[i]))
            for slot in self.slots.values():
                candidates = slot.logic.on_bar(start, o, h, low, c, float(volumes[i]))
                if not warmup or i == newest:  # a (re)start replays no history into the books
                    for executor in slot.executors:
                        events = executor.on_bar(bar, line=slot.logic.line)
                        texts += self._report(slot, executor, events)
                if warmup or i != newest or slot.name != context.mode:
                    continue
                for candidate in candidates:
                    text = self._consider(slot, candidate, context, now)
                    if text:
                        texts.append(text)
                        break  # one scalp at a time
        self._last = ensure_utc(times[-1])
        self.last_close = float(columns[3][-1])
        return texts

    def _report(self, slot: ModeSlot, executor: Executor, events: Sequence[BookEvent]) -> list[str]:
        texts: list[str] = []
        for event in events:
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trade = event.trade
                if trade.exit_reason is ExitReason.STOP:
                    slot.stopped_out[trade.direction] = trade.exit_time
                texts.append(messages.scalp_closed(trade, slot.name, slot.where(executor)))
        return texts

    def _consider(
        self, slot: ModeSlot, candidate: Candidate, context: ScalpContext, now: dt.datetime
    ) -> str | None:
        """Every gate, then the order. Returns the message for a placed scalp."""
        if not context.may_open:
            return None
        if now - candidate.time > STALE_AFTER:  # the job ran late: the moment has passed
            log.info("scalp skipped: the setup is stale", age=str(now - candidate.time))
            return None
        executor = slot.target(context.demo)
        if self.busy(executor):
            return None
        gate = slot.logic.gate(candidate)
        if not gate.passed:
            log.info("scalp skipped", mode=slot.name, reason=gate.reason)
            return None
        stopped = slot.stopped_out.get(candidate.direction)
        cooldown = dt.timedelta(seconds=self._config.scalper.risk.cooldown_sec)
        if stopped is not None and candidate.time - stopped < cooldown:
            return None
        try:
            spread = float(self._broker.spread_points(self.spec.name))
        except BrokerError as exc:
            log.warning("scalp skipped: no spread", error=str(exc))
            return None
        median = self._db.median_spread(
            self.spec.name, since=now - dt.timedelta(days=MEDIAN_SPREAD_DAYS), min_samples=30
        )
        costs = LadderCosts.of(self.spec, self._config.backtest.costs, spread)
        made = slot.logic.plan(
            candidate, costs, plan_id=self._next_id(slot, now), session=gate.session
        )
        if made.plan is None:
            log.info("scalp skipped", mode=slot.name, reason=made.reason)
            return None
        plan = made.plan
        if self._guard is not None and context.main_exposures is not None:
            risk = context.equity * self._config.scalper.risk.risk_per_scalp_pct / 100.0
            mine = [x for e in self.demo_executors for x in self.exposures(e)]
            decision = self._guard.check(
                now,
                equity=context.equity,
                direction=plan.direction,
                new_risk=risk,
                exposures=[*context.main_exposures, *mine],
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
        where = slot.where(executor)
        log.info("scalp placed", id=plan.id, where=where, rule=candidate.rule)
        return messages.scalp_placed(plan, event, slot.name, where, candidate.reason)

    def _next_id(self, slot: ModeSlot, now: dt.datetime) -> str:
        """The next id, ``<date>-<prefix>-<n>`` (counted once the scalp is placed)."""
        day = f"{now:%Y-%m-%d}"
        counts = dict(self._db.get_state(IDS_STATE, {}))
        number = int(counts.get(f"{slot.logic.prefix}:{day}", 0)) + 1
        return f"{day}-{slot.logic.prefix}-{number:03d}"

    def _count_id(self, plan_id: str) -> None:
        day, prefix, number = plan_id[:10], plan_id[11:].rsplit("-", 1)[0], plan_id.rsplit("-")[-1]
        counts = {
            key: value
            for key, value in dict(self._db.get_state(IDS_STATE, {})).items()
            if key.endswith(day)  # only today's counts are kept
        }
        counts[f"{prefix}:{day}"] = int(number)
        self._db.set_state(IDS_STATE, counts)

    # -- the main strategy, the kill switch, the confirmation ------------------------- #

    def yield_to_main(self, direction: Direction) -> list[str]:
        """The main strategy is about to take ``direction``: opposite scalps on the demo
        account are closed first (no hedging; the main strategy comes first, D92)."""
        texts: list[str] = []
        for slot in self.slots.values():
            demo = slot.demo
            if demo is None or not any(e.direction is not direction for e in self.exposures(demo)):
                continue
            try:
                tick = self._broker.get_tick(self.spec.name)
            except BrokerError as exc:
                log.error("opposite scalp not closed: no quote", error=str(exc))
                return texts
            events = demo.close_everything(
                ensure_utc(self._clock.now()),
                tick.bid,
                tick.ask,
                "the main strategy takes the other side",
            )
            texts += self._report(slot, demo, events)
        return texts

    def close_everything(self, detail: str) -> list[str]:
        """The kill switch: every scalp of every mode, on paper and on the demo account
        (every position with a scalper magic number). One message per closed scalp."""
        texts: list[str] = []
        try:
            tick = self._broker.get_tick(self.spec.name)
        except BrokerError as exc:
            log.error("scalps not closed: no quote", error=str(exc))
            return texts
        now = ensure_utc(self._clock.now())
        for slot in self.slots.values():
            for executor in slot.executors:
                if self.busy(executor) or executor is slot.demo:
                    events = executor.close_everything(now, tick.bid, tick.ask, detail)
                    texts += self._report(slot, executor, events)
        return texts

    def arm(self) -> list[str]:
        notes: list[str] = []
        for slot in self.slots.values():
            if slot.demo is not None:
                notes += [f"{slot.name}: {note}" for note in slot.demo.arm()]
        return notes

    def closed_since(self, now: dt.datetime, hours: int = 24) -> list[TradeRow]:
        """Scalps of every mode closed in the last ``hours``, on paper and on the demo."""
        start = now - dt.timedelta(hours=hours)
        rows = trades_between(self._db, SCALP_PAPER_TABLE, start, now)
        if self.demo_executors:
            rows += trades_between(self._db, SCALP_LIVE_TABLE, start, now)
        return rows

    def today(self, now: dt.datetime) -> tuple[int, float]:
        """Scalps closed in the last 24 hours, and their total R."""
        rows = self.closed_since(now)
        return len(rows), sum(row.r_multiple for row in rows)

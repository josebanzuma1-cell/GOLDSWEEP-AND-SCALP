"""The scalper running live (S5-S6, S9; D111, D112, D115): its modes on the M1 feed.

Called once a minute by the bot (under its lock). Each call:

1. reads the M1 bars closed since the last call from the broker (the first call warms
   every mode up on the last ``WARMUP_BARS`` bars without trading them);
2. feeds each bar to every mode's logic (:mod:`~goldsweep.scalping.logic`: HFT on M1,
   intraday on M5, swing scalping on M15, each building its own bars), then to that
   mode's executors, which manage its open scalps with the shared rules. Every mode
   is fed, selected or not, so a switch takes effect at once and a mode's open scalps
   are managed to the end by their own rules;
3. for a candidate of a **selected** mode on the newest bar (one, two or all three
   modes may be selected, S9), runs the gates: the mode may open trades; the setup
   is fresh; that mode has no scalp open; its own filters (sessions, activity, H1
   trend, flat-by time); for HFT, a live A or A+ setup of the main strategy in its
   direction (:mod:`~goldsweep.scalping.grade_filter`); the cooldown after a
   stop-out; the rules the modes share in one account (below); a stop and ladder that
   pass the cost gate; the account guard (when the scalp shares the demo account with
   the main strategy); then the executor's own risk checks (limits, spread, news
   blackout). The order goes in at market.

**Shared rules** (S9, D115), per account (all paper books together, or the demo
account): no scalp against another mode's open scalp (no hedging, unless the owner's
``recovery.hedge_modes`` allows modes on opposite sides, S10); a second scalp the same
way only when every scalp open that way is risk-free, its stop at or past its entry
(no stacking); and one set of daily limits for all modes: the scalper's daily loss in
money, its scalps a day and the pause after losses in a row, a scalp and its adds
counting as one.

**The owner's capped recovery features** (S10, D116; :mod:`~goldsweep.scalping.recovery`),
on paper and a demo account only, never a real account: martingale sizing after a loss,
grid / averaging-down adds inside a scalp's first stop, HFT stacking while every open
HFT scalp is risk-free. Every mode follows the main strategy's graded setups (HFT A or
A+, intraday and swing A+, A or B); a scalp whose setup reaches its last target is
closed (``setup_done``) and the mode waits for the next one.

Each mode has a **paper** book (its own notional balance) and, on a live bot, a
**demo** executor on the same MT5 account with the mode's own magic number; an
approved mode (``/approve scalper <mode>``) sends its scalps there. Messages are
quiet (D97): one when a scalp is placed, one when it closes.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import pandas as pd

from goldsweep.alerts import messages
from goldsweep.analysis.periods import trading_day_of
from goldsweep.analysis.structure import Direction
from goldsweep.backtest.engine import spread_points
from goldsweep.core.clock import Clock, SessionCalculator, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError, SymbolSpec
from goldsweep.execution.book import Bar, BookEvent, EventKind, ExitReason, TradePlan, to_ns
from goldsweep.execution.executor import (
    SCALP_LIVE_TABLE,
    SCALP_PAPER_TABLE,
    Executor,
    PaperExecutor,
    TradeRow,
    trades_between,
)
from goldsweep.risk.account_guard import AccountGuard, Exposure
from goldsweep.risk.risk_manager import PAUSE_AFTER_LOSSES, NewsEvent, position_size
from goldsweep.scalping.grade_filter import (
    GRADE_NAMES,
    GradedSetup,
    PathBar,
    backing_grade,
    ended,
    followed,
    hft_permission,
    target_reached,
)
from goldsweep.scalping.grading import Analysis, Grade, grade_scalp
from goldsweep.scalping.logic import HFT, INTRADAY, SWING, Candidate, ModeLogic, make_logic
from goldsweep.scalping.modes import REGISTRY
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.scalping.recovery import (
    OpenScalp,
    add_due,
    add_plan,
    budget_pct,
    idea_room,
    is_add,
    martingale_risk,
    open_risk,
    primary_of,
    stack_refusal,
)
from goldsweep.scalping.retest import CONFIRMED, FAILED, RetestWatch, check, confirmed, retest_level
from goldsweep.scalping.stats import mode_of
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
#: The setup each open scalp follows, and the risk of each scalp's adds (S10, D116).
FOLLOWS_STATE = "scalper.follows"
ADDS_STATE = "scalper.adds"
#: The mode names the filters' messages use.
LABELS = {HFT: "HFT", INTRADAY: "intraday", SWING: "swing scalping"}
MEDIAN_SPREAD_DAYS = 20
MINUTE = dt.timedelta(minutes=1)
#: A setup older than this when the job sees it (the bot was busy) is not traded.
STALE_AFTER = dt.timedelta(seconds=90)
#: M1 bars of price path kept for the A/A+ filter (a day).
PATH_BARS = 1440
#: Closed scalps read for the shared limits (the losses-in-a-row pause looks this far).
SHARED_LOOKBACK = dt.timedelta(days=7)
_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class ScalpContext:
    """What the bot tells the runner each minute.

    ``modes`` and ``demo_modes`` (S9) say which modes are selected and which of them
    trade on the demo account; without them, ``mode`` and ``demo`` (S5-S8: one mode)
    do.
    """

    mode: str = "off"  # one selected mode: off / hft / intraday / swing
    may_open: bool = False  # /mode allows the scalper, trading is active
    demo: bool = False  # ``mode``'s orders go to the demo account (approved there)
    events: Sequence[NewsEvent] = ()  # releases, for the blackout
    #: The main strategy's open trades and waiting orders in the demo account
    #: (``None``: no scalp shares an account with it, e.g. paper beside live).
    main_exposures: Sequence[Exposure] | None = None
    equity: float = 0.0  # that account's equity, for the guard
    modes: frozenset[str] | None = None  # the selected (built) modes
    demo_modes: frozenset[str] | None = None  # those approved for the demo account
    #: The main strategy's A/A+ setups HFT may follow (``None``: the filter is off).
    hft_setups: Sequence[GradedSetup] | None = None
    #: Each mode's graded setups to follow (S10); a mode missing or None: no filter.
    setups: Mapping[str, Sequence[GradedSetup] | None] | None = None
    #: Every live A+, A or B main setup per mode, for grading a scalp (S11, D118), and
    #: the main strategy's picture of the market sent with each signal.
    graded: Mapping[str, Sequence[GradedSetup]] | None = None
    analysis: Analysis | None = None

    def setups_for(self, mode: str) -> Sequence[GradedSetup] | None:
        """The setups ``mode`` follows (``None``: it may trade either way)."""
        if self.setups is not None:
            return self.setups.get(mode)
        return self.hft_setups if mode == HFT else None

    @property
    def selected(self) -> frozenset[str]:
        if self.modes is not None:
            return self.modes
        return frozenset() if self.mode == "off" else frozenset({self.mode})

    def on_demo(self, mode: str) -> bool:
        """Do ``mode``'s new scalps go to the demo account?"""
        if self.demo_modes is not None:
            return mode in self.demo_modes
        return self.demo and mode == self.mode


@dataclass(frozen=True, slots=True)
class Decision:
    """What a selected mode did on its latest bar (the chart's Quick Scalp line)."""

    time: dt.datetime  # the signal bar's close (UTC)
    signal: str  # BUY / SELL / NONE
    text: str  # "placed <id> (paper)", "skipped: <why>", "no setup on the last bar"
    rule: str = ""


@dataclass
class ModeSlot:
    """One mode: its logic, its paper book and (live) its demo executor."""

    logic: ModeLogic
    paper: Executor
    demo: Executor | None = None
    stopped_out: dict[Direction, dt.datetime] = field(default_factory=dict)
    decision: Decision | None = None
    retest: RetestWatch | None = None  # a setup waiting for its retest (S11)

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


def risk_free(position: Mapping[str, object], direction: Direction) -> bool:
    """Is an open scalp's stop at or past its entry (it can no longer lose)?"""
    stop = float(position["stop"])  # type: ignore[arg-type]
    fill = float(position["fill_price"])  # type: ignore[arg-type]
    if direction is Direction.LONG:
        return stop >= fill - _EPSILON
    return stop <= fill + _EPSILON


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
        #: Recent M1 highs and lows: has an A/A+ setup reached its target or failed?
        self.path: deque[PathBar] = deque(maxlen=PATH_BARS)
        #: The setup each open scalp follows (S10), and each scalp's adds' risk in %.
        self.follows: dict[str, GradedSetup] = {
            key: GradedSetup.from_state(value)
            for key, value in dict(db.get_state(FOLLOWS_STATE, {})).items()
        }
        self.adds: dict[str, list[float]] = {
            key: [float(v) for v in value]
            for key, value in dict(db.get_state(ADDS_STATE, {})).items()
        }

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

    @staticmethod
    def recovery_on(slot: ModeSlot, executor: Executor) -> bool:
        """The owner's recovery features run on paper and a demo account, never a real one."""
        return executor is slot.paper or bool(getattr(executor, "demo", False))

    def _filter(self, mode: str) -> tuple[str, dt.timedelta]:
        """The grades ``mode`` follows and how long a setup may lead it (S9, S10)."""
        scalper = self._config.scalper
        if mode == HFT:
            return scalper.hft.grade_filter, dt.timedelta(minutes=scalper.hft.a_window_min)
        if mode == INTRADAY:
            intraday = scalper.intraday
            return intraday.grade_filter, dt.timedelta(minutes=intraday.setup_window_min)
        swing = scalper.swing_scalp
        return swing.grade_filter, dt.timedelta(minutes=swing.setup_window_min)

    def _save_follows(self) -> None:
        self._db.set_state(FOLLOWS_STATE, {k: v.to_state() for k, v in self.follows.items()})
        self._db.set_state(ADDS_STATE, self.adds)

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
            self.path.append(PathBar(start, h, low))
            for slot in self.slots.values():
                candidates = slot.logic.on_bar(start, o, h, low, c, float(volumes[i]))
                if not warmup or i == newest:  # a (re)start replays no history into the books
                    for executor in slot.executors:
                        events = executor.on_bar(bar, line=slot.logic.line)
                        texts += self._report(slot, executor, events)
                if not warmup and i == newest:
                    texts += self._setup_done(slot, now)
                if warmup or i != newest or slot.name not in context.selected:
                    continue
                texts += self._add_if_due(slot, context, c, start + MINUTE, now)
                waiting = slot.retest is not None
                retested = self._retest_tick(slot, start, h, low, c)
                if retested is not None:
                    text = self._consider(slot, retested, context, now)
                    if text:
                        texts.append(text)
                        continue  # one scalp at a time
                if not candidates and not waiting:
                    slot.decision = Decision(start + MINUTE, "NONE", "no setup on the last bar")
                for candidate in candidates:
                    if self._waits_for_retest(slot):
                        self._start_retest(slot, candidate, now)
                        continue
                    text = self._consider(slot, candidate, context, now)
                    if text:
                        texts.append(text)
                        break  # one scalp at a time
        self._last = ensure_utc(times[-1])
        self.last_close = float(columns[3][-1])
        return texts

    def _report(self, slot: ModeSlot, executor: Executor, events: Sequence[BookEvent]) -> list[str]:
        texts: list[str] = []
        changed = False
        for event in events:
            if event.kind in (EventKind.CLOSED, EventKind.CANCELLED, EventKind.EXPIRED):
                plan_id = event.plan.id
                changed |= self.follows.pop(plan_id, None) is not None
                if not is_add(plan_id) and not self._still_open(plan_id):
                    changed |= self.adds.pop(plan_id, None) is not None
            signals = self._config.execution.signals_only
            if signals and event.kind is EventKind.STOP_MOVED and event.detail == "breakeven":
                texts.append(messages.breakeven_reminder(event.plan, slot.name, event.price))
            if event.kind is EventKind.CLOSED and event.trade is not None:
                trade = event.trade
                if trade.exit_reason is ExitReason.STOP:
                    slot.stopped_out[trade.direction] = trade.exit_time
                if signals:
                    texts.append(messages.scalp_signal_result(trade, slot.name))
                else:
                    texts.append(messages.scalp_closed(trade, slot.name, slot.where(executor)))
        if changed:
            self._save_follows()
        return texts

    def _still_open(self, plan_id: str) -> bool:
        return any(p["id"] == plan_id for e in self.executors for p in e.open_positions) or any(
            plan.id == plan_id for e in self.executors for plan in e.pending_orders
        )

    # -- S11: the retest ----------------------------------------------------------------- #

    def _waits_for_retest(self, slot: ModeSlot) -> bool:
        retest = self._config.scalper.retest
        return retest.enabled and slot.name in retest.modes

    def _start_retest(self, slot: ModeSlot, candidate: Candidate, now: dt.datetime) -> None:
        """A new setup waits for price to come back to its level (S11, D118)."""
        side = "BUY" if candidate.direction is Direction.LONG else "SELL"
        gate = slot.logic.gate(candidate)
        if not gate.passed:
            slot.decision = Decision(
                candidate.time,
                side,
                f"skipped: {gate.reason or 'the modes filters'}",
                candidate.rule,
            )
            return
        try:
            spread = float(self._broker.spread_points(self.spec.name))
        except BrokerError:
            return
        costs = LadderCosts.of(self.spec, self._config.backtest.costs, spread)
        made = slot.logic.plan(candidate, costs, plan_id="retest", session=gate.session)
        if made.plan is None:
            slot.decision = Decision(
                candidate.time, side, f"skipped: {made.reason}", candidate.rule
            )
            return
        retest = self._config.scalper.retest
        level = retest_level(candidate, retest.pullback_r)
        slot.retest = RetestWatch(
            candidate=replace(candidate, retest_level=level),
            level=level,
            target=made.plan.take_profit_1,
            tolerance=retest.tolerance_atr * candidate.atr,
            deadline=candidate.time + dt.timedelta(minutes=retest.window(slot.name)),
        )
        slot.decision = Decision(
            candidate.time,
            side,
            f"waiting for a retest of {level:.2f} until {slot.retest.deadline:%H:%M} UTC",
            candidate.rule,
        )
        log.info("scalp waits for its retest", mode=slot.name, level=round(level, 2))

    def _retest_tick(
        self, slot: ModeSlot, opened: dt.datetime, high: float, low: float, close: float
    ) -> Candidate | None:
        """The candidate entered on its retest, when this bar confirmed it; else None."""
        watch = slot.retest
        if watch is None or opened < watch.candidate.time:
            return None
        outcome = check(watch, opened, high, low, close)
        if outcome is None:
            return None
        slot.retest = None
        side = "BUY" if watch.long else "SELL"
        if outcome == CONFIRMED:
            return confirmed(watch.candidate, close, opened + MINUTE)
        why = (
            f"price crossed the stop before the retest of {watch.level:.2f}"
            if outcome == FAILED
            else f"no retest of {watch.level:.2f} (it ran to TP1 or the window ended)"
        )
        slot.decision = Decision(opened + MINUTE, side, f"skipped: {why}", watch.candidate.rule)
        log.info("scalp retest " + outcome, mode=slot.name, level=round(watch.level, 2))
        return None

    def _grade(
        self, slot: ModeSlot, candidate: Candidate, context: ScalpContext, now: dt.datetime
    ) -> Grade:
        """A+, A or B (or none) for a scalp, from the main setups, bias, levels, news."""
        graded = (context.graded or {}).get(slot.name, [])
        _grade, window = self._filter(slot.name)
        backing, setup = backing_grade(candidate.direction, graded, self.path, now, window)
        level = (
            candidate.retest_level if candidate.retest_level is not None else candidate.reference
        )
        return grade_scalp(
            candidate.direction,
            backing=backing,
            backing_label=setup.label if setup is not None else "",
            analysis=context.analysis or Analysis(),
            level=level,
            level_kind=getattr(candidate.source, "level_kind", None),
            atr=candidate.atr,
        )

    # -- S10: the setup is done; grid / averaging down -------------------------------- #

    def _setup_done(self, slot: ModeSlot, now: dt.datetime) -> list[str]:
        """Close every scalp (and add) of ``slot`` whose setup reached its last target:
        the move is done; the mode waits for the next setup (S10, D116)."""
        if not self._config.scalper.recovery.close_when_setup_done:
            return []
        texts: list[str] = []
        tick = None
        for executor in slot.executors:
            ids = {p["id"] for p in executor.open_positions}
            ids |= {plan.id for plan in executor.pending_orders}
            for plan_id in sorted(ids):
                setup = self.follows.get(plan_id)
                if setup is None or not target_reached(setup, self.path):
                    continue
                if tick is None:
                    try:
                        tick = self._broker.get_tick(self.spec.name)
                    except BrokerError as exc:
                        log.error(
                            "scalp not closed at its setup's target: no quote", error=str(exc)
                        )
                        return texts
                detail = f"the {setup.label} setup reached its target"
                log.info("scalp closed: setup done", id=plan_id, setup=setup.id)
                events = executor.close_plan(now, tick.bid, tick.ask, plan_id, detail)
                texts += self._report(slot, executor, events)
        return texts

    def _add_if_due(
        self,
        slot: ModeSlot,
        context: ScalpContext,
        close: float,
        bar_close: dt.datetime,
        now: dt.datetime,
    ) -> list[str]:
        """Grid / averaging down (S10, D116): an add to an open scalp of ``slot`` whose
        first stop still stands, once a bar closes far enough against it."""
        recovery = self._config.scalper.recovery
        if not recovery.average_down.enabled or not context.may_open:
            return []
        demo = context.on_demo(slot.name)
        executor = slot.target(demo)
        if not self.recovery_on(slot, executor):
            return []
        texts: list[str] = []
        for position in list(executor.open_positions):
            primary = str(position["id"])
            plan = None if is_add(primary) else executor.plan_for(primary)
            if plan is None:
                continue
            scalp = OpenScalp(plan, float(position["fill_price"]), float(position["stop"]))
            done = self.adds.get(primary, [])
            if not add_due(scalp, close, len(done), recovery.average_down):
                continue
            _grade, window = self._filter(slot.name)
            setup = self.follows.get(primary)
            if setup is not None and ended(setup, self.path, now, window) is not None:
                continue  # the setup is no longer live: no more risk for it
            base = plan.risk_pct or self._config.scalper.risk.risk_per_scalp_pct
            pct = recovery.average_down.add_risk_pct
            if pct > idea_room(base, done, recovery.max_idea_risk_pct) + _EPSILON:
                continue
            if pct > self._budget(demo, now, executor) + _EPSILON:
                continue
            number = len(done) + 1
            level = recovery.average_down.add_at_r[len(done)]
            add = add_plan(
                scalp,
                number,
                close=close,
                time=bar_close,
                expires_at=bar_close + MINUTE,
                risk_pct=pct,
                level_r=level,
            )
            if add is None:
                continue
            event = self._submit(slot, executor, add, context, now, demo=demo)
            if event is None:
                continue
            self.adds[primary] = [*done, add.risk_pct or pct]
            if setup is not None:
                self.follows[add.id] = setup
            self._save_follows()
            reason = (
                f"add {number} of {len(recovery.average_down.add_at_r)} to {primary}: "
                f"{level:g} R against (grid / averaging down), same stop, TP at its TP1"
            )
            texts.append(messages.scalp_placed(add, event, slot.name, slot.where(executor), reason))
        return texts

    def _submit(
        self,
        slot: ModeSlot,
        executor: Executor,
        plan: TradePlan,
        context: ScalpContext,
        now: dt.datetime,
        *,
        demo: bool,
    ) -> BookEvent | None:
        """Guard (on the demo account) and send an add; None when it was not placed."""
        try:
            spread = float(self._broker.spread_points(self.spec.name))
        except BrokerError:
            return None
        if not self._guard_allows(slot, plan, context, now, demo=demo):
            return None
        median = self._db.median_spread(
            self.spec.name, since=now - dt.timedelta(days=MEDIAN_SPREAD_DAYS), min_samples=30
        )
        event = executor.submit(
            plan, spread_points=spread, median_spread_points=median, events=context.events
        )
        if event.kind is not EventKind.PLACED:
            log.info("add not placed", id=plan.id, detail=event.detail)
            return None
        return event

    def _guard_allows(
        self,
        slot: ModeSlot,
        plan: TradePlan,
        context: ScalpContext,
        now: dt.datetime,
        *,
        demo: bool,
    ) -> bool:
        reason = self._guard_refusal(slot, plan, context, now, demo=demo)
        return reason is None

    def _guard_refusal(
        self,
        slot: ModeSlot,
        plan: TradePlan,
        context: ScalpContext,
        now: dt.datetime,
        *,
        demo: bool,
    ) -> str | None:
        """The account guard on the demo account (both strategies' open risk, no hedge
        against the main strategy), or the same open-risk ceiling on paper."""
        limits = self._config.scalper.risk
        pct = plan.risk_pct or limits.risk_per_scalp_pct
        hedge = self._config.scalper.recovery.hedge_modes and self.recovery_on(
            slot, slot.target(demo)
        )
        if demo:
            if self._guard is None or context.main_exposures is None:
                return None
            mine = [x for e in self.demo_executors for x in self.exposures(e)]
            decision = self._guard.check(
                now,
                equity=context.equity,
                direction=plan.direction,
                new_risk=context.equity * pct / 100.0,
                exposures=[*context.main_exposures, *mine],
                hedge_exposures=list(context.main_exposures) if hedge else None,
            )
            if decision.allowed:
                return None
            return decision.reasons[0] if decision.reasons else "the account guard"
        equity = slot.paper.equity
        value = self.spec.value_per_price_unit
        held = sum(open_risk(p, value) for s in self.slots.values() for p in s.paper.open_positions)
        ceiling = limits.max_open_risk_pct / 100.0 * equity
        if held + equity * pct / 100.0 > ceiling + _EPSILON:
            return (
                f"open scalper risk would pass {limits.max_open_risk_pct:g}% of equity "
                f"({held:.2f} already open)"
            )
        return None

    def _today_rows(self, demo: bool, now: dt.datetime) -> list[TradeRow]:
        boundary = self._config.strategy.liquidity.day_boundary
        table = SCALP_LIVE_TABLE if demo else SCALP_PAPER_TABLE
        today = trading_day_of(now, boundary)
        rows = trades_between(self._db, table, now - dt.timedelta(days=2), now + MINUTE)
        return [row for row in rows if trading_day_of(row.exit_time, boundary) == today]

    def _budget(self, demo: bool, now: dt.datetime, executor: Executor) -> float:
        """What the day's scalper loss limit still lets a new scalp risk, in %."""
        limits = self._config.scalper.risk
        today_net = sum(row.net_pnl for row in self._today_rows(demo, now))
        value = self.spec.value_per_price_unit
        account = [s.demo if demo else s.paper for s in self.slots.values()]
        held = sum(open_risk(p, value) for e in account if e is not None for p in e.open_positions)
        return budget_pct(limits.daily_loss_limit_pct, today_net, held, executor.equity)

    def _risk_pct(
        self, slot: ModeSlot, executor: Executor, demo: bool, now: dt.datetime
    ) -> tuple[float, list[str]]:
        """A new scalp's risk in % and why: the base, a martingale step (S10), never more
        than the day's loss limit leaves."""
        limits = self._config.scalper.risk
        pct, notes = limits.risk_per_scalp_pct, []
        if self.recovery_on(slot, executor):
            ideas: dict[str, float] = {}
            for row in sorted(self._today_rows(demo, now), key=lambda r: r.exit_time):
                if mode_of(row.id) == slot.name:
                    key = primary_of(row.id)
                    ideas[key] = ideas.get(key, 0.0) + row.net_pnl
            martingale = self._config.scalper.recovery.martingale
            pct, step = martingale_risk(pct, list(ideas.values()), martingale)
            if step:
                notes.append(f"martingale step {step} after a loss")
        budget = self._budget(demo, now, executor)
        if budget < pct:
            pct = max(0.0, budget)
            notes.append("sized to what the daily loss limit leaves")
        return pct, notes

    def _consider(
        self, slot: ModeSlot, candidate: Candidate, context: ScalpContext, now: dt.datetime
    ) -> str | None:
        """Every gate, then the order. Returns the message for a placed scalp; the
        outcome is kept in ``slot.decision`` either way (the chart's Quick Scalp line)."""
        side = "BUY" if candidate.direction is Direction.LONG else "SELL"

        def skip(reason: str) -> None:
            slot.decision = Decision(candidate.time, side, f"skipped: {reason}", candidate.rule)
            return None

        if not context.may_open:
            return skip("the scalper may not trade now (/mode, pause or mode not built)")
        if now - candidate.time > STALE_AFTER:  # the job ran late: the moment has passed
            log.info("scalp skipped: the setup is stale", age=str(now - candidate.time))
            return skip("the setup is stale")
        demo = context.on_demo(slot.name)
        executor = slot.target(demo)
        recovery = self._config.scalper.recovery
        recovery_on = self.recovery_on(slot, executor)
        notes: list[str] = []
        if self.busy(executor):
            if not (slot.name == HFT and recovery_on and recovery.hft_stack.enabled):
                return skip("a scalp of this mode is already open")
            refusal = stack_refusal(
                executor.open_positions,
                len(executor.pending_orders),
                candidate.direction,
                recovery.hft_stack,
            )
            if refusal is not None:
                return skip(refusal)
            count = len(executor.open_positions) + 1
            notes.append(f"stacked: HFT scalp {count} of {recovery.hft_stack.max_open}")
        gate = slot.logic.gate(candidate)
        if not gate.passed:
            log.info("scalp skipped", mode=slot.name, reason=gate.reason)
            return skip(gate.reason or "the mode's filters")
        setups = context.setups_for(slot.name)
        following: GradedSetup | None = None
        if setups is not None:
            grade, window = self._filter(slot.name)
            refusal = hft_permission(
                candidate.direction,
                setups,
                self.path,
                now,
                window,
                GRADE_NAMES.get(grade, "A or A+"),
                LABELS.get(slot.name, slot.name),
            )
            if refusal is not None:
                log.info("scalp skipped", mode=slot.name, reason=refusal)
                return skip(refusal)
            following = followed(candidate.direction, setups, self.path, now, window)
            if following is not None:
                notes.append(f"follows the {following.label} setup")
        stopped = slot.stopped_out.get(candidate.direction)
        cooldown = dt.timedelta(seconds=self._config.scalper.risk.cooldown_sec)
        if stopped is not None and candidate.time - stopped < cooldown:
            return skip("cooldown after a stop-out")
        graded = self._grade(slot, candidate, context, now)
        lowest = self._config.scalper.signal_min_grade
        if not graded.at_least(lowest):
            letter = graded.letter or "below B"
            return skip(f"graded {letter}: {'; '.join(graded.reasons)}")
        shared = self.shared_refusal(
            slot,
            candidate.direction,
            demo=demo,
            now=now,
            hedge=recovery_on and recovery.hedge_modes,
        )
        if shared is not None:
            log.info("scalp skipped", mode=slot.name, reason=shared)
            return skip(shared)
        pct, sizing = self._risk_pct(slot, executor, demo, now)
        pct = round(pct, 4)  # what a plan carries
        if pct <= 0:
            return skip("the daily loss limit leaves no room for another scalp")
        notes += sizing
        try:
            spread = float(self._broker.spread_points(self.spec.name))
        except BrokerError as exc:
            log.warning("scalp skipped: no spread", error=str(exc))
            return skip("no spread from the broker")
        median = self._db.median_spread(
            self.spec.name, since=now - dt.timedelta(days=MEDIAN_SPREAD_DAYS), min_samples=30
        )
        costs = LadderCosts.of(self.spec, self._config.backtest.costs, spread)
        made = slot.logic.plan(
            candidate, costs, plan_id=self._next_id(slot, now), session=gate.session
        )
        if made.plan is None:
            log.info("scalp skipped", mode=slot.name, reason=made.reason)
            return skip(made.reason)
        plan = replace(made.plan, risk_pct=pct)
        refused = self._guard_refusal(slot, plan, context, now, demo=demo)
        if refused is not None:
            log.info("scalp refused by the open-risk rules", reason=refused)
            return skip(refused)
        event = executor.submit(
            plan, spread_points=spread, median_spread_points=median, events=context.events
        )
        if event.kind is not EventKind.PLACED:
            log.info("scalp not placed", id=plan.id, detail=event.detail)
            return skip(event.detail or "not placed")
        self._count_id(plan.id)
        if following is not None:
            self.follows[plan.id] = following
            self._save_follows()
        where = slot.where(executor)
        log.info("scalp placed", id=plan.id, where=where, rule=candidate.rule, risk_pct=pct)
        if self._config.execution.signals_only:
            slot.decision = Decision(
                candidate.time, side, f"signal {graded.letter} sent: {plan.id}", candidate.rule
            )
            equity = context.equity or executor.equity
            size = position_size(equity, plan.entry, plan.stop_loss, self.spec, pct)
            return messages.scalp_signal(
                plan,
                slot.name,
                graded,
                context.analysis or Analysis(),
                reason=candidate.reason,
                retest=candidate.retest_level if candidate.retested else None,
                lots=size.lots if size.ok else None,
                valid_min=self._config.scalper.signal_valid_min,
            )
        slot.decision = Decision(
            candidate.time, side, f"placed {plan.id} ({where})", candidate.rule
        )
        reason = candidate.reason + (f"\n{'; '.join(notes)}" if notes else "")
        return messages.scalp_placed(plan, event, slot.name, where, reason)

    def shared_refusal(
        self,
        slot: ModeSlot,
        direction: Direction,
        *,
        demo: bool,
        now: dt.datetime,
        hedge: bool = False,
    ) -> str | None:
        """The rules all modes share in one account (S9, D115), or None when they pass.

        The account is the demo account (every mode's demo executor) or paper (every
        mode's paper book). No hedging (unless ``hedge``: the owner's capped hedging
        between modes, S10), no stacking unless risk-free, and one set of daily limits
        for every mode together.
        """
        for other in self.slots.values():
            if other is slot:
                continue
            executor = other.demo if demo else other.paper
            if executor is None:
                continue
            name = other.name
            for position in executor.open_positions:
                way = Direction(position["direction"])
                if way is not direction:
                    if hedge:
                        continue
                    return f"the {name} scalp is open the other way (no hedging)"
                if not risk_free(position, way):
                    return f"the {name} scalp this way is not risk-free yet (no stacking)"
            for plan in executor.pending_orders:
                if plan.direction is not direction:
                    if hedge:
                        continue
                    return f"the {name} scalp is waiting the other way (no hedging)"
                return f"the {name} scalp this way is still waiting (no stacking)"
        target = slot.target(demo)
        return self._shared_limits(demo, now, float(getattr(target, "equity", 0.0)))

    def _shared_limits(self, demo: bool, now: dt.datetime, equity: float = 0.0) -> str | None:
        """The scalper's daily limits counted over every mode in the account; a scalp
        and its adds count as one (S10)."""
        limits = self._config.scalper.risk
        table = SCALP_LIVE_TABLE if demo else SCALP_PAPER_TABLE
        rows = trades_between(self._db, table, now - SHARED_LOOKBACK, now + MINUTE)
        ideas: dict[str, tuple[dt.datetime, float]] = {}
        for row in rows:
            key = primary_of(row.id)
            last, net = ideas.get(key, (row.exit_time, 0.0))
            ideas[key] = (max(last, row.exit_time), net + row.net_pnl)
        paused_until: dt.datetime | None = None
        in_a_row = 0
        for exit_time, net in sorted(ideas.values()):  # as the risk manager counts
            if net < 0:
                in_a_row += 1
                if in_a_row >= limits.max_consecutive_losses:
                    paused_until, in_a_row = exit_time + PAUSE_AFTER_LOSSES, 0
            else:
                in_a_row = 0
        if paused_until is not None and now < paused_until:
            return (
                f"{limits.max_consecutive_losses} losses in a row across the modes: "
                f"paused until {paused_until:%Y-%m-%d %H:%M} UTC"
            )
        boundary = self._config.strategy.liquidity.day_boundary
        today = trading_day_of(now, boundary)
        day = [row for row in rows if trading_day_of(row.exit_time, boundary) == today]
        net_today = sum(row.net_pnl for row in day)
        limit = limits.daily_loss_limit_pct / 100.0 * equity
        if equity > 0 and net_today <= -limit + _EPSILON:
            return (
                f"the daily loss limit across the modes ({net_today:+.2f} of "
                f"-{limit:.2f}, {limits.daily_loss_limit_pct:g}%)"
            )
        executors = [s.demo if demo else s.paper for s in self.slots.values()]
        opened = sum(
            1 for e in executors if e is not None for p in e.open_positions if not is_add(p["id"])
        )
        scalps = len({primary_of(row.id) for row in day})
        if scalps + opened >= limits.max_scalps_per_day:
            return f"{limits.max_scalps_per_day} scalps today across the modes (the daily maximum)"
        return None

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

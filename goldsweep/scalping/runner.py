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
account): no scalp against another mode's open scalp (no hedging); a second scalp the
same way only when every scalp open that way is risk-free, its stop at or past its
entry (no stacking); and one set of daily limits for all modes: the scalper's daily
loss in R, its scalps a day, and the pause after losses in a row.

Each mode has a **paper** book (its own notional balance) and, on a live bot, a
**demo** executor on the same MT5 account with the mode's own magic number; an
approved mode (``/approve scalper <mode>``) sends its scalps there. Messages are
quiet (D97): one when a scalp is placed, one when it closes.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
from goldsweep.risk.risk_manager import PAUSE_AFTER_LOSSES, NewsEvent
from goldsweep.scalping.grade_filter import GRADE_NAMES, GradedSetup, PathBar, hft_permission
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
            self.path.append(PathBar(start, h, low))
            for slot in self.slots.values():
                candidates = slot.logic.on_bar(start, o, h, low, c, float(volumes[i]))
                if not warmup or i == newest:  # a (re)start replays no history into the books
                    for executor in slot.executors:
                        events = executor.on_bar(bar, line=slot.logic.line)
                        texts += self._report(slot, executor, events)
                if warmup or i != newest or slot.name not in context.selected:
                    continue
                if not candidates:
                    slot.decision = Decision(start + MINUTE, "NONE", "no setup on the last bar")
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
        if self.busy(executor):
            return skip("a scalp of this mode is already open")
        gate = slot.logic.gate(candidate)
        if not gate.passed:
            log.info("scalp skipped", mode=slot.name, reason=gate.reason)
            return skip(gate.reason or "the mode's filters")
        if slot.name == HFT and context.hft_setups is not None:
            hft = self._config.scalper.hft
            refusal = hft_permission(
                candidate.direction,
                context.hft_setups,
                self.path,
                now,
                dt.timedelta(minutes=hft.a_window_min),
                GRADE_NAMES.get(hft.grade_filter, "A or A+"),
            )
            if refusal is not None:
                log.info("scalp skipped", mode=slot.name, reason=refusal)
                return skip(refusal)
        stopped = slot.stopped_out.get(candidate.direction)
        cooldown = dt.timedelta(seconds=self._config.scalper.risk.cooldown_sec)
        if stopped is not None and candidate.time - stopped < cooldown:
            return skip("cooldown after a stop-out")
        shared = self.shared_refusal(slot, candidate.direction, demo=demo, now=now)
        if shared is not None:
            log.info("scalp skipped", mode=slot.name, reason=shared)
            return skip(shared)
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
        plan = made.plan
        if self._guard is not None and context.main_exposures is not None and demo:
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
                return skip(decision.reasons[0] if decision.reasons else "the account guard")
        event = executor.submit(
            plan, spread_points=spread, median_spread_points=median, events=context.events
        )
        if event.kind is not EventKind.PLACED:
            log.info("scalp not placed", id=plan.id, detail=event.detail)
            return skip(event.detail or "not placed")
        self._count_id(plan.id)
        where = slot.where(executor)
        log.info("scalp placed", id=plan.id, where=where, rule=candidate.rule)
        slot.decision = Decision(
            candidate.time, side, f"placed {plan.id} ({where})", candidate.rule
        )
        return messages.scalp_placed(plan, event, slot.name, where, candidate.reason)

    def shared_refusal(
        self, slot: ModeSlot, direction: Direction, *, demo: bool, now: dt.datetime
    ) -> str | None:
        """The rules all modes share in one account (S9, D115), or None when they pass.

        The account is the demo account (every mode's demo executor) or paper (every
        mode's paper book). No hedging, no stacking unless risk-free, and one set of
        daily limits for every mode together.
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
                    return f"the {name} scalp is open the other way (no hedging)"
                if not risk_free(position, way):
                    return f"the {name} scalp this way is not risk-free yet (no stacking)"
            for plan in executor.pending_orders:
                if plan.direction is not direction:
                    return f"the {name} scalp is waiting the other way (no hedging)"
                return f"the {name} scalp this way is still waiting (no stacking)"
        return self._shared_limits(demo, now)

    def _shared_limits(self, demo: bool, now: dt.datetime) -> str | None:
        """The scalper's daily limits counted over every mode in the account."""
        limits = self._config.scalper.risk
        table = SCALP_LIVE_TABLE if demo else SCALP_PAPER_TABLE
        rows = sorted(
            trades_between(self._db, table, now - SHARED_LOOKBACK, now + MINUTE),
            key=lambda row: row.exit_time,
        )
        paused_until: dt.datetime | None = None
        in_a_row = 0
        for row in rows:  # as the risk manager counts, over every mode together
            if row.net_pnl < 0:
                in_a_row += 1
                if in_a_row >= limits.max_consecutive_losses:
                    paused_until, in_a_row = row.exit_time + PAUSE_AFTER_LOSSES, 0
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
        limit_r = limits.daily_loss_limit_pct / limits.risk_per_scalp_pct
        total_r = sum(row.r_multiple for row in day)
        if total_r <= -limit_r + _EPSILON:
            return f"the daily loss limit across the modes ({total_r:+.2f} R of -{limit_r:g} R)"
        executors = [s.demo if demo else s.paper for s in self.slots.values()]
        opened = sum(len(e.open_positions) for e in executors if e is not None)
        if len(day) + opened >= limits.max_scalps_per_day:
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

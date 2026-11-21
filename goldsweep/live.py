"""The trading loop: ``python main.py --paper`` and ``python main.py --live`` (spec 7b).

On every closed M5 bar (read a couple of seconds after the close):

1. the new bar is applied to the orders and positions (fills, stops, targets,
   breakeven, trailing): on paper by the backtester's own trade book, live by
   the MT5 executor, which follows the broker and moves real stops;
2. the signal engine looks at the closed bars, with the news bias and the
   post-news windows;
3. each signal is numbered, graded, stored and judged: when it meets its
   session's playbook rule (D86) and clears the auto-trade threshold it becomes
   an order (if the risk rules allow), at or above the alert threshold it is an
   alert, below that it is only stored. A setup that meets the session's A+
   candidate, not yet approved, is also traded on the shadow book (paper only,
   D87); once enough of those have finished with a positive average, the owner
   is asked to send ``/approve <session>``;
4. ``signals.json`` is rewritten for the on-chart panel.

Alongside run the news refresh (calendar, headlines, AI classification), the
pre-news warnings and breakeven moves, the scenario briefs, the Telegram
commands (/status /pause /resume /stop /golive /approve /revoke) and the daily
summary at 21:00 New York time. LIVE also compares orders and positions with the broker every few
seconds, so fills, targets, stops and breakeven moves are reported at once.
Every step is serialised by one lock: the MetaTrader 5 Python API is not
thread-safe.

PAPER never sends an order. LIVE needs ``MODE=LIVE`` in ``.env``, ``mode: LIVE``
in config.yaml and the account number typed at startup; Phase 6 trades demo
accounts only unless ``execution.allow_real_account`` is set.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

import pandas as pd

from goldsweep.ai.news_analyzer import NewsAnalyzer
from goldsweep.ai.news_bias import NewsBias, current_news_bias
from goldsweep.ai.scenarios import ScenarioBuilder, build_context, due_triggers
from goldsweep.alerts import messages
from goldsweep.alerts.telegram import Command, Notifier, TelegramBot, make_notifier
from goldsweep.analysis.bias import compute_bias
from goldsweep.analysis.context import ContextFrames, with_context
from goldsweep.analysis.grades import (
    PLAYBOOK_SESSIONS,
    describe_rule,
    grade,
    meets,
    parse_playbook,
    playbook_name,
    playbook_session,
)
from goldsweep.analysis.indicators import latest_atr
from goldsweep.analysis.pressure import STRUCTURE_BARS, PressureReading, pressure_meter
from goldsweep.analysis.refine import (
    CONTEXT_BARS,
    RefineResult,
    RefineSetup,
    RefineStatus,
    refine,
)
from goldsweep.analysis.signals import MODELS, Signal, SignalEngine
from goldsweep.analysis.smt import with_smt
from goldsweep.backtest.engine import spread_points as cost_spreads
from goldsweep.backtest.simulator import TrailLevels
from goldsweep.core.clock import UTC, Clock, SessionCalculator, SystemClock, ensure_utc
from goldsweep.core.logging import get_logger, setup_logging, shutdown_logging
from goldsweep.core.settings import PlaybookRule, Settings
from goldsweep.core.types import Mode, Timeframe
from goldsweep.data.broker import BrokerClient, BrokerError
from goldsweep.data.calendar import CalendarService, EconomicEvent, is_blackout_event
from goldsweep.data.factory import create_broker
from goldsweep.data.news import NewsService
from goldsweep.execution.book import Bar, BookEvent, EventKind, TradePlan, refined_plan, to_ns
from goldsweep.execution.executor import Executor, PaperExecutor
from goldsweep.execution.panel import PanelWriter
from goldsweep.execution.shadow import ShadowBook, Tally, fingerprint
from goldsweep.risk.risk_manager import NewsEvent
from goldsweep.scalping import modes as scalper_modes
from goldsweep.scalping.modes import MODE_NAMES
from goldsweep.scalping.ticks import TickRecorder, tick_folder
from goldsweep.storage.db import Database

log = get_logger(__name__)

ACTIVE, PAUSED, STOPPED = "active", "paused", "stopped"
STATE_WARNED = "alerts.warned"
#: What the dashboard reads (Phase 7): the latest market and trading picture, and
#: proof that the bot is alive.
SNAPSHOT_KEY = "dashboard.snapshot"
HEARTBEAT_KEY = "dashboard.heartbeat"
HEARTBEAT_EVERY = dt.timedelta(seconds=10)
#: Dashboard commands older than this are not carried out (the bot was not running).
COMMAND_MAX_AGE = dt.timedelta(minutes=2)
#: The strategy and scalper-mode switch (D103), as "name:argument" for the dashboard.
SWITCH_COMMANDS = (
    "mode:main",
    "mode:scalper",
    "mode:both",
    *(f"scalper:{mode}" for mode in ("off", *MODE_NAMES)),
)
DASHBOARD_COMMANDS = ("pause", "resume", "stop", "status", *SWITCH_COMMANDS)
#: M5 bars kept in SQLite for the dashboard chart.
CANDLES_KEPT = 300
DUPLICATE_WINDOW = dt.timedelta(hours=12)
#: The news job runs every minute: waiting entries are withdrawn when a blackout
#: begins within the next minute, so none can fill inside it.
WITHDRAW_LEAD = dt.timedelta(minutes=1)
#: M1 refinement (D83): bars read before the oldest armed setup, the most read at
#: once, and how long after its window a setup may wait for the deciding bar.
REFINE_CONTEXT_BARS = CONTEXT_BARS
REFINE_MAX_BARS = 2000
REFINE_GRACE = dt.timedelta(minutes=5)
#: H1 bars read for the trend and structure context tags (D86): enough for the
#: 200 EMA, the daily structure and the previous week.
CONTEXT_HOURS = 2000
#: Session playbooks (D87): the candidates the owner approved ({playbook: rule
#: fingerprint}) and those whose approval was already asked for. Shared by paper and
#: live, like the shadow book: monitoring never trades, and an approval is the owner's
#: decision about a strategy, not about one mode.
APPROVED_KEY = "playbooks.approved"
ASKED_KEY = "playbooks.asked"
#: The switch (D103): which strategies may trade and the scalper's mode, as changed
#: on Telegram or the dashboard (config.yaml ``switch:`` until then); and the scalper
#: modes the owner approved for trading. Shared by paper and live, like approvals.
STRATEGIES_KEY = "switch.strategies"
SCALPER_MODE_KEY = "switch.scalper_mode"
SCALPER_APPROVED_KEY = "scalper.approved"
STRATEGY_CHOICES = ("main", "scalper", "both")
MEDIAN_SPREAD_MIN_SAMPLES = 30
EXIT_OK, EXIT_BROKER, EXIT_REFUSED, EXIT_NOT_CONFIRMED = 0, 1, 2, 3


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #


class NewsDesk:
    """Calendar, headlines, classifier and scenario writer, kept fresh by the loop."""

    def __init__(
        self,
        settings: Settings,
        calendar: CalendarService,
        news: NewsService,
        analyzer: NewsAnalyzer,
        scenarios: ScenarioBuilder,
    ) -> None:
        self._config = settings.config.news
        self.calendar = calendar
        self.news = news
        self.analyzer = analyzer
        self.scenarios = scenarios

    @classmethod
    def from_settings(
        cls, settings: Settings, db: Database, *, clock: Clock, use_llm: bool = True
    ) -> NewsDesk:
        from goldsweep.ai.llm import make_client, setup_problem
        from goldsweep.news_report import build_calendar, build_news

        calendar, _notes = build_calendar(settings, db, clock=clock)
        news, _notes = build_news(settings, db, clock=clock)
        client = make_client(settings) if use_llm else None
        note = setup_problem(settings) if use_llm else "--no-llm"
        return cls(
            settings,
            calendar,
            news,
            NewsAnalyzer(settings.config.news, db, client=client, clock=clock, setup_note=note),
            ScenarioBuilder(settings.config.news, db, client=client, clock=clock),
        )

    def refresh(self, now: dt.datetime) -> None:
        self.calendar.refresh(now)
        self.news.poll(now)
        self.analyzer.classify_pending(self.news.pending(now), now)

    def events(self, now: dt.datetime) -> list[EconomicEvent]:
        return self.calendar.events(now - dt.timedelta(days=1), now + dt.timedelta(days=2))

    def bias(self, now: dt.datetime) -> NewsBias:
        return current_news_bias(
            self.analyzer.usage._db,  # noqa: SLF001 - the same database
            self.events(now),
            now,
            self._config.headline_half_life_min,
            self._config.blackout_currencies,
        )

    def blackouts(self, now: dt.datetime) -> list[NewsEvent]:
        return self.calendar.blackout_events(now - dt.timedelta(days=1), now + dt.timedelta(days=2))

    def windows(self, now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
        return self.calendar.post_news_windows(
            now - dt.timedelta(days=1), now + dt.timedelta(days=1)
        )

    def next_event(self, now: dt.datetime) -> EconomicEvent | None:
        return self.calendar.next_blackout_event(now)


# --------------------------------------------------------------------------- #
# The trader
# --------------------------------------------------------------------------- #


class Trader:
    """Everything the loop does, as plain methods the scheduler (or a test) calls.

    Without an ``executor`` it trades on paper (PAPER mode only); the live loop
    passes an :class:`~goldsweep.execution.live_executor.MT5Executor`.
    """

    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        db: Database,
        notifier: Notifier,
        *,
        clock: Clock | None = None,
        news: NewsDesk | None = None,
        bot: TelegramBot | None = None,
        starting_equity: float | None = None,
        executor: Executor | None = None,
        panel: PanelWriter | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.config
        self._broker = broker
        self._db = db
        self._notifier = notifier
        self._clock = clock or SystemClock()
        self._news = news
        self._bot = bot
        self._panel = panel
        self._lock = threading.RLock()
        self._sessions = SessionCalculator(self._config.sessions)
        if executor is None and settings.mode is not Mode.PAPER:
            raise RuntimeError("the paper trader only runs in PAPER mode")
        self.spec = broker.resolve_symbol(self._config.symbol_candidates)
        if executor is None:
            equity = starting_equity
            if equity is None:
                equity = broker.account_info().equity
            executor = PaperExecutor(settings, self.spec, db, starting_equity=equity)
        self.executor = executor
        self.label = executor.label
        self.live = executor.mode is Mode.LIVE
        prefix = "live" if self.live else "paper"
        self._state_last_bar = f"{prefix}.last_bar"
        self._state_trading = f"{prefix}.trading"
        #: Setups waiting for their M1 turn (D83), saved so a restart carries on.
        self._state_refining = f"{prefix}.refining"
        #: The spec's model; it always runs, because the dashboard and the scenario
        #: briefs read its pools, levels and bias (its signals count only if enabled).
        self.engine = SignalEngine.from_settings(
            settings, point=self.spec.point, symbol=self.spec.name
        )
        #: The other setup models that are enabled (D85), each its own engine.
        self._extra_engines = {
            name: SignalEngine.from_settings(
                settings, point=self.spec.point, symbol=self.spec.name, model=MODELS[name]
            )
            for name in MODELS
            if name != "sweep_mss" and getattr(self._config.models, name).enabled
        }
        self._silver = self._resolve_silver(broker)
        #: Monitored A+ setups, traded on paper only (D87).
        self._shadow = ShadowBook(settings, self.spec, db)
        #: Every gold tick, saved for testing the scalper's tick mode (D106).
        ticks = self._config.ticks
        self.ticks = (
            TickRecorder(
                broker,
                self.spec.name,
                tick_folder(settings.data_dir, self.spec.name),
                db,
                self._clock,
                keep_days=ticks.keep_days,
            )
            if ticks.record
            else None
        )
        saved = db.get_state(self._state_last_bar)
        self._last_bar = dt.datetime.fromisoformat(saved) if saved else None
        self._market: dict[str, Any] = {}
        self._snapshot_market: dict[str, Any] = {}
        self._last_heartbeat: dt.datetime | None = None

    # -- setup models and confluence (D85) ------------------------------------------ #

    def _engines(self) -> list[tuple[str, SignalEngine]]:
        return [("sweep_mss", self.engine), *self._extra_engines.items()]

    def _resolve_silver(self, broker: BrokerClient) -> str | None:
        """The broker's silver symbol for the SMT tag, or None (the tag is then left out)."""
        smt = self._config.confluence.smt
        if not smt.enabled:
            return None
        try:
            return broker.resolve_symbol(smt.symbol_candidates).name
        except BrokerError as exc:
            log.warning("no silver symbol: the SMT tag is left out", error=str(exc))
            return None

    def _with_smt(self, signals: list[Signal], trigger: pd.DataFrame) -> list[Signal]:
        if not signals or self._silver is None:
            return signals
        smt = self._config.confluence.smt
        tf = self._config.timeframes.trigger
        try:
            silver = self._broker.get_rates(self._silver, tf, len(trigger))
        except BrokerError as exc:
            log.warning("silver bars not available: the SMT tag is left out", error=str(exc))
            return signals
        return [
            with_smt(s, trigger, silver, lookback_bars=smt.lookback_bars, bar=tf.duration)
            for s in signals
        ]

    def _with_context(self, signals: list[Signal]) -> list[Signal]:
        """Trend and structure tags (D86), from the broker's H1 bars."""
        if not signals:
            return signals
        try:
            hourly = self._broker.get_rates(self.spec.name, Timeframe.H1, CONTEXT_HOURS)
        except BrokerError as exc:
            log.warning("H1 bars not available: no context tags", error=str(exc))
            return signals
        boundary = self._config.strategy.liquidity.day_boundary
        frames = ContextFrames.from_hourly(hourly, boundary)
        return [with_context(s, frames) for s in signals]

    # -- state ------------------------------------------------------------------ #

    @property
    def trading(self) -> str:
        return str(self._db.get_state(self._state_trading, ACTIVE))

    # -- the switch (D103) ---------------------------------------------------------- #

    @property
    def strategies(self) -> str:
        """main / scalper / both: which strategies may open trades."""
        value = self._db.get_state(STRATEGIES_KEY)
        return str(value) if value in STRATEGY_CHOICES else self._config.switch.strategies

    @property
    def scalper_mode(self) -> str:
        """off / tick / intraday / swing."""
        value = self._db.get_state(SCALPER_MODE_KEY)
        if value == "off" or value in MODE_NAMES:
            return str(value)
        return self._config.switch.scalper_mode

    def _scalper_approved(self, mode: str) -> bool:
        return mode in self._db.get_state(SCALPER_APPROVED_KEY, [])

    def _set_strategies(self, argument: str) -> str:
        choice = argument.strip().lower()
        if not choice:
            return (
                f"Strategies that may trade: {self.strategies}. "
                "Send /mode main, /mode scalper or /mode both."
            )
        if choice not in STRATEGY_CHOICES:
            return "Send /mode main, /mode scalper or /mode both."
        self._db.set_state(STRATEGIES_KEY, choice)
        log.info("strategies switched", strategies=choice)
        if choice == "main":
            return "✅ Only the main strategy may open trades; the scalper does not trade."
        note = self._scalper_note()
        if choice == "scalper":
            return f"✅ Only the scalper may open trades; main setups are alerts only.{note}"
        return f"✅ Both strategies may open trades, side by side.{note}"

    def _scalper_note(self) -> str:
        mode = self.scalper_mode
        if mode == "off":
            return " The scalper's mode is off: send /scalper tick, intraday or swing."
        info = scalper_modes.REGISTRY[mode]
        if not info.built:
            return f" Its {mode} mode is not built yet (phase {info.phase}): nothing runs."
        if not (info.passed and self._scalper_approved(mode)):
            return f" Its {mode} mode runs on paper only until it passes and is approved."
        return ""

    def _set_scalper_mode(self, argument: str) -> str:
        choice = argument.strip().lower()
        if not choice:
            return (
                f"Scalper mode: {self._scalper_line()}. Send /scalper off, tick, intraday or swing."
            )
        if choice != "off" and choice not in MODE_NAMES:
            return "Send /scalper off, /scalper tick, /scalper intraday or /scalper swing."
        self._db.set_state(SCALPER_MODE_KEY, choice)
        log.info("scalper mode switched", mode=choice)
        if choice == "off":
            return "✅ Scalper off: no scalper mode runs."
        line = scalper_modes.describe(
            choice, selected=choice, approved=self._scalper_approved(choice)
        )
        if self.strategies == "main":
            line += ". The scalper may not trade while /mode is main"
        return f"✅ Scalper mode: {line}."

    def _scalper_line(self) -> str:
        mode = self.scalper_mode
        if mode == "off":
            return "off"
        return scalper_modes.describe(mode, selected=mode, approved=self._scalper_approved(mode))

    def _approve_scalper(self, mode: str) -> str:
        """``/approve scalper <mode>``: only a mode whose evidence test passed (D103)."""
        if mode not in MODE_NAMES:
            return "Send /approve scalper tick, intraday or swing."
        info = scalper_modes.REGISTRY[mode]
        if not info.built:
            return f"The {mode} mode is not built yet (phase {info.phase}): nothing to approve."
        if not info.passed:
            return (
                f"Not approved: the {mode} mode has not passed its evidence test, so it can "
                "only run on paper."
            )
        approved = list(self._db.get_state(SCALPER_APPROVED_KEY, []))
        if mode not in approved:
            approved.append(mode)
            self._db.set_state(SCALPER_APPROVED_KEY, approved)
        return f"✅ The scalper's {mode} mode is approved for trading when it is selected."

    def _set_trading(self, state: str) -> None:
        self._db.set_state(self._state_trading, state)

    def _send(self, text: str | None) -> None:
        """Deliver a message. A failed message is logged; it never stops the trading."""
        if not text:
            return
        try:
            self._notifier.send(text)
        except Exception as exc:  # noqa: BLE001 - alerts are best effort, trading is not
            log.error("alert not delivered", error=repr(exc), text=text[:200])

    def _report(self, events: Sequence[BookEvent]) -> None:
        for event in events:
            self._send(messages.trade_message(event, self.label))

    # -- each closed bar ----------------------------------------------------------- #

    def on_bar_close(self) -> bool:
        """Process the bars that closed since the last call.

        Returns ``False`` while the broker has not published the newest closed
        bar yet (the caller retries), ``True`` otherwise.
        """
        with self._lock:
            return self._on_bar_close()

    def _on_bar_close(self) -> bool:
        now = ensure_utc(self._clock.now())
        tf = self._config.timeframes
        symbol = self.spec.name
        step = tf.trigger.duration
        expected = pd.Timestamp(now).floor(tf.trigger.pandas_freq) - step
        trigger = self._broker.get_rates(symbol, tf.trigger, self.engine.lookback + 60)
        if trigger.empty or trigger.index[-1] < expected:
            return False
        newest = trigger.index[-1].to_pydatetime()
        if self._last_bar is not None and newest <= self._last_bar:
            return True
        liquidity = self._broker.get_rates(symbol, tf.liquidity, 500)
        hourly = self._broker.get_rates(symbol, tf.bias, 400)
        self._db.upsert_candles(symbol, tf.trigger, trigger.tail(CANDLES_KEPT))
        spread = float(self._broker.spread_points(symbol))
        self._db.record_spread(symbol, now, spread)
        median = self._db.median_spread(
            symbol,
            since=now - dt.timedelta(days=self._config.risk.spread_median_days),
            min_samples=MEDIAN_SPREAD_MIN_SAMPLES,
        )
        bias = self._news.bias(now) if self._news else None
        score = bias.score if bias is not None else 0.0
        windows = self._news.windows(now) if self._news else []

        # 1. The new bars move the orders and positions.
        spreads = cost_spreads(trigger, self._config.backtest.costs) * self.spec.point
        trail = TrailLevels.from_candles(trigger, tf.trigger, self._config.strategy.swing_lookback)
        first = (
            0
            if self._last_bar is None
            else int(trigger.index.searchsorted(pd.Timestamp(self._last_bar), side="right"))
        )
        if self._last_bar is None:
            first = len(trigger) - 1  # first start: no history replayed into the book
        for i in range(first, len(trigger)):
            row = trigger.iloc[i]
            opened = to_ns(trigger.index[i].to_pydatetime())
            bar = Bar(
                opened,
                opened + int(step.total_seconds() * 1e9),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(spreads[i]),
            )
            self._report(self.executor.on_bar(bar, trail))
            self._monitor_bar(bar, trail)

        # 2. Each model's engine looks for setups that completed at this close (D85).
        signals: list[Signal] = []
        for name, engine in self._engines():
            found = engine.on_bar(
                trigger,
                liquidity,
                hourly,
                spread_points=spread,
                news_bias=score,
                median_spread_points=median,
                news_windows=windows,
            )
            if getattr(self._config.models, name).enabled:
                signals += found
        signals = self._with_context(self._with_smt(signals, trigger))
        self._market = {
            "trigger": trigger,
            "liquidity": liquidity,
            "hourly": hourly,
            "news_score": score,
        }
        pressure: PressureReading | None = None
        for signal in signals:
            pressure = self._handle_signal(signal, now, spread, median, bias)
        self._last_bar = newest
        self._db.set_state(self._state_last_bar, newest.isoformat())
        reading = pressure or self._pressure(hourly, score)
        if self._panel is not None:
            self._write_panel(now, reading, bias)
        self._snapshot_market = self._market_picture(now, trigger, reading, bias)
        self._write_snapshot(now)
        return True

    # -- what the dashboard reads ------------------------------------------------- #

    def _market_picture(
        self,
        now: dt.datetime,
        trigger: pd.DataFrame,
        reading: PressureReading | None,
        bias: NewsBias | None,
    ) -> dict[str, Any]:
        price = float(trigger["close"].iloc[-1])
        nearest = sorted(self.engine.pools, key=lambda p: abs(p.price - price))[:24]
        h1 = self.engine.bias
        return {
            "bar_time": now.isoformat(),
            "price": price,
            "news_bias": bias.score if bias is not None else None,
            "h1_bias": None if h1 is None else {"value": h1.bias.value, "score": h1.score},
            "pressure": None
            if reading is None
            else {
                "text": reading.describe(),
                "buyers": reading.buyers,
                "sellers": reading.sellers,
                "score": reading.score,
                "label": reading.label,
                "components": dict(reading.components),
            },
            "pools": [
                {
                    "label": p.label,
                    "price": p.price,
                    "side": p.side.value,
                    "strength": round(p.strength, 2),
                    "swept": p.swept,
                }
                for p in nearest
            ],
        }

    def _write_snapshot(self, now: dt.datetime) -> None:
        """The latest market and trading picture, for the dashboard. Never stops trading."""
        try:
            self._db.set_state(
                SNAPSHOT_KEY,
                {
                    **self._snapshot_market,
                    "time": now.isoformat(),
                    "mode": self.label,
                    "live": self.live,
                    "state": self.trading,
                    "symbol": self.spec.name,
                    "digits": self.spec.digits,
                    "equity": self.executor.equity,
                    "positions": self.executor.open_positions,
                    "orders": [plan.to_state() for plan in self.executor.pending_orders],
                    "risk": self.executor.manager.status(now),
                    "armed": self.executor.armed,
                    "switch": {"strategies": self.strategies, "scalper": self._scalper_line()},
                },
            )
        except Exception as exc:  # noqa: BLE001 - the dashboard is a viewer only
            log.warning("dashboard snapshot not written", error=repr(exc))

    def _pressure(self, hourly: pd.DataFrame, news_score: float) -> PressureReading | None:
        meter = self._config.pressure
        try:
            bars = self._broker.get_rates(self.spec.name, meter.timeframe, meter.window)
        except BrokerError:
            return None
        return pressure_meter(
            bars,
            meter,
            hourly=hourly.iloc[-STRUCTURE_BARS:],
            news_bias=news_score,
            swing_lookback=self._config.strategy.swing_lookback,
        )

    def _write_panel(
        self, now: dt.datetime, pressure: PressureReading | None, bias: NewsBias | None
    ) -> None:
        if self._panel is None:
            return
        self._panel.write(
            self._db,
            now=now,
            mode=self.label,
            state=self.trading,
            pressure=pressure.describe() if pressure else None,
            news_bias=bias.score if bias is not None else None,
            open_ids={p["id"] for p in self.executor.open_positions},
            waiting_ids={plan.id for plan in self.executor.pending_orders},
        )

    def _numbered(self, signal: Signal) -> Signal:
        """Signal ids continue from the database, so they stay unique across restarts."""
        prefix = signal.id.rsplit("-", 1)[0]
        # GLOB "...-LDN-[0-9]*" counts this model's ids only, not "...-LDN-CISD-01".
        (count,) = self._db.query(
            "SELECT COUNT(*) FROM signals WHERE id GLOB ?", (f"{prefix}-[0-9]*",)
        )[0]
        return replace(signal, id=f"{prefix}-{int(count) + 1:02d}")

    def _duplicate_of(self, signal: Signal) -> str | None:
        """Spec 7b: one trade per sweep. A restart must not trade the same raid twice.

        Checked per model (D85): an alert-only model's signal must not stop the
        trading model from taking the same raid.
        """
        since = int((signal.time - DUPLICATE_WINDOW).timestamp())
        rows = self._db.query(
            "SELECT id, detail FROM signals WHERE time >= ? AND direction = ? "
            "AND action IN ('traded', 'blocked', 'alert', 'armed')",
            (since, signal.direction.value),
        )
        for signal_id, detail in rows:
            data = json.loads(detail or "{}")
            if data.get("model", "sweep_mss") != signal.model:
                continue
            if data.get("pool") == signal.pool.label and data.get("pool_price") == round(
                signal.pool.price, 2
            ):
                return str(signal_id)
        return None

    def _order_text(self, plan: TradePlan, event: BookEvent) -> str:
        valid = f"valid until {plan.expires_at:%H:%M} UTC"
        if plan.expiry_note:
            valid += f" (before {plan.expiry_note})"
        if not self.live:
            if plan.entry_mode == "market":
                return "paper market order at the next open"
            return f"paper limit order placed, {valid}"
        text = f"sent to MT5: {event.detail}"
        if "(limit" in event.detail:
            text += f"; {valid}"
        return text

    def _handle_signal(
        self,
        signal: Signal,
        now: dt.datetime,
        spread: float,
        median: float | None,
        bias: NewsBias | None,
    ) -> PressureReading | None:
        signal = self._numbered(signal)
        blackouts = self._news.blackouts(now) if self._news else []
        plan = (
            TradePlan.from_signal(signal, tp3_r=self._config.risk.tp3_r)
            .with_exit_targets(self._config.risk)
            .with_deadline(
                sessions=self._sessions,
                events=blackouts,
                blackout_before_min=self._config.news.blackout_before_min,
            )
        )
        execution = self._config.execution
        traded = blocked = False
        lots: float | None = None
        follow_up: list[BookEvent] = []
        duplicate = self._duplicate_of(signal)
        if duplicate is not None:
            action, text = "duplicate", f"same raid as {duplicate}: ignored"
        elif not signal.meets_alert:
            action, text = (
                "below_alert",
                f"score below the alert threshold {signal.alert_threshold:g}",
            )
        elif self.trading != ACTIVE:
            action, text = "alert", f"alert only: trading is {self.trading}"
        elif not execution.auto_trade:
            action, text = "alert", "alert only: auto_trade is off"
        elif self.strategies == "scalper":
            action, text = "alert", "alert only: the main strategy is switched off (/mode)"
        elif (refusal := self._playbook_refusal(signal)) is not None:
            action, text = "alert", f"alert only: {refusal}"
        elif self._config.strategy.refinement.mode == "m1":
            # D83: no order yet; the refine job waits for the M1 turn inside the gap.
            self._save_armed(
                [*self._armed(), RefineSetup.from_signal(signal, expires_at=plan.expires_at)]
            )
            action = "armed"
            text = f"waiting for an M1 turn inside the gap until {plan.expires_at:%H:%M} UTC"
            if plan.expiry_note:
                text += f" (before {plan.expiry_note})"
        else:
            event = self.executor.submit(
                plan, spread_points=spread, median_spread_points=median, events=blackouts
            )
            if event.kind is EventKind.PLACED:
                traded, lots, action = True, event.lots, "traded"
                text = self._order_text(plan, event)
                follow_up = self.executor.sync(now)  # a market order fills at once
            else:
                blocked, action, text = True, "blocked", f"blocked: {event.detail}"
        monitoring = None
        if action not in ("below_alert", "duplicate"):
            monitoring = self._monitor(signal, plan, spread, median, blackouts)
        grade_text = self._grade_text(signal)
        pressure = self._pressure(
            self._market.get("hourly", pd.DataFrame()), bias.score if bias else 0.0
        )
        self._store_signal(
            signal, action, text, pressure, plan, grade=grade_text, monitoring=monitoring
        )
        log.info("signal", id=signal.id, action=action, detail=text, score=signal.score)
        if action in ("below_alert", "duplicate") or (
            blocked and not self._config.alerts.send_blocked
        ):
            return pressure
        self._send(
            messages.signal_alert(
                signal,
                action=text,
                traded=traded,
                blocked=blocked,
                lots=lots,
                risk_pct=self._config.risk.risk_per_trade_pct,
                pressure=pressure,
                bias=bias,
                next_event=self._news.next_event(now) if self._news else None,
                now=now,
                mode=self.label,
                take_profit_3=plan.take_profit_3,
                plan=plan,
                grade=grade_text,
                monitoring=monitoring,
            )
        )
        self._report(follow_up)
        return pressure

    def _store_signal(
        self,
        signal: Signal,
        action: str,
        text: str,
        pressure: PressureReading | None,
        plan: TradePlan,
        *,
        grade: str | None = None,
        monitoring: str | None = None,
    ) -> None:
        detail = {
            "pool": signal.pool.label,
            "pool_price": round(signal.pool.price, 2),
            "action": text,
            "notes": list(signal.notes),
            "pressure": pressure.describe() if pressure else None,
            "components": dict(signal.components),
            "take_profit_3": plan.take_profit_3,
            "mode": self.label,
            "model": signal.model,
            "tags": list(signal.tags),
            "grade": grade,
            "monitoring": monitoring,
        }
        self._db.execute(
            "INSERT OR REPLACE INTO signals (id, time, direction, session, score, entry, "
            "stop_loss, take_profit_1, take_profit_2, risk_reward, action, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal.id,
                int(signal.time.timestamp()),
                signal.direction.value,
                signal.session,
                signal.score,
                signal.entry,
                signal.stop_loss,
                signal.take_profit_1,
                signal.take_profit_2,
                signal.risk_reward_1,
                action,
                json.dumps(detail),
            ),
        )

    # -- session playbooks and monitoring (D86, D87) ---------------------------------- #

    def _neutral_extra(self) -> float:
        return self._config.strategy.bias.neutral_extra_score

    def _candidate(self, playbook: str) -> PlaybookRule | None:
        rule: PlaybookRule | None = getattr(self._config.playbooks, playbook).candidate
        return rule

    def _approved_rule(self, playbook: str) -> PlaybookRule | None:
        """The session's candidate, if the owner approved it exactly as it stands now.

        An approval names the rule's fingerprint, so editing the candidate in
        config.yaml withdraws the approval and starts its monitoring again.
        """
        candidate = self._candidate(playbook)
        if candidate is None:
            return None
        approved = self._db.get_state(APPROVED_KEY, {}).get(playbook)
        return candidate if approved == fingerprint(candidate) else None

    def _live_rule(self, playbook: str) -> PlaybookRule | None:
        """What trades for real in a session: the approved candidate, else the trading rule."""
        approved = self._approved_rule(playbook)
        if approved is not None:
            return approved
        rule: PlaybookRule | None = getattr(self._config.playbooks, playbook).trading
        return rule

    def _playbook_refusal(self, signal: Signal) -> str | None:
        """Why ``signal`` may not trade for real, or None when its session's rule takes it."""
        if not getattr(self._config.models, signal.model).trade:
            return f"the {messages.model_name(signal.model)} model does not trade yet"
        if not signal.meets_auto_trade:
            return f"score below the auto-trade threshold {signal.auto_trade_threshold:g}"
        playbook = playbook_session(signal.session)
        if playbook is None:
            return "no session playbook covers this window"
        rule = self._live_rule(playbook)
        name = playbook_name(playbook)
        if rule is None:
            return f"the {name} playbook does not trade yet"
        if not meets(signal, rule, neutral_extra=self._neutral_extra()):
            return f"outside the {name} playbook ({describe_rule(rule)})"
        return None

    def _grade_text(self, signal: Signal) -> str:
        letter = grade(signal, self._config.playbooks, neutral_extra=self._neutral_extra())
        playbook = playbook_session(signal.session)
        if letter == "A+" and playbook is not None:
            return f"A+ ({playbook_name(playbook)} playbook)"
        return letter

    def _monitor(
        self,
        signal: Signal,
        plan: TradePlan,
        spread: float,
        median: float | None,
        blackouts: Sequence[NewsEvent],
    ) -> str | None:
        """A setup that meets a session's candidate, not yet approved, is shadow-traded.

        This happens whether or not the trading rule also took it, so the London and
        New York candidates are measured too. Nothing is sent to MT5. Returns the
        line for the alert, or None when the setup is not monitored.
        """
        if self.trading == STOPPED:
            return None
        playbook = playbook_session(signal.session)
        if playbook is None:
            return None
        candidate = self._candidate(playbook)
        if candidate is None or self._approved_rule(playbook) is not None:
            return None
        if not meets(signal, candidate, neutral_extra=self._neutral_extra()):
            return None
        name = playbook_name(playbook)
        try:
            event = self._shadow.submit(
                plan,
                candidate,
                spread_points=spread,
                median_spread_points=median,
                events=blackouts,
            )
            tally = self._shadow.tally(playbook, candidate)
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading
            log.error("shadow book failed", error=repr(exc), id=signal.id)
            return None
        if event.kind is not EventKind.PLACED:
            return f"A+ for the {name} playbook, not monitored ({event.detail})"
        needed = self._config.playbooks.monitor_trades
        return f"monitored for the {name} A+ playbook ({tally.finished} of {needed} finished)"

    def _monitor_bar(self, bar: Bar, trail: TrailLevels) -> None:
        """The bar moves the monitored setups; finished ones are counted.

        A finished monitored trade sends no message (the owner's choice, D87): the
        tally is in /status, and Telegram speaks only to ask for the approval.
        """
        try:
            closed = self._shadow.on_bar(bar, trail)
        except Exception as exc:  # noqa: BLE001 - monitoring must never stop trading
            log.error("shadow book failed", error=repr(exc))
            return
        for event in closed:
            playbook = playbook_session(event.plan.session)
            candidate = self._candidate(playbook) if playbook is not None else None
            if playbook is None or candidate is None or event.trade is None:
                continue
            tally = self._shadow.tally(playbook, candidate)
            log.info(
                "monitored trade finished",
                id=event.plan.id,
                playbook=playbook,
                exit=event.trade.exit_reason.value,
                r=round(event.trade.r_multiple, 2),
                finished=tally.finished,
                average_r=tally.average_r,
            )
            self._ask_approval(playbook, candidate, tally)

    def _ask_approval(self, playbook: str, candidate: PlaybookRule, tally: Tally) -> None:
        """Once per candidate: enough monitored trades with a positive average."""
        if not tally.confirmed(self._config.playbooks.monitor_trades):
            return
        if self._approved_rule(playbook) is not None:
            return
        asked = dict(self._db.get_state(ASKED_KEY, {}))
        key = fingerprint(candidate)
        if asked.get(playbook) == key:
            return
        self._send(
            messages.approval_request(
                playbook, playbook_name(playbook), describe_rule(candidate), tally
            )
        )
        asked[playbook] = key
        self._db.set_state(ASKED_KEY, asked)

    def _approve(self, argument: str) -> str:
        """``/approve <session>``: a confirmed candidate trades through the executor.

        ``/approve scalper <mode>`` approves a scalper mode instead (D103).
        """
        first, _, rest = argument.strip().partition(" ")
        if first.lower() == "scalper":
            return self._approve_scalper(rest.strip().lower())
        playbook = parse_playbook(argument)
        if playbook is None:
            return "Send /approve asia, /approve london or /approve newyork."
        name = playbook_name(playbook)
        candidate = self._candidate(playbook)
        if candidate is None:
            return f"The {name} playbook has no A+ candidate to approve."
        if self._approved_rule(playbook) is not None:
            return f"The {name} A+ playbook is already approved and trading."
        needed = self._config.playbooks.monitor_trades
        tally = self._shadow.tally(playbook, candidate)
        if not tally.confirmed(needed):
            return (
                f"Not approved: the {name} A+ playbook needs {needed} finished monitored "
                f"trades with a positive average. So far: {messages.tally_text(tally)}."
            )
        approved = dict(self._db.get_state(APPROVED_KEY, {}))
        approved[playbook] = fingerprint(candidate)
        self._db.set_state(APPROVED_KEY, approved)
        log.info("playbook approved", playbook=playbook, rule=describe_rule(candidate))
        note = ""
        if not getattr(self._config.models, candidate.model).trade:
            note = (
                f" Note: models.{candidate.model}.trade is false in config.yaml, so its "
                "setups still alert only."
            )
        return (
            f"✅ {name} A+ playbook approved: {describe_rule(candidate)}. From now on only "
            f"these setups trade in the {name} session ({messages.tally_text(tally)} in "
            f"monitoring). /revoke {playbook} undoes it.{note}"
        )

    def _revoke(self, argument: str) -> str:
        """``/revoke <session>``: back to the trading rule; the candidate is monitored again."""
        playbook = parse_playbook(argument)
        if playbook is None:
            return "Send /revoke asia, /revoke london or /revoke newyork."
        name = playbook_name(playbook)
        approved = dict(self._db.get_state(APPROVED_KEY, {}))
        if approved.pop(playbook, None) is None:
            return f"The {name} A+ playbook was not approved: nothing to revoke."
        self._db.set_state(APPROVED_KEY, approved)
        log.info("playbook approval revoked", playbook=playbook)
        trading = getattr(self._config.playbooks, playbook).trading
        now = describe_rule(trading) if trading is not None else "no trading rule (alerts only)"
        return f"↩️ {name} A+ playbook approval revoked. The {name} session now uses: {now}."

    def _playbook_lines(self) -> list[str]:
        """One line per session for /status."""
        needed = self._config.playbooks.monitor_trades
        lines = ["Playbooks:"]
        for playbook in PLAYBOOK_SESSIONS:
            name = playbook_name(playbook)
            live = self._live_rule(playbook)
            trades = describe_rule(live) if live is not None else "alerts only"
            candidate = self._candidate(playbook)
            if candidate is None:
                state = "no A+ candidate"
            elif self._approved_rule(playbook) is not None:
                state = "A+ candidate approved"
            else:
                tally = self._shadow.tally(playbook, candidate)
                state = f"A+ candidate monitored: {messages.tally_text(tally)} (needs {needed})"
            lines.append(f"• {name}: trades {trades} | {state}")
        return lines

    # -- M1 refinement: "sniper" entries (D83) ------------------------------------------ #

    def _armed(self) -> list[RefineSetup]:
        return [
            RefineSetup.from_state(item) for item in self._db.get_state(self._state_refining, [])
        ]

    def _save_armed(self, setups: Sequence[RefineSetup]) -> None:
        self._db.set_state(self._state_refining, [setup.to_state() for setup in setups])

    def refine_job(self) -> None:
        """Called each minute: every armed setup is checked against the closed M1 bars."""
        try:
            with self._lock:
                self.refine_tick()
        except BrokerError as exc:
            log.error("broker error in the refine job", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            log.error("refine job failed", error=repr(exc))

    def refine_tick(self) -> None:
        armed = self._armed()
        if not armed:
            return
        now = ensure_utc(self._clock.now())
        strategy = self._config.strategy
        oldest = min(setup.time for setup in armed)
        count = int((now - oldest).total_seconds() // 60) + REFINE_CONTEXT_BARS + 5
        bars = self._broker.get_rates(self.spec.name, Timeframe.M1, min(count, REFINE_MAX_BARS))
        keep: list[RefineSetup] = []
        for setup in armed:
            result = refine(
                setup,
                bars,
                strategy.refinement,
                atr_period=strategy.atr_period,
                atr_method=strategy.atr_method,
                min_rr=strategy.min_rr,
            )
            if result.status is RefineStatus.WAITING:
                if now < ensure_utc(setup.expires_at) + REFINE_GRACE:
                    keep.append(setup)
                    continue
                result = RefineResult(RefineStatus.EXPIRED, "no M1 turn inside the gap in time")
            if result.status is RefineStatus.READY:
                self._place_refined(setup, result, now)
            else:
                self._db.execute(
                    "UPDATE signals SET action = ? WHERE id = ?", ("no_trigger", setup.id)
                )
                self._send(messages.refine_dropped(setup, result.detail, self.label))
        self._save_armed(keep)

    def _place_refined(self, setup: RefineSetup, result: RefineResult, now: dt.datetime) -> None:
        blackouts = self._news.blackouts(now) if self._news else []
        plan = (
            refined_plan(setup, result, tp3_r=self._config.risk.tp3_r)
            .with_exit_targets(self._config.risk)
            .with_deadline(
                sessions=self._sessions,
                events=blackouts,
                blackout_before_min=self._config.news.blackout_before_min,
            )
        )
        follow_up: list[BookEvent] = []
        traded = False
        if self.trading != ACTIVE:
            action, text = "alert", f"not traded: trading is {self.trading}"
        else:
            spread = float(self._broker.spread_points(self.spec.name))
            median = self._db.median_spread(
                self.spec.name,
                since=now - dt.timedelta(days=self._config.risk.spread_median_days),
                min_samples=MEDIAN_SPREAD_MIN_SAMPLES,
            )
            event = self.executor.submit(
                plan, spread_points=spread, median_spread_points=median, events=blackouts
            )
            if event.kind is EventKind.PLACED:
                traded, action, text = True, "traded", self._order_text(plan, event)
                follow_up = self.executor.sync(now)  # a market order fills at once
            else:
                action, text = "blocked", f"blocked: {event.detail}"
        self._db.execute(
            "UPDATE signals SET action = ?, entry = ?, stop_loss = ? WHERE id = ?",
            (action, plan.entry, plan.stop_loss, setup.id),
        )
        log.info("refined entry", id=setup.id, action=action, entry=plan.entry, stop=plan.stop_loss)
        self._send(
            messages.refined_entry(
                setup,
                plan,
                symbol=self.spec.name,
                action=text,
                traded=traded,
                mode=self.label,
                risk_pct=self._config.risk.risk_per_trade_pct,
            )
        )
        self._report(follow_up)

    # -- following the broker (live) ------------------------------------------------- #

    def sync(self) -> None:
        """LIVE: compare orders and positions with the broker and report what changed."""
        with self._lock:
            now = ensure_utc(self._clock.now())
            events = self.executor.sync(now)
            self._report(events)
            if events:
                self._write_snapshot(now)

    def latency_line(self) -> str:
        """The terminal's ping and recent order round trips (S2), when known."""
        parts: list[str] = []
        try:
            ping = self._broker.terminal_info().ping_ms
        except BrokerError:
            ping = None
        if ping is not None:
            parts.append(f"ping {ping:g} ms")
        times = [
            float(value)
            for (value,) in self._db.query(
                "SELECT elapsed_ms FROM order_log WHERE elapsed_ms IS NOT NULL "
                "ORDER BY id DESC LIMIT 20"
            )
        ]
        if times:
            middle = sorted(times)[len(times) // 2]
            parts.append(f"orders {middle:g} ms (median of the last {len(times)})")
        return ", ".join(parts)

    def tick_job(self) -> None:
        """Each minute: save the finished hours of ticks (D106). Never stops trading."""
        if self.ticks is None:
            return
        try:
            with self._lock:
                self.ticks.record()
        except Exception as exc:  # noqa: BLE001 - recording must never stop trading
            log.error("tick recorder failed", error=repr(exc))

    def sync_job(self) -> None:
        try:
            self.sync()
        except BrokerError as exc:
            log.error("broker error in the sync job", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            log.error("sync job failed", error=repr(exc))

    # -- commands (spec 6: kill switch) ------------------------------------------- #

    def handle(self, command: Command) -> str:
        """Carry out one command (Telegram or dashboard) and return the reply."""
        with self._lock:
            if command.name == "golive":
                reply = self._go_live(command.argument)
            elif command.name == "approve":
                reply = self._approve(command.argument)
            elif command.name == "revoke":
                reply = self._revoke(command.argument)
            elif command.name == "mode":
                reply = self._set_strategies(command.argument)
            elif command.name == "scalper":
                reply = self._set_scalper_mode(command.argument)
            else:
                reply = self._handle(command.name)
            if command.name in ("pause", "resume", "stop", "golive", "mode", "scalper"):
                self._write_snapshot(ensure_utc(self._clock.now()))
            return reply

    def _go_live(self, argument: str) -> str:
        """The typed confirmation after an unattended start: /golive <account number>."""
        executor = self.executor
        if not self.live:
            return "Paper trading: there is nothing to confirm."
        if executor.armed:
            return f"Live orders are already allowed on account {executor.login}."
        if argument.strip() != str(executor.login):
            return f"Not confirmed. To allow live orders, send exactly: /golive {executor.login}"
        notes = executor.arm()
        self._report(executor.sync(ensure_utc(self._clock.now())))
        found = "".join(f"\n• {note}" for note in notes)
        return (
            f"✅ Live orders confirmed for account {executor.login}. Trading is {self.trading}."
            f"{found}"
        )

    def _handle(self, name: str) -> str:
        if name == "pause":
            self._set_trading(PAUSED)
            return "⏸ Paused: no new trades. Open trades are still managed."
        if name == "resume":
            self._set_trading(ACTIVE)
            return "▶️ Trading resumed."
        if name == "stop":
            return self._kill_switch()
        if name == "status":
            return self.status()
        return messages.HELP

    def dashboard_job(self) -> None:
        """Carry out commands sent from the dashboard, and note that the bot is alive.

        The heartbeat is written without waiting for the trading lock; commands wait
        for the next run (2 s later) when the lock is busy, so the job never piles up.
        """
        try:
            now = ensure_utc(self._clock.now())
            if self._last_heartbeat is None or now - self._last_heartbeat >= HEARTBEAT_EVERY:
                self._db.set_state(
                    HEARTBEAT_KEY,
                    {"time": now.isoformat(), "mode": self.label, "armed": self.executor.armed},
                )
                self._last_heartbeat = now
            if not self._lock.acquire(timeout=1.0):
                return
            try:
                rows = self._db.query(
                    "SELECT id, created, name FROM commands WHERE status = 'pending' ORDER BY id"
                )
                for command_id, created, name in rows:
                    if now.timestamp() - created > COMMAND_MAX_AGE.total_seconds():
                        self._finish(command_id, "expired", "not carried out: it waited too long")
                        continue
                    if name not in DASHBOARD_COMMANDS:
                        self._finish(command_id, "done", messages.HELP)
                        continue
                    base, _, argument = name.partition(":")  # "mode:both" -> /mode both
                    reply = self.handle(Command(base, argument, 0))
                    self._finish(command_id, "done", reply)
                    if name != "status":
                        shown = f"{base} {argument}".strip()
                        self._send(f"🖥 Dashboard /{shown}: {reply}")
            finally:
                self._lock.release()
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            log.error("dashboard job failed", error=repr(exc))

    # -- Phase 8: heartbeat and housekeeping ------------------------------------------ #

    def heartbeat_job(self) -> None:
        """A quiet "still running" message (spec 9.8), with what matters at a glance."""
        try:
            now = ensure_utc(self._clock.now())
            picture = self._db.get_state(SNAPSHOT_KEY) or {}
            upcoming = self._sessions.next_tradeable_span(now)
            current = self._sessions.status(now)
            if current.allowed and current.session:
                window = f"{messages.session_name(current.session)} open now"
            elif upcoming is not None:
                window = f"next: {upcoming.label} {upcoming.start:%a %H:%M} UTC"
            else:
                window = "no window ahead"
            bar = self._last_bar.strftime("%H:%M UTC") if self._last_bar else "none yet"
            confirm = (
                ""
                if self.executor.armed
                else (f" | live orders NOT confirmed: send /golive {self.executor.login}")
            )
            equity = picture.get("equity", self.executor.manager.equity)
            open_count = len(picture.get("positions", []))
            self._notifier.send(
                f"💓 GoldSweep {self.label} running | trading {self.trading}{confirm} | "
                f"equity {equity:,.2f} | open trades {open_count} | last bar {bar} | {window}",
                quiet=True,
            )
        except Exception as exc:  # noqa: BLE001 - a heartbeat must never stop trading
            log.error("heartbeat failed", error=repr(exc))

    def housekeeping_job(self) -> None:
        """Daily: a copy of the database in data/backups, old log rows removed."""
        try:
            now = ensure_utc(self._clock.now())
            keep = self._config.housekeeping
            folder = self._settings.data_dir / "backups"
            if keep.backups_kept > 0:
                stem = f"{Path(self._db.path).stem}-{now:%Y-%m-%d}"
                self._db.backup(folder / f"{stem}.sqlite3")
                copies = sorted(folder.glob(f"{Path(self._db.path).stem}-*.sqlite3"))
                for old in copies[: max(0, len(copies) - keep.backups_kept)]:
                    old.unlink(missing_ok=True)
            removed = self._db.prune_logs(now - dt.timedelta(days=keep.log_days))
            log.info("housekeeping done", removed_log_rows=removed)
        except Exception as exc:  # noqa: BLE001 - upkeep must never stop trading
            log.error("housekeeping failed", error=repr(exc))

    def _finish(self, command_id: int, status: str, reply: str) -> None:
        self._db.execute(
            "UPDATE commands SET status = ?, finished = ?, reply = ? WHERE id = ?",
            (status, int(ensure_utc(self._clock.now()).timestamp()), reply, command_id),
        )

    def _kill_switch(self) -> str:
        now = ensure_utc(self._clock.now())
        self._set_trading(STOPPED)  # first: nothing new may start while closing
        self._save_armed([])  # setups waiting for an M1 entry are dropped too
        self._shadow.clear(now)  # and the monitored ones (D87): nothing of them is counted
        tick = self._broker.get_tick(self.spec.name)
        events = self.executor.close_everything(now, tick.bid, tick.ask, "kill switch (/stop)")
        self._report(events)
        closed = sum(1 for e in events if e.kind is EventKind.CLOSED)
        cancelled = sum(1 for e in events if e.kind is EventKind.CANCELLED)
        left = len(self.executor.open_positions)
        warning = f" {left} position(s) could not be closed: check MT5 now!" if left else ""
        return (
            f"🛑 Kill switch: {closed} position(s) closed, {cancelled} order(s) cancelled. "
            f"Trading is stopped until /resume.{warning}"
        )

    def poll_commands(self, wait_seconds: int = 0) -> None:
        if self._bot is None:
            return
        for command in self._bot.commands(wait_seconds=wait_seconds):
            self._send(self.handle(command))

    def status(self) -> str:
        now = ensure_utc(self._clock.now())
        upcoming = self._sessions.next_tradeable_span(now)
        window = f"{upcoming.label} {upcoming.start:%a %H:%M} UTC" if upcoming else "none"
        current = self._sessions.status(now)
        if current.allowed and current.session:
            window = f"{messages.session_name(current.session)} open now"
        text = messages.status_message(
            mode=self.label,
            state=self.trading,
            equity=self.executor.equity,
            positions=self.executor.open_positions,
            orders=self.executor.pending_orders,
            today_trades=self.executor.trades_between(now - dt.timedelta(hours=24), now),
            bias=self._news.bias(now) if self._news else None,
            next_window=window,
            llm=self._news.analyzer.llm_status(now) if self._news else "news layer off",
        )
        armed = self._armed()
        if armed:
            waiting = ", ".join(f"{s.id} until {s.expires_at:%H:%M} UTC" for s in armed)
            text += f"\nWaiting for an M1 entry: {waiting}"
        text += "\n" + "\n".join(self._playbook_lines())
        text += f"\nStrategies that may trade: {self.strategies} | scalper: {self._scalper_line()}"
        latency = self.latency_line()
        if latency:
            text += f"\nLatency: {latency}"
        monitored = [p.id for p in self._shadow.orders] + [p["id"] for p in self._shadow.positions]
        if monitored:
            text += f"\nMonitored now (paper only): {', '.join(monitored)}"
        return text

    # -- news, warnings, scenarios, summary ---------------------------------------- #

    def news_tick(self) -> None:
        """Refresh the news, then send any warning, breakeven move or brief now due.

        The refresh (feeds and AI calls, which can take a while) runs outside the
        trading lock: it never touches the broker, and the bar job must not wait
        for it.
        """
        if self._news is None:
            return
        try:
            self._news.refresh(ensure_utc(self._clock.now()))
        except Exception as exc:  # noqa: BLE001 - one bad refresh must not stop the loop
            log.error("news refresh failed", error=str(exc))
        with self._lock:
            now = ensure_utc(self._clock.now())
            events = self._news.events(now)
            self._warn_before_releases(now, events)
            self._withdraw_before_releases(now, events)
            self._protect_before_releases(now, events)
            if self._config.alerts.send_scenarios:
                self._scenarios(now, events)

    def _warn_before_releases(self, now: dt.datetime, events: Sequence[EconomicEvent]) -> None:
        alerts, news = self._config.alerts, self._config.news
        warned: list[str] = list(self._db.get_state(STATE_WARNED, []))
        lead = dt.timedelta(minutes=alerts.pre_news_warning_min)
        for event in events:
            key = f"{event.source}-{event.event_id}"
            if key in warned or not is_blackout_event(event, news):
                continue
            if now < event.time <= now + lead:
                self._send(
                    messages.pre_news_warning(
                        event, now, news.blackout_before_min, news.blackout_after_min
                    )
                )
                warned.append(key)
        self._db.set_state(STATE_WARNED, warned[-200:])

    def _withdraw_before_releases(self, now: dt.datetime, events: Sequence[EconomicEvent]) -> None:
        """No entry may fill in a blackout. Entries placed while a release was already
        known expire before it (``entry_deadline``); this catches releases the calendar
        added later: within a minute of their blackout, waiting entries are withdrawn."""
        if not self.executor.pending_orders:
            return
        news = self._config.news
        before = dt.timedelta(minutes=news.blackout_before_min)
        after = dt.timedelta(minutes=news.blackout_after_min)
        for event in events:
            if is_blackout_event(event, news) and (
                event.time - before - WITHDRAW_LEAD <= now < event.time + after
            ):
                detail = f"withdrawn before the news blackout for {event.currency} {event.title}"
                self._report(self.executor.withdraw_pending(now, detail))
                return

    def _protect_before_releases(self, now: dt.datetime, events: Sequence[EconomicEvent]) -> None:
        """Spec 5.6: inside a release's blackout, trades far enough up go to breakeven."""
        news = self._config.news
        min_r = news.breakeven_before_news_r
        if min_r <= 0 or not self.executor.open_positions:
            return
        before = dt.timedelta(minutes=news.blackout_before_min)
        for event in events:
            if is_blackout_event(event, news) and event.time - before <= now < event.time:
                tick = self._broker.get_tick(self.spec.name)
                detail = f"before {event.currency} {event.title}"
                self._report(
                    self.executor.protect_before_news(now, tick.bid, tick.ask, min_r, detail)
                )
                return

    def _scenarios(self, now: dt.datetime, events: Sequence[EconomicEvent]) -> None:
        assert self._news is not None
        triggers = due_triggers(now, self._sessions, events, self._config.news)
        market = self._market
        if not triggers or not market:
            return
        trigger_bars, liquidity, hourly = market["trigger"], market["liquidity"], market["hourly"]
        price = float(trigger_bars["close"].iloc[-1])
        strategy = self._config.strategy
        levels = self.engine.day_levels
        bias = compute_bias(
            hourly,
            price=price,
            previous_mid=levels.previous_mid if levels else None,
            news_bias=market["news_score"],
            config=strategy.bias,
            swing_lookback=strategy.swing_lookback,
        )
        atr = latest_atr(liquidity, strategy.atr_period, strategy.atr_method)
        reading = self._pressure(hourly, market["news_score"])
        for trigger in triggers:
            if self._news.scenarios.built(trigger.key):
                continue
            context = build_context(
                now=now,
                trigger=trigger.text,
                price=price,
                atr=atr,
                pools=self.engine.pools,
                bias=bias.bias.value,
                news_bias=market["news_score"],
                pressure=reading.describe() if reading else None,
                events=[e for e in events if e.time.date() == now.date()],
                config=self._config.news,
                min_strength=strategy.liquidity.min_sweep_pool_strength,
            )
            brief = self._news.scenarios.build(context, trigger.key)
            self._send(messages.scenario_message(brief))

    def daily_summary(self) -> None:
        with self._lock:
            now = ensure_utc(self._clock.now())
            since = now - dt.timedelta(hours=24)
            counts: dict[str, int] = {}
            for (action,) in self._db.query(
                "SELECT action FROM signals WHERE time >= ?", (int(since.timestamp()),)
            ):
                counts[action] = counts.get(action, 0) + 1
            cost = self._news.analyzer.usage.day(now).cost_usd if self._news else 0.0
            local = now.astimezone(_zone(self._config.alerts.daily_summary_tz))
            self._send(
                messages.daily_summary(
                    day=f"{local:%Y-%m-%d}",
                    mode=self.label,
                    trades=self.executor.trades_between(since, now),
                    equity=self.executor.equity,
                    signals=counts,
                    bias=self._news.bias(now) if self._news else None,
                    llm_cost=cost,
                )
            )

    # -- the scheduler's jobs ------------------------------------------------------- #

    def bar_job(self) -> None:
        """Called just after each M5 close; keeps asking until the broker has the bar."""
        deadline = time.monotonic() + self._config.execution.bar_wait_seconds
        try:
            while not self.on_bar_close():
                if time.monotonic() >= deadline:
                    log.warning("the closed bar did not arrive in time; trying at the next close")
                    return
                time.sleep(2)
        except BrokerError as exc:
            log.error("broker error in the bar job", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; the error is logged
            log.error("bar job failed", error=repr(exc))

    def command_job(self) -> None:
        try:
            self.poll_commands(wait_seconds=10)
        except Exception as exc:  # noqa: BLE001
            log.error("command job failed", error=repr(exc))

    def command_loop(self, stop: threading.Event) -> None:
        """Telegram commands, read by long polling (up to 10 s a call) until ``stop`` is set.

        It runs in its own thread: as a scheduler job every second it overlapped
        itself and the scheduler logged a warning every second.
        """
        while not stop.is_set():
            self.command_job()
            stop.wait(0.5)


#: The Phase 5 name, kept for callers and tests.
PaperTrader = Trader


def _zone(name: str) -> dt.tzinfo:
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


# --------------------------------------------------------------------------- #
# Running it
# --------------------------------------------------------------------------- #


def _schedule(trader: Trader, settings: Settings, bot: TelegramBot | None, clock: Clock) -> Any:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone=UTC)
    delay = settings.config.execution.bar_delay_seconds
    scheduler.add_job(
        trader.bar_job,
        CronTrigger(minute="*/5", second=delay, timezone=UTC),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    if settings.config.strategy.refinement.mode == "m1":
        scheduler.add_job(
            trader.refine_job,
            CronTrigger(minute="*", second=delay, timezone=UTC),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
    scheduler.add_job(
        trader.news_tick,
        IntervalTrigger(minutes=1, timezone=UTC),
        max_instances=1,
        coalesce=True,
        next_run_time=clock.now(),
    )
    alerts = settings.config.alerts
    scheduler.add_job(
        trader.daily_summary,
        CronTrigger(
            hour=alerts.daily_summary_time.hour,
            minute=alerts.daily_summary_time.minute,
            timezone=_zone(alerts.daily_summary_tz),
        ),
        max_instances=1,
        coalesce=True,
    )
    if trader.live:
        scheduler.add_job(
            trader.sync_job,
            IntervalTrigger(seconds=settings.config.execution.sync_seconds, timezone=UTC),
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        trader.dashboard_job,
        IntervalTrigger(seconds=2, timezone=UTC),
        max_instances=1,
        coalesce=True,
    )
    if trader.ticks is not None:
        scheduler.add_job(
            trader.tick_job,
            CronTrigger(minute="*", second=30, timezone=UTC),  # clear of the bar job
            max_instances=1,
            coalesce=True,
        )
    if alerts.heartbeat_minutes > 0:
        scheduler.add_job(
            trader.heartbeat_job,
            IntervalTrigger(minutes=alerts.heartbeat_minutes, timezone=UTC),
            max_instances=1,
            coalesce=True,
        )
    upkeep = settings.config.housekeeping.backup_time_utc
    scheduler.add_job(
        trader.housekeeping_job,
        CronTrigger(hour=upkeep.hour, minute=upkeep.minute, timezone=UTC),
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def _run_scheduler(scheduler: Any, trader: Trader, bot: TelegramBot | None) -> None:
    """Run the jobs, plus the Telegram command thread, until Ctrl+C."""
    stop = threading.Event()
    if bot is not None:
        threading.Thread(
            target=trader.command_loop, args=(stop,), name="telegram-commands", daemon=True
        ).start()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stop.set()


def run_paper(
    settings: Settings,
    *,
    use_mock: bool = False,
    use_llm: bool = True,
    out: TextIO | None = None,
) -> int:
    """Start the paper loop and run until Ctrl+C. Returns a process exit code."""
    stream = out or sys.stdout
    if settings.mode is not Mode.PAPER:
        print(
            "Effective mode is LIVE: start live trading with  python main.py --live  "
            "(or set MODE=PAPER in .env to paper-trade).",
            file=stream,
        )
        return EXIT_REFUSED
    print("Starting GoldSweep paper trading (loading prices and news)...", file=stream, flush=True)
    clock = SystemClock()
    db = Database(settings.mock_db_path if use_mock else settings.db_path)
    setup_logging(settings.log_dir, db=db, console=False)
    broker = create_broker(settings, mock=use_mock, clock=clock)
    try:
        broker.connect()
        notifier, bot = make_notifier(settings, db, stream=stream)
        news = NewsDesk.from_settings(settings, db, clock=clock, use_llm=use_llm)
        trader = Trader(
            settings,
            broker,
            db,
            notifier,
            clock=clock,
            news=news,
            bot=bot,
            panel=PanelWriter.for_broker(broker, settings),
        )
        restored = ""
        if trader.executor.restored:
            restored = (
                f" Restored {len(trader.executor.open_positions)} position(s) and "
                f"{len(trader.executor.pending_orders)} order(s)."
            )
        notifier.send(
            f"🚀 GoldSweep PAPER started: {trader.spec.name}, equity "
            f"{trader.executor.equity:,.2f}, trading {trader.trading}.{restored} "
            "No real orders are sent."
            + ("" if bot else " (Telegram not configured: alerts print here.)")
        )
        scheduler = _schedule(trader, settings, bot, clock)
        print("Paper trading is running. Press Ctrl+C to stop.", file=stream, flush=True)
        _run_scheduler(scheduler, trader, bot)
        notifier.send("⏹ GoldSweep PAPER stopped. Open paper positions resume at the next start.")
        return EXIT_OK
    except BrokerError as exc:
        print(f"Broker problem: {exc}", file=stream)
        return EXIT_BROKER
    finally:
        broker.shutdown()
        shutdown_logging()
        db.close()


def live_refusal(settings: Settings, account: Any, terminal: Any) -> str | None:
    """Why live trading must not start on this account, or ``None``."""
    execution = settings.config.execution
    if not account.is_demo and not execution.allow_real_account:
        return (
            f"Account {account.login} is a {account.account_type.value} account. Phase 6 trades "
            "demo accounts only; set execution.allow_real_account: true in config.yaml only "
            "after the demo results have been reviewed."
        )
    if not account.hedging:
        return (
            f"Account {account.login} is a netting account: MetaTrader 5 merges every position "
            "on a symbol into one, so GoldSweep's split into separate positions with their own "
            "targets cannot work. Use a hedging account (offered when opening a demo account)."
        )
    if not terminal.trade_allowed:
        return (
            "Algo Trading is off in MetaTrader 5. Press the 'Algo Trading' button in the "
            "terminal's toolbar (it turns green), then start again."
        )
    if not account.trade_allowed:
        return f"The broker does not allow trading on account {account.login} right now."
    return None


def confirm_live(
    settings: Settings,
    account: Any,
    spec: Any,
    *,
    ask: Callable[[str], str],
    out: TextIO,
) -> bool:
    """Ground rule 3: live orders only after the account number is typed at startup."""
    risk, execution = settings.config.risk, settings.config.execution
    kind = account.account_type.value
    lines = [
        "",
        "LIVE TRADING: GoldSweep will send real orders to this MetaTrader 5 account:",
        f"  Account   {account.login} @ {account.server} ({account.company}) - {kind}",
        f"  Balance   {account.balance:,.2f} {account.currency} | leverage 1:{account.leverage}",
        f"  Symbol    {spec.name} | risk {risk.risk_per_trade_pct:g}% per trade | "
        f"at most {execution.max_total_lots:g} lots | magic number {execution.magic_number}",
        "Every order carries a stop-loss and a take-profit. Trades can lose; the risk "
        "limits keep each loss small.",
        f"Type  LIVE {account.login}  and press Enter to start (anything else cancels):",
    ]
    print("\n".join(lines), file=out, flush=True)
    try:
        answer = ask("> ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip() == f"LIVE {account.login}"


def run_live(
    settings: Settings,
    *,
    use_llm: bool = True,
    out: TextIO | None = None,
    ask: Callable[[str], str] = input,
    broker_factory: Callable[[Settings, Clock, Database], BrokerClient] | None = None,
    run_scheduler: bool = True,
    unattended: bool = False,
) -> int:
    """Start live trading on the MT5 account after the typed confirmation.

    ``unattended`` (Task Scheduler, a VPS) asks nothing at the console: the bot
    starts watching only, and live orders wait for ``/golive <account>`` typed by
    the owner on Telegram.
    """
    from goldsweep.data.mt5_client import MT5Client
    from goldsweep.execution.live_executor import MT5Executor, order_logger
    from goldsweep.execution.mt5_gateway import MT5Gateway

    stream = out or sys.stdout
    if settings.mode is not Mode.LIVE:
        print(
            "Live trading needs MODE=LIVE in .env AND mode: LIVE in config.yaml "
            f"(now: {settings.mode_summary}). Paper-trade with  python main.py --paper.",
            file=stream,
        )
        return EXIT_REFUSED
    print("Connecting to MetaTrader 5 for LIVE trading...", file=stream, flush=True)
    clock = SystemClock()
    db = Database(settings.db_path)
    setup_logging(settings.log_dir, db=db, console=False)
    factory = broker_factory or (
        lambda s, c, d: MT5Client.from_settings(s, clock=c, state=d)  # type: ignore[misc]
    )
    broker = factory(settings, clock, db)
    try:
        if not isinstance(broker, MT5Client):
            print("Live trading needs the MetaTrader 5 terminal.", file=stream)
            return EXIT_REFUSED
        broker.connect()
        account, terminal = broker.account_info(), broker.terminal_info()
        spec = broker.resolve_symbol(settings.config.symbol_candidates)
        problem = live_refusal(settings, account, terminal)
        if problem:
            print(f"Not starting: {problem}", file=stream)
            return EXIT_REFUSED
        if unattended:
            if not (settings.env.telegram_bot_token and settings.env.telegram_chat_id):
                print(
                    "Not starting: an unattended start needs Telegram (TELEGRAM_BOT_TOKEN and "
                    "TELEGRAM_CHAT_ID in .env) for the /golive confirmation.",
                    file=stream,
                )
                return EXIT_REFUSED
        elif not confirm_live(settings, account, spec, ask=ask, out=stream):
            print("Not confirmed: no orders were sent.", file=stream)
            return EXIT_NOT_CONFIRMED
        execution = settings.config.execution
        gateway = MT5Gateway(
            broker,
            spec,
            magic=execution.magic_number,
            deviation_points=execution.deviation_points,
            order_log=order_logger(db, clock),
        )
        executor = MT5Executor(
            settings, gateway, db, clock=clock, demo=account.is_demo, armed=not unattended
        )
        notes = executor.reconcile()
        notifier, bot = make_notifier(settings, db, stream=stream)
        news = NewsDesk.from_settings(settings, db, clock=clock, use_llm=use_llm)
        trader = Trader(
            settings,
            broker,
            db,
            notifier,
            clock=clock,
            news=news,
            bot=bot,
            executor=executor,
            panel=PanelWriter.for_broker(broker, settings),
        )
        found = "".join(f"\n• {note}" for note in notes)
        waiting = (
            f"\nUnattended start: watching only. To allow live orders, send /golive {account.login}"
            if unattended
            else ""
        )
        notifier.send(
            f"🚀 GoldSweep {trader.label} started: {spec.name} on account {account.login}, "
            f"balance {account.balance:,.2f} {account.currency}, trading {trader.trading}."
            f"{found}{waiting}" + ("" if bot else " (Telegram not configured: alerts print here.)")
        )
        trader.sync()
        if not run_scheduler:
            return EXIT_OK
        scheduler = _schedule(trader, settings, bot, clock)
        print(
            f"{trader.label} trading is running. Ctrl+C stops the bot; open positions keep "
            "their stop-loss and take-profit at the broker.",
            file=stream,
            flush=True,
        )
        _run_scheduler(scheduler, trader, bot)
        notifier.send(
            f"⏹ GoldSweep {trader.label} stopped. Open positions keep their stop-loss and "
            "take-profit at the broker and are managed again at the next start."
        )
        return EXIT_OK
    except BrokerError as exc:
        print(f"Broker problem: {exc}", file=stream)
        return EXIT_BROKER
    finally:
        broker.shutdown()
        shutdown_logging()
        db.close()

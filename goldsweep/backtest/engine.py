"""The event-driven backtester (spec 9.3).

Two passes over the history:

1. **Signals.** Every closed trigger (M5) bar is handed to the same
   :class:`~goldsweep.analysis.signals.SignalEngine` the live bot uses, with only
   the bars that had closed at that moment (higher timeframes are cut to their
   closed bars too). This is the slow part, so its result is cached on disk,
   keyed by the data, the settings and the analysis code.
2. **Trades.** :class:`~goldsweep.backtest.simulator.Simulator` plays those
   signals against the prices under the risk rules and the cost model.

The walk-forward test re-runs only the second pass, many times.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pickle
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from goldsweep.analysis.context import CONTEXT_TAGS, ContextFrames, with_context
from goldsweep.analysis.grades import (
    PLAYBOOK_SESSIONS,
    describe_rule,
    meets,
    playbook_name,
    playbook_session,
)
from goldsweep.analysis.pressure import STRUCTURE_BARS, pressure_meter
from goldsweep.analysis.refine import M1Refinement
from goldsweep.analysis.signals import MODELS, SignalEngine
from goldsweep.analysis.smt import with_smt
from goldsweep.backtest.data import fingerprint
from goldsweep.backtest.metrics import (
    WEEKDAYS,
    Metrics,
    breakdown,
    by_direction,
    by_pressure,
    by_session,
    by_weekday,
    summarize,
)
from goldsweep.backtest.simulator import (
    PriceSeries,
    SignalOutcome,
    SignalRecord,
    SimulationResult,
    Simulator,
    TrailLevels,
    combine_series,
)
from goldsweep.backtest.walkforward import Combo, WalkForwardResult, make_selector, walk_forward
from goldsweep.core.clock import ASIA, LONDON, NEW_YORK, OVERLAP, SessionCalculator, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import CostConfig, PlaybookRule, Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import SymbolSpec
from goldsweep.data.candles import aggregate_candles
from goldsweep.execution.book import Outcome, Trade
from goldsweep.risk.risk_manager import NewsEvent

log = get_logger(__name__)

#: Bars of each higher timeframe handed to the engine per step (it needs fewer).
LIQUIDITY_BARS = 500
HOURLY_BARS = 400
CACHE_FORMAT = 1
_ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "analysis"

Progress = Callable[[float, dt.datetime], None]


@dataclass(frozen=True)
class MarketData:
    """Everything the backtest trades on."""

    symbol: str
    spec: SymbolSpec
    trigger: pd.DataFrame  # trigger-timeframe bars, warm-up included
    fine: pd.DataFrame | None  # one-minute bars for fills, when stored
    source: str  # where the prices came from, for the report
    synthetic: bool = False
    #: Silver bars of the trigger timeframe, for the SMT tag (D85); None when not stored.
    silver: pd.DataFrame | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.trigger, self.fine)


@dataclass
class BacktestResult:
    start: dt.datetime
    end: dt.datetime
    data: MarketData
    records: list[SignalRecord]
    result: SimulationResult
    metrics: Metrics
    breakdowns: dict[str, dict[str, Metrics]]
    walk_forward: WalkForwardResult | None
    limitations: list[str]
    fingerprints: dict[str, str]
    signals_cached: bool
    settings_snapshot: dict[str, object] = field(default_factory=dict)
    #: The rule each session traded by (D87), described; "none" where it alerted only.
    playbooks: dict[str, str] = field(default_factory=dict)
    #: True when the run traded the A+ candidates as if approved (``--candidates``).
    candidates: bool = False


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #


def spread_points(candles: pd.DataFrame, costs: CostConfig) -> np.ndarray:
    """The spread, in points, that the cost model charges on each bar."""
    if costs.spread_source == "fixed" or candles.empty:
        base = np.full(len(candles), costs.fixed_spread_points, dtype="float64")
    else:
        base = candles["spread"].to_numpy(dtype="float64")
    return base + costs.extra_spread_points


def rolling_median(values: np.ndarray, index: pd.DatetimeIndex, days: int) -> np.ndarray:
    """Median of ``values`` over the last ``days`` days at each bar (that bar included)."""
    if len(values) == 0:
        return np.asarray(values, dtype="float64")
    series = pd.Series(values, index=index)
    return series.rolling(f"{days}D").median().to_numpy(dtype="float64")


# --------------------------------------------------------------------------- #
# Pass 1: signals
# --------------------------------------------------------------------------- #


def _code_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(_ANALYSIS_DIR.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update(Path(__file__).read_bytes())
    return digest.hexdigest()[:16]


def traded_models(settings: Settings) -> list[str]:
    """The setup models a backtest trades: enabled and allowed to trade (D85)."""
    models = settings.config.models
    return [
        name for name in MODELS if getattr(models, name).enabled and getattr(models, name).trade
    ]


def cache_key(
    settings: Settings,
    data: MarketData,
    start: dt.datetime,
    end: dt.datetime,
    model: str = "sweep_mss",
) -> str:
    """Identity of a signal pass: same key, same signals."""
    config = settings.config
    payload = {
        "format": CACHE_FORMAT,
        "model": model,
        "data": data.fingerprint,
        "symbol": data.symbol,
        "point": data.spec.point,
        "start": ensure_utc(start).isoformat(),
        "end": ensure_utc(end).isoformat(),
        # How long an entry waits, and the M1 refinement, change no signal, only the
        # trading: the simulator applies them, so trying others reuses the signal pass.
        "strategy": config.strategy.model_dump(
            mode="json", exclude={"entry_expiry_bars", "refinement"}
        ),
        "timeframes": config.timeframes.model_dump(mode="json"),
        "sessions": config.sessions.model_dump(mode="json"),
        "pressure": config.pressure.model_dump(mode="json"),
        "costs": config.backtest.costs.model_dump(mode="json"),
        "spread_days": config.risk.spread_median_days,
        "code": _code_hash(),
    }
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def generate_signals(
    settings: Settings,
    data: MarketData,
    start: dt.datetime,
    end: dt.datetime,
    *,
    progress: Progress | None = None,
    news_bias: float = 0.0,
    model: str = "sweep_mss",
) -> list[SignalRecord]:
    """Feed every closed trigger bar to the signal engine of ``model``; keep signals
    in ``[start, end)``."""
    config = settings.config
    timeframes = config.timeframes
    trigger = data.trigger
    start, end = ensure_utc(start), ensure_utc(end)
    engine = SignalEngine(
        config.strategy,
        timeframes,
        SessionCalculator(config.sessions),
        point=data.spec.point,
        symbol=data.symbol,
        model=MODELS[model],
    )
    liquidity = aggregate_candles(trigger, timeframes.liquidity)
    hourly = aggregate_candles(trigger, timeframes.bias)
    meter = config.pressure
    usable_meter = meter.timeframe.minutes >= timeframes.trigger.minutes
    pressure_frame = aggregate_candles(trigger, meter.timeframe) if usable_meter else None
    spreads = spread_points(trigger, config.backtest.costs)
    medians = rolling_median(spreads, trigger.index, config.risk.spread_median_days)

    def closes(frame: pd.DataFrame, timeframe: Timeframe) -> np.ndarray:
        return frame.index.as_unit("ns").asi8 + int(timeframe.duration.total_seconds() * 1e9)

    trigger_close = closes(trigger, timeframes.trigger)
    liquidity_close = closes(liquidity, timeframes.liquidity)
    hourly_close = closes(hourly, timeframes.bias)
    pressure_close = closes(pressure_frame, meter.timeframe) if pressure_frame is not None else None
    end_ns = int(pd.Timestamp(end).as_unit("ns").value)
    last = int(np.searchsorted(trigger_close, end_ns, side="right"))
    lookback = engine.lookback
    records: list[SignalRecord] = []
    step = max(1, last // 200)
    for i in range(last):
        now_ns = int(trigger_close[i])
        a = int(np.searchsorted(liquidity_close, now_ns, side="right"))
        b = int(np.searchsorted(hourly_close, now_ns, side="right"))
        median = float(medians[i]) if np.isfinite(medians[i]) else None
        signals = engine.on_bar(
            trigger.iloc[max(0, i - lookback + 1) : i + 1],
            liquidity.iloc[max(0, a - LIQUIDITY_BARS) : a],
            hourly.iloc[max(0, b - HOURLY_BARS) : b],
            spread_points=float(spreads[i]),
            news_bias=news_bias,
            median_spread_points=median,
        )
        for signal in signals:
            if not start <= signal.time < end:
                continue
            reading = None
            if pressure_frame is not None and pressure_close is not None:
                p = int(np.searchsorted(pressure_close, now_ns, side="right"))
                reading = pressure_meter(
                    pressure_frame.iloc[max(0, p - meter.window) : p],
                    meter,
                    hourly=hourly.iloc[max(0, b - STRUCTURE_BARS) : b],
                    news_bias=news_bias,
                    swing_lookback=config.strategy.swing_lookback,
                )
            records.append(SignalRecord(signal, float(spreads[i]), median, reading))
        if progress is not None and (i % step == 0 or i == last - 1):
            progress((i + 1) / last, pd.Timestamp(trigger_close[i], tz="UTC").to_pydatetime())
    return records


def load_or_generate_signals(
    settings: Settings,
    data: MarketData,
    start: dt.datetime,
    end: dt.datetime,
    *,
    cache_dir: Path | None,
    progress: Progress | None = None,
    model: str = "sweep_mss",
) -> tuple[list[SignalRecord], bool]:
    """Signals from the cache when this exact pass ran before, else generated (and cached)."""
    path = None
    if cache_dir is not None:
        path = cache_dir / f"signals-{cache_key(settings, data, start, end, model)}.pickle"
        if path.exists():
            try:
                with path.open("rb") as handle:
                    return pickle.load(handle), True  # our own file, written below
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as exc:
                log.warning("signal cache unreadable; regenerating", path=str(path), error=str(exc))
    records = generate_signals(settings, data, start, end, progress=progress, model=model)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            pickle.dump(records, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)
    return records, False


# --------------------------------------------------------------------------- #
# Pass 2: trades
# --------------------------------------------------------------------------- #


def build_simulator(
    settings: Settings, data: MarketData, *, events: Sequence[NewsEvent] = ()
) -> Simulator:
    config = settings.config
    costs = config.backtest.costs
    point = data.spec.point
    series = PriceSeries.from_candles(
        data.trigger,
        config.timeframes.trigger,
        point=point,
        spread_points=spread_points(data.trigger, costs),
    )
    if config.backtest.use_m1_for_fills and data.fine is not None and not data.fine.empty:
        fine = PriceSeries.from_candles(
            data.fine, Timeframe.M1, point=point, spread_points=spread_points(data.fine, costs)
        )
        series = combine_series(series, fine)
    trail = TrailLevels.from_candles(
        data.trigger, config.timeframes.trigger, config.strategy.swing_lookback
    )
    strategy = config.strategy
    refinement = None
    if strategy.refinement.mode == "m1":
        m1 = data.fine if data.fine is not None else data.trigger.iloc[0:0]
        refinement = M1Refinement(
            strategy.refinement,
            m1,
            atr_period=strategy.atr_period,
            atr_method=strategy.atr_method,
            min_rr=strategy.min_rr,
        )
    return Simulator(
        series,
        data.spec,
        config.risk,
        config.news,
        config.backtest,
        strategy.liquidity.day_boundary,
        trail=trail,
        events=events,
        sessions=SessionCalculator(config.sessions),
        entry_lifetime=config.timeframes.trigger.duration * strategy.entry_expiry_bars,
        refinement=refinement,
    )


def limitations(settings: Settings, data: MarketData, *, events: Sequence[NewsEvent]) -> list[str]:
    """What this backtest cannot see, in plain words. Shown in every report."""
    config = settings.config
    notes: list[str] = []
    if data.synthetic:
        notes.append(
            "These prices are SYNTHETIC (a random walk from the mock broker). The results say "
            "nothing about real gold; they only show that the pipeline works."
        )
    if not events:
        notes.append(
            "No historical economic calendar was stored for this period, so the news "
            "blackout was not applied. Live trading will skip trades near major releases."
        )
    notes.append(
        "The news bias is 0 throughout: the news part of the score and of the pressure meter "
        "is neutral."
    )
    if config.backtest.use_m1_for_fills and (data.fine is None or data.fine.empty):
        notes.append(
            "No one-minute history was stored, so fills and exits were settled on "
            f"{config.timeframes.trigger.value} bars, assuming the worse order inside each bar. "
            "Downloading M1 history (--with-m1) makes this more precise."
        )
    if config.confluence.smt.enabled and (data.silver is None or data.silver.empty):
        notes.append("No silver history was stored, so the gold-silver SMT tag was not computed.")
    refinement = config.strategy.refinement
    if refinement.mode == "m1":
        if data.fine is None or data.fine.empty:
            notes.append(
                "Entries are refined on one-minute bars (strategy.refinement), but no M1 "
                "history was stored, so no setup could be refined and nothing was traded. "
                "Download it with --with-m1."
            )
        else:
            notes.append(
                f"Entries are refined on one-minute bars (D83): {refinement.confirm} turn, "
                f"{refinement.entry} entry. Setups whose turn never came are listed as "
                "no_trigger."
            )
    notes.append(
        "Spreads come from the bars, which do not show the widest moments (news spikes). "
        "backtest.costs.extra_spread_points adds a margin to stress-test this."
    )
    notes.append("Swap (overnight financing) is not charged; most trades close the same day.")
    notes.append(
        "Equity and drawdown are measured when trades close, not while they are open, so the "
        "deepest moment inside a trade is not counted."
    )
    notes.append(
        "The pressure meter uses MT5 tick volume, a count of price changes: spot gold has no "
        "exchange volume, so it is an estimate, not real buy and sell orders."
    )
    return notes


#: Confluence (D85) and trend/structure context (D86) tags, in report order.
CONFLUENCE_TAGS = ("smt", "unicorn", "bpr", "judas", "silver_bullet", "ote", *CONTEXT_TAGS)


def tag_context(
    settings: Settings, data: MarketData, records: Sequence[SignalRecord]
) -> list[SignalRecord]:
    """Add the trend and structure context tags (cheap, so never cached)."""
    if not records:
        return []
    hourly = aggregate_candles(data.trigger, Timeframe.H1)
    frames = ContextFrames.from_hourly(hourly, settings.config.strategy.liquidity.day_boundary)
    return [
        dataclasses.replace(record, signal=with_context(record.signal, frames))
        for record in records
    ]


def tag_smt(
    settings: Settings, data: MarketData, records: Sequence[SignalRecord]
) -> list[SignalRecord]:
    """Add the gold-silver SMT tag where it applies (cheap, so never cached)."""
    smt = settings.config.confluence.smt
    if not smt.enabled or data.silver is None or data.silver.empty:
        return list(records)
    bar = settings.config.timeframes.trigger.duration
    return [
        dataclasses.replace(
            record,
            signal=with_smt(
                record.signal,
                data.trigger,
                data.silver,
                lookback_bars=smt.lookback_bars,
                bar=bar,
            ),
        )
        for record in records
    ]


def by_model(trade: Trade) -> str:
    signal = trade.signal
    return getattr(signal, "model", "sweep_mss") if signal is not None else "sweep_mss"


def by_confluence(trades: Sequence[Trade], balance: float) -> dict[str, Metrics]:
    """Results of the trades carrying each confluence tag (a trade can carry several),
    and of those carrying none."""
    groups: dict[str, Metrics] = {}
    for tag in CONFLUENCE_TAGS:
        tagged = [t for t in trades if t.signal is not None and tag in t.signal.tags]
        if tagged:
            groups[tag] = summarize(tagged, balance)
    plain = [t for t in trades if t.signal is None or not t.signal.tags]
    if plain:
        groups["no tag"] = summarize(plain, balance)
    return groups


def by_playbook(trade: Trade) -> str:
    """Asia, London or New York (the overlap and post-news windows count as New York)."""
    playbook = playbook_session(trade.plan.session)
    return playbook_name(playbook) if playbook is not None else "other"


#: Why a signal outside every session playbook was not traded.
OUTSIDE_PLAYBOOKS = "outside the session playbooks"


def playbook_rules(
    settings: Settings, *, candidates: bool = False
) -> dict[str, PlaybookRule | None]:
    """The rule each session trades by (D87).

    By default the ``trading`` rules, as live trading uses them now. With
    ``candidates`` each session's A+ candidate is traded as if the owner had
    approved it (sessions without a candidate keep their trading rule), to compare.
    """
    books = settings.config.playbooks
    rules: dict[str, PlaybookRule | None] = {}
    for name in PLAYBOOK_SESSIONS:
        book = getattr(books, name)
        rules[name] = (book.candidate or book.trading) if candidates else book.trading
    return rules


def split_by_playbook(
    records: Sequence[SignalRecord],
    rules: dict[str, PlaybookRule | None],
    *,
    neutral_extra: float,
) -> tuple[list[SignalRecord], list[SignalOutcome]]:
    """The records their session's rule takes, and NOT_SELECTED outcomes for the rest."""
    kept: list[SignalRecord] = []
    left: list[SignalOutcome] = []
    for record in records:
        playbook = playbook_session(record.signal.session)
        rule = rules.get(playbook) if playbook is not None else None
        if rule is not None and meets(record.signal, rule, neutral_extra=neutral_extra):
            kept.append(record)
        else:
            left.append(SignalOutcome(record, Outcome.NOT_SELECTED, OUTSIDE_PLAYBOOKS))
    return kept, left


def run_backtest(
    settings: Settings,
    data: MarketData,
    start: dt.datetime,
    end: dt.datetime,
    *,
    run_walk_forward: bool = True,
    cache_dir: Path | None = None,
    progress: Progress | None = None,
    events: Sequence[NewsEvent] = (),
    candidates: bool = False,
) -> BacktestResult:
    """The full backtest: signals, the configured strategy's trades, walk-forward.

    Only the setups their session's playbook rule takes are traded (D87), as in live
    trading; with ``candidates`` the A+ candidates are traded as if approved.
    """
    config = settings.config
    strategy = config.strategy
    start, end = ensure_utc(start), ensure_utc(end)
    if end <= start:
        raise ValueError("the backtest needs an end after its start")
    records: list[SignalRecord] = []
    cached = True
    for model in traded_models(settings):
        found, reused = load_or_generate_signals(
            settings, data, start, end, cache_dir=cache_dir, progress=progress, model=model
        )
        records += found
        cached = cached and reused
    records.sort(key=lambda record: record.time)
    records = tag_context(settings, data, tag_smt(settings, data, records))
    simulator = build_simulator(settings, data, events=events)
    threshold = (
        strategy.auto_trade_threshold
        if config.backtest.trade_threshold == "auto_trade"
        else strategy.alert_threshold
    )
    neutral_extra = strategy.bias.neutral_extra_score
    balance = config.backtest.initial_balance
    rules = playbook_rules(settings, candidates=candidates)
    tradeable, outside = split_by_playbook(records, rules, neutral_extra=neutral_extra)
    result = simulator.run(
        tradeable,
        select=make_selector(threshold, strategy.min_rr, neutral_extra),
        start=start,
        end=end,
    )
    window = [o for o in outside if start <= o.record.time < end]
    result.outcomes.extend(window)
    trades = result.trades
    breakdowns: dict[str, dict[str, Metrics]] = {
        "playbook": breakdown(
            trades, by_playbook, balance, order=[playbook_name(p) for p in PLAYBOOK_SESSIONS]
        ),
        "session": breakdown(trades, by_session, balance, order=[ASIA, LONDON, NEW_YORK, OVERLAP]),
        "weekday": breakdown(trades, by_weekday, balance, order=list(WEEKDAYS)),
        "direction": breakdown(trades, by_direction, balance, order=["LONG", "SHORT"]),
        "pressure": breakdown(
            trades,
            by_pressure,
            balance,
            order=["meter agreed", "meter disagreed", "meter balanced", "no reading"],
        ),
        "model": breakdown(trades, by_model, balance, order=list(MODELS)),
        "confluence": by_confluence(trades, balance),
    }
    wf = None
    if run_walk_forward:
        wf = walk_forward(
            simulator,
            tradeable,
            config=config.backtest.walk_forward,
            start=start,
            end=end,
            defaults=Combo(threshold, strategy.min_rr, strategy.entry_expiry_bars),
            neutral_extra=neutral_extra,
            initial_balance=balance,
            bar=config.timeframes.trigger.duration,
        )
    settings_json = json.dumps(config.model_dump(mode="json"), sort_keys=True, default=str)
    return BacktestResult(
        start=start,
        end=end,
        data=data,
        records=records,
        result=result,
        metrics=summarize(trades, balance),
        breakdowns=breakdowns,
        walk_forward=wf,
        limitations=limitations(settings, data, events=events),
        fingerprints={
            "data": data.fingerprint,
            "settings": hashlib.sha256(settings_json.encode()).hexdigest()[:16],
            "code": _code_hash(),
        },
        signals_cached=cached,
        settings_snapshot=config.model_dump(mode="json"),
        playbooks={
            playbook_name(name): describe_rule(rule) if rule is not None else "none"
            for name, rule in rules.items()
        },
        candidates=candidates,
    )

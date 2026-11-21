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

import datetime as dt
import hashlib
import json
import pickle
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from goldsweep.analysis.pressure import STRUCTURE_BARS, pressure_meter
from goldsweep.analysis.signals import SignalEngine
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
    SignalRecord,
    SimulationResult,
    Simulator,
    TrailLevels,
    combine_series,
)
from goldsweep.backtest.walkforward import Combo, WalkForwardResult, make_selector, walk_forward
from goldsweep.core.clock import LONDON, NEW_YORK, OVERLAP, SessionCalculator, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import CostConfig, Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import SymbolSpec
from goldsweep.data.candles import aggregate_candles
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


def cache_key(settings: Settings, data: MarketData, start: dt.datetime, end: dt.datetime) -> str:
    """Identity of a signal pass: same key, same signals."""
    config = settings.config
    payload = {
        "format": CACHE_FORMAT,
        "data": data.fingerprint,
        "symbol": data.symbol,
        "point": data.spec.point,
        "start": ensure_utc(start).isoformat(),
        "end": ensure_utc(end).isoformat(),
        # How long an entry waits changes no signal, only the trading: it is applied
        # by the simulator, so trying other expiries reuses the same signal pass.
        "strategy": config.strategy.model_dump(mode="json", exclude={"entry_expiry_bars"}),
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
) -> list[SignalRecord]:
    """Feed every closed trigger bar to the signal engine; keep signals in ``[start, end)``."""
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
) -> tuple[list[SignalRecord], bool]:
    """Signals from the cache when this exact pass ran before, else generated (and cached)."""
    path = None
    if cache_dir is not None:
        path = cache_dir / f"signals-{cache_key(settings, data, start, end)}.pickle"
        if path.exists():
            try:
                with path.open("rb") as handle:
                    return pickle.load(handle), True  # our own file, written below
            except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as exc:
                log.warning("signal cache unreadable; regenerating", path=str(path), error=str(exc))
    records = generate_signals(settings, data, start, end, progress=progress)
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
    return Simulator(
        series,
        data.spec,
        config.risk,
        config.news,
        config.backtest,
        config.strategy.liquidity.day_boundary,
        trail=trail,
        events=events,
        sessions=SessionCalculator(config.sessions),
        entry_lifetime=config.timeframes.trigger.duration * config.strategy.entry_expiry_bars,
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
) -> BacktestResult:
    """The full backtest: signals, the configured strategy's trades, walk-forward."""
    config = settings.config
    strategy = config.strategy
    start, end = ensure_utc(start), ensure_utc(end)
    if end <= start:
        raise ValueError("the backtest needs an end after its start")
    records, cached = load_or_generate_signals(
        settings, data, start, end, cache_dir=cache_dir, progress=progress
    )
    simulator = build_simulator(settings, data, events=events)
    threshold = (
        strategy.auto_trade_threshold
        if config.backtest.trade_threshold == "auto_trade"
        else strategy.alert_threshold
    )
    neutral_extra = strategy.bias.neutral_extra_score
    balance = config.backtest.initial_balance
    result = simulator.run(
        records,
        select=make_selector(threshold, strategy.min_rr, neutral_extra),
        start=start,
        end=end,
    )
    trades = result.trades
    breakdowns: dict[str, dict[str, Metrics]] = {
        "session": breakdown(trades, by_session, balance, order=[LONDON, NEW_YORK, OVERLAP]),
        "weekday": breakdown(trades, by_weekday, balance, order=list(WEEKDAYS)),
        "direction": breakdown(trades, by_direction, balance, order=["LONG", "SHORT"]),
        "pressure": breakdown(
            trades,
            by_pressure,
            balance,
            order=["meter agreed", "meter disagreed", "meter balanced", "no reading"],
        ),
    }
    wf = None
    if run_walk_forward:
        wf = walk_forward(
            simulator,
            records,
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
    )

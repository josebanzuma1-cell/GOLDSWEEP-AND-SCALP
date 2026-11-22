"""Signals: sweep -> market-structure shift -> entry zone -> scored signal.

This is the "best entry after liquidation" engine of spec 5.3 to 5.7, built on
the pure functions in the other analysis modules. The workflow is the one the
liquidity guide teaches:

1. Know the higher-timeframe bias.
2. Know where the liquidity is.
3. Wait for a pool to be raided and for structure to shift.
4. Enter from the fair value gap (or inversion gap, or order block) left behind.
5. Target the next pool of external liquidity.

:class:`SignalEngine` is fed one closed trigger bar at a time. It keeps a small
amount of state (which pools exist, which have been raided, how far each raid
has progressed) and nothing else, so a live run and a backtest fed the same
bars produce the same signals.

The engine never looks ahead: it only ever receives closed bars, a swing only
counts once its confirming bars have closed, and a gap only once its third
candle has closed.

The engine decides *whether a setup exists and how good it is*. Whether an order
is actually sent (news blackout, spread, loss limits, position limits) is the
risk manager's decision in a later phase.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldsweep.analysis.bias import Bias, BiasResult, compute_bias
from goldsweep.analysis.crt import CrtSetup, active_crt, crt_pools, detect_crt, range_candles
from goldsweep.analysis.indicators import atr_values, latest_atr
from goldsweep.analysis.liquidity import (
    DayLevels,
    Pool,
    PoolKind,
    Side,
    SweepEvent,
    SweepStatus,
    day_levels,
    day_week_pools,
    detect_sweep,
    merge_pools,
    round_number_pools,
    session_pools,
    swing_pools,
)
from goldsweep.analysis.periods import closed_as_of
from goldsweep.analysis.structure import (
    Direction,
    DisplacementRule,
    MssEvent,
    Swing,
    displacement_atr,
    find_cisd,
    find_mss,
    find_swings,
    strongest_displacement,
)
from goldsweep.analysis.zones import (
    Zone,
    ZoneKind,
    find_breaker,
    find_fvgs,
    find_inversions,
    find_order_block,
    first_touch,
    ote_price,
    overlaps,
)
from goldsweep.core.clock import (
    ASIA,
    LONDON,
    NEW_YORK,
    OVERLAP,
    SessionCalculator,
    ensure_utc,
)
from goldsweep.core.logging import get_logger

if TYPE_CHECKING:
    from goldsweep.core.settings import Settings, StrategyConfig, TimeframesConfig

log = get_logger(__name__)

#: Session name of a setup allowed only by post-news sweep mode (spec 5.6).
POST_NEWS = "post_news"
_SESSION_CODES = {
    LONDON: "LDN",
    NEW_YORK: "NY",
    ASIA: "ASIA",
    OVERLAP: "OVL",
    POST_NEWS: "NEWS",
}
_CRT_KINDS = frozenset({PoolKind.CRT_HIGH, PoolKind.CRT_LOW})
_ZONE_SHARE = {
    ZoneKind.FVG: 1.0,
    ZoneKind.IFVG: 0.8,
    ZoneKind.ORDER_BLOCK: 0.5,
    ZoneKind.BREAKER: 0.5,
}
_NEW_YORK_TZ = ZoneInfo("America/New_York")
#: ICT's Silver Bullet hours, in New York time: a confluence tag (D85; the model
#: that traded only these hours was replaced by session playbooks, D86).
SILVER_BULLET_WINDOWS = (
    (dt.time(3), dt.time(4)),
    (dt.time(10), dt.time(11)),
    (dt.time(14), dt.time(15)),
)


def in_silver_bullet(moment: dt.datetime) -> bool:
    """True inside one of the New York 03-04, 10-11 or 14-15 hours."""
    local = ensure_utc(moment).astimezone(_NEW_YORK_TZ).time()
    return any(start <= local < end for start, end in SILVER_BULLET_WINDOWS)


def ny_midnight_open(bars: pd.DataFrame, now: dt.datetime) -> float | None:
    """The open of the bar that starts at midnight New York on the New York date of
    ``now`` (the Judas reference, D85), or ``None`` when that bar is not in ``bars``."""
    local = ensure_utc(now).astimezone(_NEW_YORK_TZ)
    midnight = pd.Timestamp(
        dt.datetime.combine(local.date(), dt.time(0), _NEW_YORK_TZ).astimezone(dt.UTC)
    )
    position = int(bars.index.searchsorted(midnight))
    if position < len(bars) and bars.index[position] == midnight:
        return float(bars["open"].iloc[position])
    return None


#: The stages of a raid that has not become a signal yet, as the chart names them.
FORMING_STAGES = {
    "pierced": "pierced: waiting for a close back inside",
    "swept": "swept: waiting for the structure shift",
    "shifted": "shifted: the entry gap completes on the next bar",
}


@dataclass(frozen=True, slots=True)
class FormingSetup:
    """A raid of a liquidity pool on its way to becoming a signal (S8, D114).

    It may still fail (no close back inside, no shift in time, price runs back
    through the extreme): it is a mark to watch, not an order.
    """

    pool: Pool
    direction: Direction  # the trade it would be
    stage: str  # pierced / swept / shifted (see FORMING_STAGES)
    since: dt.datetime  # open time of the bar that pierced the pool (UTC)
    extreme: float | None  # the raid's furthest price so far
    judas: bool  # the raid went beyond the New York midnight open
    model: str

    @property
    def describe(self) -> str:
        return FORMING_STAGES.get(self.stage, self.stage)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """How a setup model differs from the spec's sweep -> MSS -> gap model (D85)."""

    name: str
    code: str  # added to signal ids; "" for the spec's model
    confirmation: str = "mss"  # mss | cisd
    pools: str = "external"  # external | internal (only with the H1 trend)
    kind: str = "sweep"  # sweep (fade the raid) | breakout (follow the break, D86)


SWEEP_MSS = ModelSpec("sweep_mss", "")
MODELS: dict[str, ModelSpec] = {
    "sweep_mss": SWEEP_MSS,
    "cisd": ModelSpec("cisd", "CISD", confirmation="cisd"),
    "continuation": ModelSpec("continuation", "CONT", pools="internal"),
    "breakout": ModelSpec("breakout", "BRK", kind="breakout"),
}
#: Levels the breakout model trades the break of: the session ranges and the
#: previous day (the Asian-range break at the London open, and the like).
BREAKOUT_KINDS = frozenset(
    {
        PoolKind.ASIAN_HIGH,
        PoolKind.ASIAN_LOW,
        PoolKind.LONDON_HIGH,
        PoolKind.LONDON_LOW,
        PoolKind.NEWYORK_HIGH,
        PoolKind.NEWYORK_LOW,
        PoolKind.PREVIOUS_DAY_HIGH,
        PoolKind.PREVIOUS_DAY_LOW,
    }
)
_SECONDARY_SESSION_SHARE = 0.6  # overlap and Asia score less than the two kill zones
_EPSILON = 1e-9
_ORDER_BLOCK_LOOKBACK = 5

PoolKey = tuple[str, str, float]


@dataclass(frozen=True, slots=True)
class Signal:
    """A complete, priced and scored setup.

    Index fields inside ``sweep``, ``mss`` and ``zone`` refer to the engine's
    internal window of bars; use their time fields outside the engine.
    """

    id: str
    symbol: str
    time: dt.datetime  # close of the bar that completed the setup (UTC)
    direction: Direction
    session: str | None
    entry_mode: str
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None
    risk_reward_1: float
    risk_reward_2: float | None
    score: float  # 0..100
    components: Mapping[str, float]  # points per scoring component
    alert_threshold: float
    auto_trade_threshold: float
    sweep: SweepEvent
    mss: MssEvent
    zone: Zone | None
    target_1: Pool
    target_2: Pool | None
    bias: BiasResult
    atr: float  # trigger-timeframe ATR when the signal formed
    spread: float  # spread in price units when the signal formed
    expires_at: dt.datetime  # an unfilled entry order is cancelled at this time
    notes: tuple[str, ...] = ()
    #: The setup model that found it (D85).
    model: str = "sweep_mss"
    #: Confluences present (unicorn, bpr, judas, silver_bullet, ote, smt); they do
    #: not change the score, they are reported so their value can be measured.
    tags: tuple[str, ...] = ()

    @property
    def pool(self) -> Pool:
        """The liquidity pool that was swept."""
        return self.sweep.pool

    @property
    def risk(self) -> float:
        """Distance from entry to stop, in price."""
        return abs(self.entry - self.stop_loss)

    @property
    def meets_alert(self) -> bool:
        return self.score >= self.alert_threshold

    @property
    def meets_auto_trade(self) -> bool:
        return self.score >= self.auto_trade_threshold


@dataclass(frozen=True, slots=True)
class Rejection:
    """A raid that did not become a signal, and why. Kept for diagnostics."""

    time: dt.datetime
    pool: Pool
    direction: Direction
    reason: str


class _Stage(StrEnum):
    RESTING = "resting"  # the pool is intact
    PIERCED = "pierced"  # price is beyond it; will it close back inside?
    TRACKING = "tracking"  # swept; waiting for the market-structure shift
    AWAIT_ZONE = "await_zone"  # shifted; one more bar completes the displacement's gap


@dataclass(slots=True)
class _Track:
    pool: Pool
    stage: _Stage = _Stage.RESTING
    pierce_time: dt.datetime | None = None
    min_sweep: float = 0.0  # frozen when the pool is pierced
    mss_time: dt.datetime | None = None


class SignalEngine:
    """Turns closed bars into signals. Call :meth:`on_bar` once per closed trigger bar."""

    def __init__(
        self,
        strategy: StrategyConfig,
        timeframes: TimeframesConfig,
        sessions: SessionCalculator,
        *,
        point: float,
        symbol: str = "XAUUSD",
        window: int = 300,
        model: ModelSpec = SWEEP_MSS,
    ) -> None:
        """Create an engine.

        Args:
            strategy: the ``strategy`` section of config.yaml.
            timeframes: bias / liquidity / trigger timeframes.
            sessions: session calculator for the kill-zone gate.
            point: the symbol's point size, to turn a spread in points into price.
            symbol: used in signal ids and alerts only.
            window: how many trigger bars the engine looks back over.
            model: which setup model this engine looks for (D85).
        """
        if point <= 0:
            raise ValueError("point must be positive")
        self._model = model
        self._cfg = strategy
        self._tf = timeframes
        self._sessions = sessions
        self._point = point
        self._symbol = symbol
        self._window = max(
            window,
            strategy.atr_period * 3,
            strategy.mss_structure_lookback + strategy.mss_window + 20,
        )
        self._warmup = strategy.max_bars_back_inside + strategy.mss_window + 3
        self._rule = DisplacementRule.from_config(strategy)
        self._tracks: dict[PoolKey, _Track] = {}
        self._taken: dict[PoolKey, dt.datetime] = {}
        self._counters: dict[tuple[str, str], int] = {}
        self._rejections: list[Rejection] = []
        self._last_bar: dt.datetime | None = None
        self._last_liquidity_bar: pd.Timestamp | None = None
        self._last_hourly_bar: pd.Timestamp | None = None
        self._levels: DayLevels | None = None
        self._crt_setups: list[CrtSetup] = []
        self._crt_pools: list[Pool] = []
        self._min_sweep: float | None = None
        self._tolerance = 0.0
        self._bias: BiasResult | None = None
        self._news_windows: tuple[tuple[dt.datetime, dt.datetime], ...] = ()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        point: float,
        symbol: str = "XAUUSD",
        model: ModelSpec = SWEEP_MSS,
    ) -> SignalEngine:
        config = settings.config
        return cls(
            config.strategy,
            config.timeframes,
            SessionCalculator(config.sessions),
            point=point,
            symbol=symbol,
            model=model,
        )

    @property
    def model(self) -> ModelSpec:
        return self._model

    @property
    def _turn_name(self) -> str:
        if self._model.confirmation == "cisd":
            return "change in the state of delivery"
        return "market-structure shift"

    # -- what the engine knows -------------------------------------------- #

    @property
    def pools(self) -> list[Pool]:
        """Every pool being tracked, lowest price first. Raided ones have ``swept`` set."""
        return sorted((track.pool for track in self._tracks.values()), key=lambda pool: pool.price)

    @property
    def lookback(self) -> int:
        """Trigger bars the engine looks back over: callers may pass just this many."""
        return self._window

    @property
    def bias(self) -> BiasResult | None:
        """The bias computed for the most recent setup, if any."""
        return self._bias

    @property
    def rejections(self) -> list[Rejection]:
        """Raids from the latest bar that did not become signals."""
        return list(self._rejections)

    @property
    def day_levels(self) -> DayLevels | None:
        return self._levels

    def forming(self, trigger: pd.DataFrame) -> list[FormingSetup]:
        """Raids under way: pools pierced, swept or shifted but not yet a signal.

        ``trigger`` is the latest trigger bars (the extreme so far and the New York
        midnight open are read from them). Read-only: nothing in the engine changes.
        """
        names = {_Stage.PIERCED: "pierced", _Stage.TRACKING: "swept", _Stage.AWAIT_ZONE: "shifted"}
        found: list[FormingSetup] = []
        last = trigger.index[-1].to_pydatetime() if len(trigger) else None
        midnight_open = ny_midnight_open(trigger, last) if last is not None else None
        for track in self._tracks.values():
            stage = names.get(track.stage)
            if stage is None or track.pierce_time is None:
                continue
            pool = track.pool
            direction = pool.side.trade_direction
            since = trigger.loc[trigger.index >= pd.Timestamp(track.pierce_time)]
            extreme: float | None = None
            if not since.empty:
                extreme = (
                    float(since["high"].max())
                    if pool.side is Side.BUY_SIDE
                    else float(since["low"].min())
                )
            judas = (
                extreme is not None
                and midnight_open is not None
                and (midnight_open - extreme) * direction.sign > 0
            )
            found.append(
                FormingSetup(
                    pool, direction, stage, track.pierce_time, extreme, judas, self._model.name
                )
            )
        return sorted(found, key=lambda f: f.since)

    @property
    def crt_setups(self) -> list[CrtSetup]:
        return list(self._crt_setups)

    def reset(self) -> None:
        """Forget everything (used between backtest runs)."""
        self._tracks.clear()
        self._taken.clear()
        self._counters.clear()
        self._rejections = []
        self._last_bar = None
        self._last_liquidity_bar = None
        self._last_hourly_bar = None
        self._levels = None
        self._crt_setups, self._crt_pools = [], []
        self._min_sweep, self._tolerance, self._bias = None, 0.0, None

    # -- the main entry point --------------------------------------------- #

    def on_bar(
        self,
        trigger: pd.DataFrame,
        liquidity: pd.DataFrame,
        hourly: pd.DataFrame,
        *,
        spread_points: float,
        news_bias: float = 0.0,
        median_spread_points: float | None = None,
        news_windows: Sequence[tuple[dt.datetime, dt.datetime]] = (),
    ) -> list[Signal]:
        """Process the newest closed trigger bar and return any signals it completes.

        Args:
            trigger: closed trigger-timeframe (M5) bars; the last row is the bar
                that just closed.
            liquidity: closed liquidity-timeframe (M15) bars.
            hourly: closed bias-timeframe (H1) bars.
            spread_points: current spread, in broker points.
            news_bias: news score in -1..+1 (0 until the news layer exists).
            median_spread_points: the usual spread, if the caller measures it.
            news_windows: post-news sweep windows (15 to 90 minutes after a
                high-impact release). A setup whose bar opens inside one is
                allowed outside the kill zones, never inside a hard block.

        Bars that were missed since the previous call are replayed in order
        first, so the engine's state is the same as if it had seen every bar.
        Signals are only returned for the newest bar. Every signal that forms is
        returned with its score; whether it clears the alert or auto-trade
        threshold is the caller's question (``meets_alert``, ``meets_auto_trade``).
        """
        self._rejections = []
        self._news_windows = tuple(news_windows)
        if trigger.empty:
            return []
        if self._last_bar is None:
            first = max(0, len(trigger) - self._warmup)
        else:
            first = int(trigger.index.searchsorted(pd.Timestamp(self._last_bar), side="right"))
        newest = len(trigger) - 1
        signals: list[Signal] = []
        for position in range(first, newest + 1):
            now = trigger.index[position].to_pydatetime() + self._tf.trigger.duration
            self._rejections = []
            signals = self._step(
                trigger.iloc[: position + 1],
                closed_as_of(liquidity, self._tf.liquidity, now),
                closed_as_of(hourly, self._tf.bias, now),
                now,
                spread_points,
                news_bias,
                median_spread_points,
            )
        self._last_bar = trigger.index[newest].to_pydatetime()
        return [self._with_id(signal) for signal in signals] if first <= newest else []

    # -- one bar ----------------------------------------------------------- #

    def _step(
        self,
        trigger: pd.DataFrame,
        liquidity: pd.DataFrame,
        hourly: pd.DataFrame,
        now: dt.datetime,
        spread_points: float,
        news_bias: float,
        median_spread: float | None,
    ) -> list[Signal]:
        cfg = self._cfg
        window = trigger.iloc[-self._window :] if len(trigger) > self._window else trigger
        if len(window) < cfg.atr_period + 2:
            return []
        open_ = window["open"].to_numpy(dtype="float64")
        high = window["high"].to_numpy(dtype="float64")
        low = window["low"].to_numpy(dtype="float64")
        close = window["close"].to_numpy(dtype="float64")
        atr = atr_values(high, low, close, cfg.atr_period, cfg.atr_method)
        last = len(window) - 1
        if not np.isfinite(atr[last]) or atr[last] <= 0:
            return []
        bar_open = window.index[last].to_pydatetime()

        self._refresh(now, liquidity, hourly, float(close[last]))
        if self._min_sweep is None:
            return []  # no liquidity-timeframe ATR yet, so a sweep cannot be judged

        swings: list[Swing] | None = None
        found: list[tuple[PoolKey, Signal]] = []
        for key, track in list(self._tracks.items()):
            pool = track.pool
            if track.stage is _Stage.RESTING:
                if pool.expires is not None and now > pool.expires:
                    del self._tracks[key]
                    continue
                if bar_open < pool.created:
                    continue
                beyond = (
                    (high[last] - pool.price)
                    if pool.side is Side.BUY_SIDE
                    else (pool.price - low[last])
                )
                if beyond < self._min_sweep - _EPSILON:
                    continue
                # The liquidity has been taken, whatever happens next.
                self._taken[key] = pool.expires or now + dt.timedelta(
                    hours=cfg.liquidity.max_pool_age_hours
                )
                track.pool = pool = pool.as_swept()
                if self._model.kind == "breakout":
                    if not BREAKOUT_KINDS.intersection(pool.sources):
                        del self._tracks[key]  # breakouts of session ranges and the day only
                        continue
                elif self._model.pools == "internal":
                    if pool.is_external:  # the continuation model raids internal pools only
                        del self._tracks[key]
                        continue
                elif pool.strength < cfg.liquidity.min_sweep_pool_strength:
                    del self._tracks[key]
                    continue
                track.stage, track.pierce_time, track.min_sweep = (
                    _Stage.PIERCED,
                    bar_open,
                    self._min_sweep,
                )

            pierce = self._position(window, track.pierce_time)
            if pierce is None:
                del self._tracks[key]
                continue
            result = detect_sweep(
                window,
                pool,
                start=pierce,
                min_sweep=track.min_sweep,
                max_bars_back_inside=cfg.max_bars_back_inside,
            )
            direction = pool.side.trade_direction
            if result.status is SweepStatus.PENDING:
                continue
            if self._model.kind == "breakout":
                del self._tracks[key]
                if result.status is SweepStatus.SWEPT:
                    self._reject(now, pool, direction, "swept back inside: not a breakout")
                    continue
                signal = self._breakout(
                    window,
                    atr,
                    pool,
                    pierce,
                    hourly,
                    now,
                    bar_open,
                    spread_points,
                    news_bias,
                    median_spread,
                )
                if signal is not None:
                    found.append((key, signal))
                continue
            if result.status is not SweepStatus.SWEPT or result.event is None:
                self._reject(now, pool, direction, "the level broke: no close back inside in time")
                del self._tracks[key]
                continue
            if track.stage is _Stage.PIERCED:
                track.stage = _Stage.TRACKING
            if swings is None:
                swings = find_swings(window, cfg.swing_lookback)
            earliest_swing = max(0, result.event.pierce_index - cfg.mss_structure_lookback)

            if track.stage is _Stage.TRACKING:
                sweep = self._extended(result.event, high, low, last)
                if last - sweep.reclaim_index > cfg.mss_window:
                    self._reject(
                        now,
                        pool,
                        direction,
                        f"no {self._turn_name} within {cfg.mss_window} bars",
                    )
                    del self._tracks[key]
                    continue
                mss = self._confirm(window, atr, swings, direction, sweep, earliest_swing)
                if mss is None:
                    continue
                track.mss_time = mss.time
                shifted_by_displacement = (
                    displacement_atr(
                        open_[mss.index],
                        high[mss.index],
                        low[mss.index],
                        close[mss.index],
                        atr[mss.index - 1],
                        direction,
                        self._rule,
                    )
                    is not None
                )
                if cfg.entry_mode != "market" and mss.index == last and shifted_by_displacement:
                    track.stage = _Stage.AWAIT_ZONE  # its own gap needs one more bar to complete
                    continue
            else:  # AWAIT_ZONE: the bar after the shift has closed
                shift = self._position(window, track.mss_time)
                if shift is None:
                    del self._tracks[key]
                    continue
                sweep = self._extended(result.event, high, low, shift)
                mss = self._confirm(
                    window.iloc[: shift + 1],
                    atr[: shift + 1],
                    swings,
                    direction,
                    sweep,
                    earliest_swing,
                )
                later = (low if direction is Direction.LONG else high)[shift + 1 : last + 1]
                if mss is None or ((sweep.extreme - later) * direction.sign > 0).any():
                    self._reject(now, pool, direction, "price ran back through the sweep extreme")
                    del self._tracks[key]
                    continue

            signal = self._build(
                window,
                atr,
                sweep,
                mss,
                hourly,
                now,
                bar_open,
                spread_points,
                news_bias,
                median_spread,
            )
            del self._tracks[key]
            if signal is not None:
                found.append((key, signal))

        return self._one_per_raid(found)

    def _breakout(
        self,
        window: pd.DataFrame,
        atr: np.ndarray,
        pool: Pool,
        pierce: int,
        hourly: pd.DataFrame,
        now: dt.datetime,
        bar_open: dt.datetime,
        spread_points: float,
        news_bias: float,
        median_spread: float | None,
    ) -> Signal | None:
        """A session-range or previous-day level that broke and held (D86).

        The trade follows the break: a long when highs broke. The bar that first
        closed beyond the level plays the part of the shift (it must belong to a leg
        with a displacement candle); the leg's start plays the part of the sweep's
        extreme (the stop goes beyond it); the entry is the gap the leg left, on the
        retest. Everything after that is the spec's pipeline (:meth:`_build`).
        """
        cfg = self._cfg
        direction = pool.side.trade_direction.opposite
        sign = direction.sign
        open_ = window["open"].to_numpy(dtype="float64")
        high = window["high"].to_numpy(dtype="float64")
        low = window["low"].to_numpy(dtype="float64")
        close = window["close"].to_numpy(dtype="float64")
        last = len(window) - 1
        beyond = np.flatnonzero((close[pierce : last + 1] - pool.price) * sign > 0)
        if len(beyond) == 0:
            return None
        broke = pierce + int(beyond[0])
        first = max(0, pierce - cfg.mss_window)
        leg = (low if direction is Direction.LONG else high)[first : pierce + 1]
        offset = int(np.argmin(leg) if direction is Direction.LONG else np.argmax(leg))
        origin_index = first + offset
        origin = float(leg[offset])
        best = strongest_displacement(
            open_, high, low, close, atr, direction, max(origin_index, 1), last, self._rule
        )
        if best is None:
            self._reject(now, pool, direction, "no displacement in the breakout")
            return None
        sweep = SweepEvent(
            pool=pool,
            direction=direction,
            pierce_index=pierce,
            pierce_time=window.index[pierce].to_pydatetime(),
            reclaim_index=broke,
            reclaim_time=window.index[broke].to_pydatetime(),
            extreme=origin,
            extreme_index=origin_index,
            depth=abs(origin - pool.price),
            bars_to_reclaim=broke - pierce,
        )
        mss = MssEvent(
            direction=direction,
            index=broke,
            time=window.index[broke].to_pydatetime(),
            level=pool.price,
            swing_index=pierce,
            displacement_index=best[0],
            displacement_atr=best[1],
        )
        return self._build(
            window, atr, sweep, mss, hourly, now, bar_open, spread_points, news_bias, median_spread
        )

    def _confirm(
        self,
        window: pd.DataFrame,
        atr: np.ndarray,
        swings: list[Swing],
        direction: Direction,
        sweep: SweepEvent,
        earliest_swing: int,
    ) -> MssEvent | None:
        """The turn that confirms a sweep: the MSS, or a CISD for that model (D85)."""
        cfg = self._cfg
        if self._model.confirmation == "cisd":
            return find_cisd(
                window,
                atr,
                direction,
                extreme_index=sweep.extreme_index,
                reclaim_index=sweep.reclaim_index,
                window=cfg.mss_window,
                rule=self._rule,
            )
        return find_mss(
            window,
            atr,
            swings,
            direction,
            extreme_index=sweep.extreme_index,
            reclaim_index=sweep.reclaim_index,
            earliest_swing_index=earliest_swing,
            window=cfg.mss_window,
            rule=self._rule,
        )

    # -- context: pools, levels, CRT ---------------------------------------- #

    def _refresh(
        self, now: dt.datetime, liquidity: pd.DataFrame, hourly: pd.DataFrame, price: float
    ) -> None:
        cfg, liq = self._cfg, self._cfg.liquidity
        hourly_bar = hourly.index[-1] if len(hourly) else None
        if (
            self._levels is None
            or hourly_bar != self._last_hourly_bar
            or now >= self._levels.day_end
        ):
            self._last_hourly_bar = hourly_bar
            self._levels = day_levels(hourly, now, liq.day_boundary)
            if cfg.crt.enabled and len(hourly):
                # 16 candles' worth of hours: more than the 12 complete candles used.
                hours = 16 * cfg.crt.timeframe.minutes // 60
                recent = hourly.iloc[-hours:] if len(hourly) > hours else hourly
                ranges = range_candles(recent, cfg.crt.timeframe, liq.day_boundary, now)
                self._crt_setups = detect_crt(ranges.tail(12), cfg.crt.timeframe)
                self._crt_pools = crt_pools(ranges, cfg.crt.timeframe)

        liquidity_bar = liquidity.index[-1] if len(liquidity) else None
        if liquidity_bar == self._last_liquidity_bar and self._min_sweep is not None:
            return
        self._last_liquidity_bar = liquidity_bar
        atr_liquidity = latest_atr(liquidity, cfg.atr_period, cfg.atr_method)
        if atr_liquidity is None or atr_liquidity <= 0:
            self._min_sweep = None
            return
        self._min_sweep = cfg.min_sweep_atr * atr_liquidity
        self._tolerance = cfg.eq_tolerance_atr * atr_liquidity
        pools = [
            *day_week_pools(self._levels),
            *session_pools(liquidity, self._sessions, now, liq.day_boundary),
            *swing_pools(
                liquidity.iloc[-400:] if len(liquidity) > 400 else liquidity,
                now,
                liq,
                lookback=cfg.swing_lookback,
                tolerance=self._tolerance,
                bar_minutes=self._tf.liquidity.minutes,
            ),
            *round_number_pools(price, now, liq),
            *(self._crt_pools if cfg.crt.enabled else ()),
        ]
        self._sync(merge_pools(pools, self._tolerance), now)

    def _sync(self, pools: list[Pool], now: dt.datetime) -> None:
        """Bring the tracked pools in line with the pools that exist now."""
        self._taken = {key: until for key, until in self._taken.items() if until > now}
        fresh = {
            pool.key: pool
            for pool in pools
            if pool.key not in self._taken and (pool.expires is None or pool.expires > now)
        }
        for key, track in list(self._tracks.items()):
            if track.stage is _Stage.RESTING and key not in fresh:
                del self._tracks[key]  # the level no longer exists
        for key, pool in fresh.items():
            track = self._tracks.get(key)
            if track is None:
                self._tracks[key] = _Track(pool)
            elif track.stage is _Stage.RESTING:
                track.pool = pool

    # -- building a signal ------------------------------------------------- #

    def _build(
        self,
        window: pd.DataFrame,
        atr: np.ndarray,
        sweep: SweepEvent,
        mss: MssEvent,
        hourly: pd.DataFrame,
        now: dt.datetime,
        bar_open: dt.datetime,
        spread_points: float,
        news_bias: float,
        median_spread: float | None,
    ) -> Signal | None:
        cfg = self._cfg
        direction, sign = sweep.direction, sweep.direction.sign
        pool = sweep.pool
        high = window["high"].to_numpy(dtype="float64")
        low = window["low"].to_numpy(dtype="float64")
        close = window["close"].to_numpy(dtype="float64")
        last = len(window) - 1
        price = float(close[last])

        zone = self._pick_zone(window, atr, sweep, mss, price)
        leg = (high if direction is Direction.LONG else low)[sweep.extreme_index : last + 1]
        leg_end = float(leg.max() if direction is Direction.LONG else leg.min())
        if cfg.entry_mode == "market":
            entry = price
        elif zone is None:
            self._reject(
                now, pool, direction, "no usable fair value gap, inversion gap or order block"
            )
            return None
        elif cfg.entry_mode == "ote":
            entry = ote_price(zone, sweep.extreme, leg_end, (cfg.fib_low + cfg.fib_high) / 2.0)
        else:
            entry = zone.price_for(cfg.entry_mode)

        spread_price = spread_points * self._point
        stop = sweep.extreme - sign * (cfg.sl_buffer_atr * float(atr[last]) + spread_price)
        risk = (entry - stop) * sign
        if risk <= _EPSILON:
            self._reject(
                now, pool, direction, "the entry is not on the profitable side of the stop"
            )
            return None

        targets = self._targets(direction, entry)
        if not targets:
            self._reject(now, pool, direction, "no external liquidity pool to target")
            return None
        take_profit_1 = self._target_price(targets[0], direction, spread_price)
        reward_1 = (take_profit_1 - entry) * sign / risk
        if reward_1 < cfg.min_rr - _EPSILON:
            self._reject(
                now,
                pool,
                direction,
                f"R:R to {targets[0].label} is {reward_1:.2f}, below the minimum {cfg.min_rr:g}",
            )
            return None
        target_2 = targets[1] if len(targets) > 1 else None
        take_profit_2 = self._target_price(target_2, direction, spread_price) if target_2 else None
        reward_2 = (take_profit_2 - entry) * sign / risk if take_profit_2 is not None else None

        at_open, at_close = self._sessions.status(bar_open), self._sessions.status(now)
        session, allowed = at_open.session, at_open.allowed
        if not allowed and not at_open.blocked and not at_close.blocked:
            if any(start <= bar_open < end for start, end in self._news_windows):
                session, allowed = POST_NEWS, True
        if not allowed or at_close.blocked:
            reasons = at_close.reasons if at_close.blocked else at_open.reasons
            self._reject(now, pool, direction, "; ".join(reasons))
            return None

        levels = self._levels
        previous_mid = levels.previous_mid if levels is not None else None
        bias = compute_bias(
            hourly.tail(240),
            price=price,
            previous_mid=previous_mid,
            news_bias=news_bias,
            config=cfg.bias,
            swing_lookback=cfg.swing_lookback,
        )
        self._bias = bias
        if not bias.bias.allows(direction):
            self._reject(
                now, pool, direction, f"against the higher-timeframe bias ({bias.bias.value})"
            )
            return None
        if self._model.pools == "internal" and bias.bias is Bias.NEUTRAL:
            self._reject(now, pool, direction, "a continuation needs an H1 trend")
            return None

        crt = active_crt(self._crt_setups, window, now, max_age_candles=cfg.crt.max_age_candles)
        raided_crt_range = bool(_CRT_KINDS.intersection(pool.sources))
        crt_agrees = cfg.crt.enabled and (
            raided_crt_range or (crt is not None and crt.direction is direction)
        )
        components = self._score(
            sweep=sweep,
            mss=mss,
            zone=zone,
            entry=entry,
            leg_end=leg_end,
            window=window,
            bias=bias,
            session=session,
            spread_points=spread_points,
            median_spread=median_spread,
            previous_mid=previous_mid,
            crt_agrees=crt_agrees,
        )
        extra = cfg.bias.neutral_extra_score if bias.bias is Bias.NEUTRAL else 0.0
        notes = [f"swept {pool.label} at {pool.price:g}"]
        if raided_crt_range:
            notes.append("the swept level is a CRT range extreme")
        if crt is not None and crt.direction is direction:
            notes.append(crt.describe())
        if bias.bias is Bias.NEUTRAL:
            notes.append(f"bias is NEUTRAL: thresholds raised by {extra:g}")
        tags = self._tags(window, atr, sweep, zone, now, bar_open, components)
        return Signal(
            id="",
            symbol=self._symbol,
            time=now,
            direction=direction,
            session=session,
            entry_mode=cfg.entry_mode,
            entry=entry,
            stop_loss=stop,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
            risk_reward_1=reward_1,
            risk_reward_2=reward_2,
            score=round(sum(components.values()), 1),
            components=components,
            alert_threshold=cfg.alert_threshold + extra,
            auto_trade_threshold=min(100.0, cfg.auto_trade_threshold + extra),
            sweep=sweep,
            mss=mss,
            zone=zone,
            target_1=targets[0],
            target_2=target_2,
            bias=bias,
            atr=float(atr[last]),
            spread=spread_price,
            expires_at=now + self._tf.trigger.duration * cfg.entry_expiry_bars,
            notes=tuple(notes),
            model=self._model.name,
            tags=tags,
        )

    def _tags(
        self,
        window: pd.DataFrame,
        atr: np.ndarray,
        sweep: SweepEvent,
        zone: Zone | None,
        now: dt.datetime,
        bar_open: dt.datetime,
        components: Mapping[str, float],
    ) -> tuple[str, ...]:
        """Confluences of this setup (D85). Recorded and reported, never scored."""
        direction, sign = sweep.direction, sweep.direction.sign
        last = len(window) - 1
        earliest = max(1, sweep.pierce_index - self._cfg.mss_structure_lookback)
        tags: list[str] = []
        if zone is not None and zone.kind is ZoneKind.FVG:
            breaker = find_breaker(
                window, direction, extreme_index=sweep.extreme_index, earliest=earliest, upto=last
            )
            if breaker is not None and overlaps(zone, breaker):
                tags.append("unicorn")
            against = find_fvgs(
                window,
                atr,
                direction.opposite,
                first=earliest,
                last=sweep.extreme_index,
                min_size_atr=self._cfg.fvg_min_atr,
            )
            if any(overlaps(zone, gap) for gap in against):
                tags.append("bpr")
        midnight_open = ny_midnight_open(window, now)
        if midnight_open is not None and (midnight_open - sweep.extreme) * sign > 0:
            tags.append("judas")  # the raid ran beyond the New York midnight open
        if in_silver_bullet(bar_open):
            tags.append("silver_bullet")
        if components.get("fib", 0.0) > 0:
            tags.append("ote")
        return tuple(tags)

    def _pick_zone(
        self, window: pd.DataFrame, atr: np.ndarray, sweep: SweepEvent, mss: MssEvent, price: float
    ) -> Zone | None:
        """The entry zone, by the configured priority (spec 5.5)."""
        cfg = self._cfg
        direction, sign = sweep.direction, sweep.direction.sign
        # A zone is usable while price has not yet come back to its entry; ote and
        # market entries judge that by the gap's middle.
        mode = cfg.entry_mode if cfg.entry_mode not in ("market", "ote") else "fvg_mid"
        last = len(window) - 1

        def usable(zone: Zone) -> bool:
            level = zone.price_for(mode)
            if (price - level) * sign <= 0:
                return False  # the entry must be a pullback from where price is now
            return first_touch(window, zone, level, start=zone.known_index + 1) is None

        for choice in cfg.entry_zone_priority:
            if choice == "fvg":
                gaps = [
                    zone
                    for zone in find_fvgs(
                        window,
                        atr,
                        direction,
                        first=max(1, sweep.extreme_index),
                        last=last - 1,
                        rule=self._rule,
                        min_size_atr=cfg.fvg_min_atr,
                    )
                    if usable(zone)
                ]
                if gaps:
                    return gaps[-1]  # the most recent: left by the latest displacement
            elif choice == "ifvg":
                inversions = [
                    zone
                    for zone in find_inversions(
                        window,
                        atr,
                        direction,
                        first=max(1, sweep.pierce_index - cfg.mss_structure_lookback),
                        last=sweep.extreme_index,
                        after=sweep.extreme_index,
                        min_size_atr=cfg.fvg_min_atr,
                    )
                    if usable(zone)
                ]
                if inversions:
                    return max(inversions, key=lambda zone: zone.price_for(mode) * sign)
            else:
                block = find_order_block(
                    window,
                    direction,
                    displacement_index=mss.displacement_index,
                    earliest=sweep.extreme_index - _ORDER_BLOCK_LOOKBACK,
                )
                if block is not None and usable(block):
                    return block
        return None

    def _targets(self, direction: Direction, entry: float) -> list[Pool]:
        """Intact external pools on the far side of ``entry``, nearest first."""
        side = Side.BUY_SIDE if direction is Direction.LONG else Side.SELL_SIDE
        candidates = [
            track.pool
            for track in self._tracks.values()
            if track.stage is _Stage.RESTING
            and track.pool.side is side
            and track.pool.is_external
            and (track.pool.price - entry) * direction.sign > _EPSILON
        ]
        return sorted(candidates, key=lambda pool: abs(pool.price - entry))

    @staticmethod
    def _target_price(pool: Pool, direction: Direction, spread_price: float) -> float:
        """Take-profit price for a pool.

        A long is closed on the bid, which is what the chart shows, so the level
        itself is used. A short is closed on the ask, one spread above the chart,
        so the target is lifted by the spread to be reached when the chart is.
        """
        return pool.price if direction is Direction.LONG else pool.price + spread_price

    # -- scoring ----------------------------------------------------------- #

    def _score(
        self,
        *,
        sweep: SweepEvent,
        mss: MssEvent,
        zone: Zone | None,
        entry: float,
        leg_end: float,
        window: pd.DataFrame,
        bias: BiasResult,
        session: str | None,
        spread_points: float,
        median_spread: float | None,
        previous_mid: float | None,
        crt_agrees: bool,
    ) -> dict[str, float]:
        """Points per component (spec 5.7). They add up to at most 100."""
        cfg, weights = self._cfg, self._cfg.scoring
        direction, sign = sweep.direction, sweep.direction.sign

        # Sweep quality: how important the pool is, how fast price came back, and
        # how firmly the sweep candle was rejected.
        speed = 1.0 - sweep.bars_to_reclaim / (cfg.max_bars_back_inside + 1)
        bar = window.iloc[sweep.extreme_index]
        bar_range = float(bar["high"] - bar["low"])
        if bar_range > 0:
            wick = (
                (bar["close"] - bar["low"])
                if direction is Direction.LONG
                else (bar["high"] - bar["close"])
            )
            rejection = float(wick) / bar_range
        else:
            rejection = 0.0
        sweep_quality = 0.6 * sweep.pool.strength + 0.2 * speed + 0.2 * rejection

        displacement = _clip((mss.displacement_atr - 0.7) / (2.2 - 0.7))
        leg_size = (leg_end - sweep.extreme) * sign
        retracement = (leg_end - entry) * sign / leg_size if leg_size > 0 else -1.0
        in_fib_zone = cfg.fib_low - _EPSILON <= retracement <= cfg.fib_high + _EPSILON

        if bias.bias is Bias.NEUTRAL:
            bias_share = 0.5
        else:
            bias_share = 1.0  # a setup against the bias never gets this far
        news_share = _clip(0.5 + 0.5 * sign * bias.news)

        if session in (LONDON, NEW_YORK, POST_NEWS):
            session_share = 1.0
        elif session is not None:
            session_share = _SECONDARY_SESSION_SHARE
        else:
            session_share = 0.0

        usual = median_spread
        if usual is None:
            recent = window["spread"].to_numpy(dtype="float64")
            usual = float(np.median(recent)) if len(recent) else 0.0
        if usual > 0:
            spread_share = _clip((2.0 - spread_points / usual) / (2.0 - 1.25))
        else:
            spread_share = 0.5  # nothing to compare against

        if previous_mid is None:
            location_share = 0.0
        else:
            location_share = 1.0 if (previous_mid - entry) * sign > 0 else 0.0

        return {
            "sweep_quality": round(weights.sweep_quality * sweep_quality, 2),
            "displacement": round(weights.displacement * displacement, 2),
            "entry_zone": round(weights.entry_zone * (_ZONE_SHARE[zone.kind] if zone else 0.0), 2),
            "fib": round(weights.fib * (1.0 if in_fib_zone else 0.0), 2),
            "htf_bias": round(weights.htf_bias * bias_share, 2),
            "news_bias": round(weights.news_bias * news_share, 2),
            "kill_zone": round(weights.kill_zone * session_share, 2),
            "spread": round(weights.spread * spread_share, 2),
            "premium_discount": round(weights.premium_discount * location_share, 2),
            "crt": round(weights.crt * (1.0 if crt_agrees else 0.0), 2),
        }

    # -- helpers ----------------------------------------------------------- #

    def _one_per_raid(self, found: list[tuple[PoolKey, Signal]]) -> list[Signal]:
        """Keep the best signal per direction and retire the raids it supersedes.

        Several pools are often raided in one move (say the Asian low and the
        previous day's low a little below it). That is one opportunity, not two.
        """
        best: dict[Direction, Signal] = {}
        for _key, signal in found:
            current = best.get(signal.direction)
            if current is None or signal.score > current.score:
                best[signal.direction] = signal
        for direction in best:
            for key, track in list(self._tracks.items()):
                in_progress = track.stage in (_Stage.TRACKING, _Stage.AWAIT_ZONE, _Stage.PIERCED)
                if in_progress and track.pool.side.trade_direction is direction:
                    del self._tracks[key]
        for signal in best.values():
            log.info(
                "signal",
                direction=signal.direction.value,
                score=signal.score,
                entry=signal.entry,
                stop_loss=signal.stop_loss,
                take_profit_1=signal.take_profit_1,
                risk_reward=round(signal.risk_reward_1, 2),
                swept=signal.pool.label,
                session=signal.session,
            )
        return list(best.values())

    def _with_id(self, signal: Signal) -> Signal:
        code = _SESSION_CODES.get(signal.session or "", "X")
        if self._model.code:
            code = f"{code}-{self._model.code}"  # e.g. 2026-11-17-LDN-CISD-01
        day = f"{signal.time:%Y-%m-%d}"
        number = self._counters.get((day, code), 0) + 1
        self._counters[(day, code)] = number
        return replace(signal, id=f"{day}-{code}-{number:02d}")

    def _reject(self, now: dt.datetime, pool: Pool, direction: Direction, reason: str) -> None:
        self._rejections.append(Rejection(now, pool, direction, reason))
        log.debug("setup rejected", pool=pool.label, direction=direction.value, reason=reason)

    @staticmethod
    def _position(window: pd.DataFrame, moment: dt.datetime | None) -> int | None:
        if moment is None:
            return None
        stamp = pd.Timestamp(moment)
        position = int(window.index.searchsorted(stamp))
        found = position < len(window) and window.index[position] == stamp
        return position if found else None

    @staticmethod
    def _extended(event: SweepEvent, high: np.ndarray, low: np.ndarray, upto: int) -> SweepEvent:
        """The sweep with its extreme updated for bars up to ``upto``.

        Price sometimes dips once more after closing back inside. The stop has
        to sit beyond the true extreme of the whole raid.
        """
        if event.direction is Direction.LONG:
            leg = low[event.pierce_index : upto + 1]
            offset = int(np.argmin(leg))
            moved = leg[offset] < event.extreme
        else:
            leg = high[event.pierce_index : upto + 1]
            offset = int(np.argmax(leg))
            moved = leg[offset] > event.extreme
        if not moved:
            return event
        extreme = float(leg[offset])
        return replace(
            event,
            extreme=extreme,
            extreme_index=event.pierce_index + offset,
            depth=abs(extreme - event.pool.price),
        )


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))

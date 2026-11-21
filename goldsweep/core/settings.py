"""Application settings.

Two sources, deliberately kept apart:

* ``config.yaml``: strategy, session and risk parameters (:class:`AppConfig`).
  Validation is strict. Unknown keys, duplicated keys and out-of-range values
  stop the program at startup instead of silently falling back to a default.
* ``.env`` and the process environment: secrets and machine-specific values
  (:class:`EnvSettings`). Secrets are never read from ``config.yaml``.

:func:`load_settings` combines both into one immutable :class:`Settings`.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from yaml.constructor import ConstructorError

from goldsweep.core.server_time import AUTO, ServerTimeRule
from goldsweep.core.types import Mode, Timeframe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

#: Spec section 6: the risk per trade can never be configured above this.
MAX_RISK_PER_TRADE_PCT = 2.0


class ConfigError(Exception):
    """config.yaml or the environment is missing or invalid."""


# --------------------------------------------------------------------------- #
# Field types
# --------------------------------------------------------------------------- #


def _check_zone(name: str) -> str:
    try:
        ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError or OSError for odd keys
        raise ValueError(f"unknown IANA time zone {name!r}") from exc
    return name


ZoneName = Annotated[str, AfterValidator(_check_zone)]

_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_clock(value: Any) -> dt.time:
    """Accept only "HH:MM" text (or a ready-made ``time``).

    An unquoted ``15:00`` in YAML arrives here as the integer 900. It must be
    rejected, not reinterpreted as a time of day.
    """
    if isinstance(value, dt.time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        match = _CLOCK_RE.match(value.strip())
        if match is not None:
            return dt.time(int(match.group(1)), int(match.group(2)))
    raise ValueError(f'clock times must be quoted 24-hour "HH:MM" strings, got {value!r}')


ClockTime = Annotated[dt.time, BeforeValidator(_parse_clock)]

#: Where a limit entry sits in its zone. fvg_near / fvg_far are the edge price
#: reaches first / last (top / bottom for a long); ote is the middle of the
#: fib_low..fib_high retracement of the move after the sweep, kept inside the zone.
EntryMode = Literal["fvg_top", "fvg_mid", "fvg_bottom", "fvg_near", "fvg_far", "ote", "market"]


def _upper_if_text(value: Any) -> Any:
    return value.strip().upper() if isinstance(value, str) else value


# --------------------------------------------------------------------------- #
# config.yaml
# --------------------------------------------------------------------------- #


class _StrictModel(BaseModel):
    """Base for config sections: unknown keys are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(_StrictModel):
    """A wall-clock window in one IANA time zone.

    ``end`` earlier than ``start`` means the window crosses local midnight.
    """

    tz: ZoneName
    start: ClockTime
    end: ClockTime

    @model_validator(mode="after")
    def _start_differs_from_end(self) -> Self:
        if self.start == self.end:
            raise ValueError("start and end must differ")
        return self


class SessionWindow(TimeWindow):
    """A session window plus whether new trades may open inside it."""

    trade: bool = True


class OverlapConfig(_StrictModel):
    """London-New York overlap: the hours when both full sessions are open."""

    trade: bool = True
    london: TimeWindow = TimeWindow(tz="Europe/London", start=dt.time(8, 0), end=dt.time(16, 30))
    newyork: TimeWindow = TimeWindow(tz="America/New_York", start=dt.time(8, 0), end=dt.time(17, 0))


class CutoffConfig(_StrictModel):
    """No new trades on Friday from this local time on."""

    tz: ZoneName = "America/New_York"
    time: ClockTime = dt.time(12, 0)


class WeekendConfig(_StrictModel):
    """Weekly market close (Friday) and reopen (Sunday), in one local time zone."""

    tz: ZoneName = "America/New_York"
    close: ClockTime = dt.time(17, 0)
    open: ClockTime = dt.time(18, 0)


class SessionsConfig(_StrictModel):
    """Spec section 4. Every window is defined in local time, never as a UTC offset."""

    asia: SessionWindow = SessionWindow(
        tz="Asia/Tokyo", start=dt.time(9, 0), end=dt.time(15, 0), trade=False
    )
    london: SessionWindow = SessionWindow(
        tz="Europe/London", start=dt.time(7, 0), end=dt.time(10, 0), trade=True
    )
    newyork: SessionWindow = SessionWindow(
        tz="America/New_York", start=dt.time(8, 0), end=dt.time(11, 0), trade=True
    )
    overlap: OverlapConfig = OverlapConfig()
    rollover_block: TimeWindow = TimeWindow(
        tz="America/New_York", start=dt.time(16, 45), end=dt.time(18, 15)
    )
    friday_cutoff: CutoffConfig = CutoffConfig()
    weekend: WeekendConfig = WeekendConfig()
    #: False (spec 4): new trades only inside the trading windows above. True: at any
    #: hour the market is open; the rollover block, Friday cutoff, weekend and news
    #: blackouts still apply, and setups outside the kill zones score lower.
    trade_all_hours: bool = False


class TimeframesConfig(_StrictModel):
    """Spec section 5: H1 for bias, M15 for liquidity, M5 (or M1) for the trigger."""

    bias: Timeframe = Timeframe.H1
    liquidity: Timeframe = Timeframe.M15
    trigger: Timeframe = Timeframe.M5

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.trigger.minutes <= self.liquidity.minutes <= self.bias.minutes:
            raise ValueError("timeframes must satisfy trigger <= liquidity <= bias")
        return self


ZoneChoice = Literal["fvg", "ifvg", "order_block"]
AtrMethod = Literal["sma", "wilder"]


class DayBoundaryConfig(_StrictModel):
    """Where one trading day ends and the next begins: gold's daily close."""

    tz: ZoneName = "America/New_York"
    time: ClockTime = dt.time(17, 0)


class LiquidityConfig(_StrictModel):
    """How liquidity pools are found (spec 5.2 plus the liquidity guide)."""

    #: Unswept M15 swing highs and lows count as pools ("previous swing highs/lows").
    swing_points: bool = True
    #: Bars on each side that make a swing point a major, external one.
    major_swing_lookback: int = Field(default=10, ge=2, le=200)
    #: Rising swing lows / falling swing highs on one line count as trendline liquidity.
    trendlines: bool = True
    trendline_min_touches: int = Field(default=3, ge=3, le=10)
    round_number_step: float = Field(default=10.0, gt=0)
    round_number_major_step: float = Field(default=50.0, gt=0)
    #: Pools weaker than this are tracked but their sweeps do not start a setup.
    min_sweep_pool_strength: float = Field(default=0.5, ge=0, le=1)
    max_pool_age_hours: int = Field(default=48, ge=1, le=720)
    day_boundary: DayBoundaryConfig = DayBoundaryConfig()

    @model_validator(mode="after")
    def _major_step_is_a_multiple(self) -> Self:
        ratio = self.round_number_major_step / self.round_number_step
        if ratio < 1 or abs(ratio - round(ratio)) > 1e-9:
            raise ValueError("round_number_major_step must be a multiple of round_number_step")
        return self


class BiasConfig(_StrictModel):
    """Spec 5.1: how H1 structure, premium/discount and news combine into a bias."""

    structure_weight: float = Field(default=0.5, ge=0, le=1)
    location_weight: float = Field(default=0.2, ge=0, le=1)
    news_weight: float = Field(default=0.3, ge=0, le=1)
    #: |score| at or above this gives LONG / SHORT; below it the bias is NEUTRAL.
    threshold: float = Field(default=0.25, gt=0, le=1)
    #: Extra points a signal needs while the bias is NEUTRAL.
    neutral_extra_score: float = Field(default=5.0, ge=0, le=50)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Self:
        total = self.structure_weight + self.location_weight + self.news_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"bias weights must add up to 1.0, got {total:g}")
        return self


class CrtConfig(_StrictModel):
    """Candle Range Theory: a higher-timeframe candle's high and low as a range."""

    enabled: bool = True
    timeframe: Timeframe = Timeframe.H4
    #: How many later candles a confirmed CRT keeps giving a directional lean.
    max_age_candles: int = Field(default=2, ge=1, le=10)

    @field_validator("timeframe")
    @classmethod
    def _supported_timeframe(cls, value: Timeframe) -> Timeframe:
        if value not in (Timeframe.H1, Timeframe.H4):
            raise ValueError("crt.timeframe must be H1 or H4")
        return value


class ScoringConfig(_StrictModel):
    """Spec 5.7: points per component. They must add up to 100."""

    sweep_quality: float = Field(default=20, ge=0, le=100)
    displacement: float = Field(default=15, ge=0, le=100)
    entry_zone: float = Field(default=10, ge=0, le=100)
    fib: float = Field(default=5, ge=0, le=100)
    htf_bias: float = Field(default=15, ge=0, le=100)
    news_bias: float = Field(default=10, ge=0, le=100)
    kill_zone: float = Field(default=10, ge=0, le=100)
    spread: float = Field(default=5, ge=0, le=100)
    premium_discount: float = Field(default=5, ge=0, le=100)
    crt: float = Field(default=5, ge=0, le=100)

    @model_validator(mode="after")
    def _weights_sum_to_100(self) -> Self:
        total = sum(self.model_dump().values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"scoring weights must add up to 100, got {total:g}")
        return self


class RefinementConfig(_StrictModel):
    """Phase 9 stage 2 (D83): "sniper" entries refined on one-minute bars.

    With ``mode: m1`` an M5 setup is not traded at once: the bot waits for price
    to come back into the setup's gap and turn there on M1, then enters with a
    stop beyond that turn. The M5 targets stay, so the R:R grows.
    """

    mode: Literal["none", "m1"] = "none"
    #: The M1 turn: a close through the last M1 swing high before the pullback's
    #: low (choch), or through the open of the down-closing run into it (cisd).
    confirm: Literal["choch", "cisd"] = "choch"
    #: market: at the close of the turning bar. fvg_mid: a limit at the middle of
    #: the M1 gap the turn left (market when it left none).
    entry: Literal["market", "fvg_mid"] = "market"
    #: M1 bars each side that make a swing point.
    swing_lookback: int = Field(default=2, ge=1, le=10)
    #: Stop beyond the pullback's extreme by this many M1 ATRs (plus the spread).
    sl_buffer_atr: float = Field(default=0.2, ge=0, le=5)
    #: The stop is never closer than this many spreads, nor than ``min_stop_atr``
    #: M1 ATRs, so the spread and slippage cannot take most of the risk.
    min_stop_spreads: float = Field(default=4.0, ge=0, le=100)
    min_stop_atr: float = Field(default=1.0, ge=0, le=20)
    #: How many M1 bars a fvg_mid limit waits after the turn.
    entry_wait_bars: int = Field(default=10, ge=1, le=240)


class StrategyConfig(_StrictModel):
    """Spec section 5 thresholds, used by the analysis engine."""

    atr_period: int = Field(default=14, ge=2, le=500)
    #: "sma" matches the ATR that MetaTrader 5 draws; "wilder" is the classic smoothing.
    atr_method: AtrMethod = "sma"
    swing_lookback: int = Field(default=3, ge=1, le=50)
    eq_tolerance_atr: float = Field(default=0.15, gt=0, le=5)
    min_sweep_atr: float = Field(default=0.10, ge=0, le=5)
    max_bars_back_inside: int = Field(default=3, ge=1, le=50)
    mss_window: int = Field(default=12, ge=1, le=500)
    #: How far back the "last lower high / higher low" may lie, in trigger bars.
    mss_structure_lookback: int = Field(default=30, ge=3, le=500)
    displacement_atr: float = Field(default=1.2, gt=0, le=20)
    #: Second way to qualify: a body of at least this share of the candle's range ...
    displacement_body_ratio: float = Field(default=0.70, gt=0, le=1)
    #: ... on a candle whose range is at least this many ATRs (a tiny full-bodied
    #: candle is not displacement).
    displacement_min_range_atr: float = Field(default=1.0, ge=0, le=20)
    #: Smallest fair value gap worth trading, as a share of ATR.
    fvg_min_atr: float = Field(default=0.05, ge=0, le=5)
    entry_mode: EntryMode = "fvg_mid"
    entry_zone_priority: tuple[ZoneChoice, ...] = ("fvg", "ifvg", "order_block")
    fib_low: float = Field(default=0.62, gt=0, lt=1)
    fib_high: float = Field(default=0.79, gt=0, lt=1)
    sl_buffer_atr: float = Field(default=0.10, ge=0, le=5)
    min_rr: float = Field(default=2.0, gt=0, le=20)
    entry_expiry_bars: int = Field(default=6, ge=1, le=500)
    alert_threshold: float = Field(default=60, ge=0, le=100)
    auto_trade_threshold: float = Field(default=75, ge=0, le=100)
    liquidity: LiquidityConfig = LiquidityConfig()
    bias: BiasConfig = BiasConfig()
    crt: CrtConfig = CrtConfig()
    scoring: ScoringConfig = ScoringConfig()
    refinement: RefinementConfig = RefinementConfig()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.auto_trade_threshold < self.alert_threshold:
            raise ValueError("auto_trade_threshold must not be below alert_threshold")
        if self.fib_low >= self.fib_high:
            raise ValueError("fib_low must be below fib_high")
        if not self.entry_zone_priority:
            raise ValueError("entry_zone_priority needs at least one zone type")
        if len(set(self.entry_zone_priority)) != len(self.entry_zone_priority):
            raise ValueError("entry_zone_priority contains duplicates")
        return self


CalendarSourceName = Literal["financecalendar", "forexfactory"]
LlmProviderName = Literal["anthropic", "groq", "openai_compatible"]


class CalendarSourceConfig(_StrictModel):
    """One economic-calendar source. Sources are tried in the order listed."""

    name: CalendarSourceName
    enabled: bool = True
    refresh_min: int = Field(default=15, ge=1, le=1440)
    #: Refresh this often within ``news.fast_window_min`` of a high-impact release.
    #: Only useful for a source that publishes actual figures; unset = never faster.
    fast_refresh_min: int | None = Field(default=None, ge=1, le=60)
    #: Stop for the day after this many requests (free plans are tight); unset = no cap.
    daily_request_limit: int | None = Field(default=None, ge=1, le=1_000_000)


class RssFeedConfig(_StrictModel):
    name: str = Field(min_length=1, max_length=60)
    url: str

    @field_validator("url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("feed urls must start with https://")
        return value


_DEFAULT_CALENDAR_SOURCES = (
    CalendarSourceConfig(name="financecalendar", refresh_min=180, daily_request_limit=10),
    CalendarSourceConfig(name="forexfactory", refresh_min=60),
)
_DEFAULT_FEEDS = (
    RssFeedConfig(name="Federal Reserve", url="https://www.federalreserve.gov/feeds/press_all.xml"),
    RssFeedConfig(name="ECB", url="https://www.ecb.europa.eu/rss/press.html"),
    RssFeedConfig(name="FXStreet", url="https://www.fxstreet.com/rss/news"),
)
_DEFAULT_BLACKOUT_KEYWORDS = (
    "CPI",
    "Non-Farm",
    "NFP",
    "FOMC Statement",
    "FOMC Meeting Minutes",
    "FOMC Press Conference",
    "FOMC Economic Projections",
    "Federal Funds Rate",
    "Fed Chair",
    "PCE",
    "GDP",
    "ISM",
    "Retail Sales",
    "Unemployment Claims",
    "Jobless Claims",
)


class NewsConfig(_StrictModel):
    """Spec section 5.6: calendar, headlines, the AI classifier and scenarios."""

    blackout_before_min: int = Field(default=30, ge=0, le=1440)
    blackout_after_min: int = Field(default=15, ge=0, le=1440)
    #: Events in these currencies that are high impact, or match a keyword, start a blackout.
    blackout_currencies: tuple[str, ...] = ("USD",)
    blackout_keywords: tuple[str, ...] = _DEFAULT_BLACKOUT_KEYWORDS
    post_news_sweep_mode: bool = True
    post_news_start_min: int = Field(default=15, ge=0, le=1440)
    post_news_end_min: int = Field(default=90, ge=1, le=1440)
    #: Spec 5.6: as a release's blackout starts, move the stop of a trade that is
    #: at least this many R in profit to breakeven (0 turns it off).
    breakeven_before_news_r: float = Field(default=1.0, ge=0, le=20)
    headline_half_life_min: int = Field(default=120, gt=0, le=10080)
    llm_enabled: bool = True
    #: The classifier falls back to keyword rules once today's estimated spend reaches this.
    llm_daily_budget_usd: float = Field(default=0.50, ge=0, le=1000)
    #: Model prices, US$ per million tokens. Unset: the provider's default
    #: (Claude Haiku 5.5: 0.10 / 0.50; Groq free plan: 0).
    llm_input_usd_per_mtok: float | None = Field(default=None, ge=0, le=1000)
    llm_output_usd_per_mtok: float | None = Field(default=None, ge=0, le=1000)
    #: Call caps that keep a free plan inside its limits. Unset: the provider's
    #: default (Groq: 6 a minute, 180 a day; Claude: none).
    llm_max_calls_per_minute: int | None = Field(default=None, ge=1, le=10_000)
    llm_max_calls_per_day: int | None = Field(default=None, ge=1, le=1_000_000)
    #: Headlines the keyword rules find unrelated to gold skip the model.
    llm_prefilter: bool = True
    calendar_sources: tuple[CalendarSourceConfig, ...] = _DEFAULT_CALENDAR_SOURCES
    fast_window_min: int = Field(default=10, ge=1, le=120)
    finnhub_categories: tuple[str, ...] = ("forex", "general")
    rss_feeds: tuple[RssFeedConfig, ...] = _DEFAULT_FEEDS
    headline_poll_min: int = Field(default=5, ge=1, le=1440)
    #: Headlines older than this when first seen are stored but not classified.
    headline_max_age_hours: int = Field(default=12, ge=1, le=168)
    scenario_before_session_min: int = Field(default=30, ge=1, le=600)
    scenario_before_event_min: int = Field(default=60, ge=1, le=600)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.post_news_end_min <= self.post_news_start_min:
            raise ValueError("post_news_end_min must be after post_news_start_min")
        names = [source.name for source in self.calendar_sources]
        if len(set(names)) != len(names):
            raise ValueError("calendar_sources lists a source twice")
        return self


class RiskConfig(_StrictModel):
    """Spec section 6. The risk manager (Phase 5) enforces these again at trade time."""

    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=MAX_RISK_PER_TRADE_PCT)
    max_open_positions: int = Field(default=1, ge=1, le=10)
    max_trades_per_day: int = Field(default=3, ge=1, le=50)
    daily_loss_limit_pct: float = Field(default=2.0, gt=0, le=20)
    weekly_loss_limit_pct: float = Field(default=5.0, gt=0, le=50)
    max_consecutive_losses: int = Field(default=3, ge=1, le=20)
    #: The stop moves to the entry once price has gone this many R in favour
    #: (0: never by distance). Reaching TP1 always moves it too (spec 7b).
    breakeven_at_r: float = Field(default=1.0, ge=0, le=10)
    #: Spec 7b split: this share closes at TP1, the next at TP2 (at most what TP1
    #: leaves), the rest at TP3.
    partial_close_at_tp1_pct: float = Field(default=50, ge=0, le=100)
    partial_close_at_tp2_pct: float = Field(default=30, ge=0, le=100)
    #: TP3 sits this many R from the entry (0: no TP3; its share goes to TP2).
    tp3_r: float = Field(default=3.0, ge=0, le=20)
    #: pools: TP1 / TP2 at the next liquidity pools, TP3 at tp3_r (the spec).
    #: fixed_r: the targets sit at fixed_targets_r R from the entry instead (D84).
    target_mode: Literal["pools", "fixed_r"] = "pools"
    #: One to three R multiples, nearest first: one value closes everything there;
    #: more values split the position as partial_close_at_tp1_pct / _tp2_pct say.
    fixed_targets_r: tuple[float, ...] = (3.0,)
    max_trades_per_session: int = Field(default=2, ge=1, le=50)
    #: Spread filter: no new trade while the spread is above this multiple of the
    #: usual spread, measured as the median over ``spread_median_days`` days.
    max_spread_multiple: float = Field(default=2.0, gt=1, le=20)
    spread_median_days: int = Field(default=20, ge=1, le=365)

    @field_validator("fixed_targets_r")
    @classmethod
    def _increasing_targets(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not 1 <= len(value) <= 3:
            raise ValueError("fixed_targets_r needs one to three values")
        if any(k <= 0 or k > 20 for k in value):
            raise ValueError("fixed_targets_r values must be above 0 and at most 20")
        if any(b <= a for a, b in zip(value, value[1:], strict=False)):
            raise ValueError("fixed_targets_r must rise from the nearest target to the farthest")
        return value


class PressureWeights(_StrictModel):
    """How the buyer/seller pressure meter combines its four readings. They add up to 1."""

    volume: float = Field(default=0.40, ge=0, le=1)
    candles: float = Field(default=0.25, ge=0, le=1)
    structure: float = Field(default=0.15, ge=0, le=1)
    news: float = Field(default=0.20, ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> Self:
        total = self.volume + self.candles + self.structure + self.news
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"pressure weights must add up to 1.0, got {total:g}")
        return self


class PressureConfig(_StrictModel):
    """The buyer/seller pressure meter: an estimate from bars and news (analysis/pressure.py)."""

    timeframe: Timeframe = Timeframe.M15
    window: int = Field(default=24, ge=5, le=500)
    #: |score| below this reads "balanced"; at or above ``strong_level`` it reads "strong".
    balanced_band: float = Field(default=0.10, ge=0, lt=1)
    strong_level: float = Field(default=0.40, gt=0, le=1)
    weights: PressureWeights = PressureWeights()

    @model_validator(mode="after")
    def _bands_in_order(self) -> Self:
        if self.strong_level <= self.balanced_band:
            raise ValueError("pressure.strong_level must be above pressure.balanced_band")
        return self


class CostConfig(_StrictModel):
    """Trading costs the backtester charges."""

    #: "bar": the spread stored with each bar; "fixed": ``fixed_spread_points`` always.
    spread_source: Literal["bar", "fixed"] = "bar"
    fixed_spread_points: float = Field(default=25.0, ge=0, le=10_000)
    #: Added to every spread: bar spreads are often the calmest moment of the bar.
    extra_spread_points: float = Field(default=0.0, ge=0, le=10_000)
    #: Adverse slippage on market entries and on stop-loss exits.
    slippage_points: float = Field(default=10.0, ge=0, le=10_000)
    #: Round-trip commission per 1.00 lot, in account currency.
    commission_per_lot: float = Field(default=7.0, ge=0, le=1_000)


class WalkForwardGrid(_StrictModel):
    """Values tried for each parameter when optimising a training window."""

    auto_trade_threshold: tuple[float, ...] = (65.0, 75.0, 85.0)
    min_rr: tuple[float, ...] = (2.0, 2.5, 3.0)
    #: How many trigger bars an unfilled entry waits (Phase 9, D82).
    entry_expiry_bars: tuple[int, ...] = (6, 12, 24)

    @field_validator("entry_expiry_bars")
    @classmethod
    def _positive_bars(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(bars < 1 for bars in value):
            raise ValueError("entry_expiry_bars values must be at least 1")
        return value

    @field_validator("auto_trade_threshold", "min_rr", "entry_expiry_bars")
    @classmethod
    def _non_empty_unique(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value:
            raise ValueError("each grid needs at least one value")
        if len(set(value)) != len(value):
            raise ValueError("grid values contain duplicates")
        return tuple(sorted(value))


class WalkForwardConfig(_StrictModel):
    """Spec 9.3: optimise on rolling 6 months, test on the next 2."""

    train_months: int = Field(default=6, ge=1, le=60)
    test_months: int = Field(default=2, ge=1, le=24)
    #: A parameter set needs at least this many training trades to be chosen.
    min_train_trades: int = Field(default=20, ge=1, le=10_000)
    grid: WalkForwardGrid = WalkForwardGrid()


class BacktestConfig(_StrictModel):
    """The backtester (spec 9.3)."""

    initial_balance: float = Field(default=10_000.0, gt=0)
    #: History replayed before the first day, so pools, levels and ATR exist on day one.
    warmup_days: int = Field(default=10, ge=3, le=120)
    #: Which signals are traded: those that clear the auto-trade threshold (as the live
    #: bot would), or every signal that clears the alert threshold.
    trade_threshold: Literal["auto_trade", "alert"] = "auto_trade"
    #: After TP1, trail the stop of the rest behind confirmed M5 swing points (spec 6).
    trail_after_tp1: bool = True
    #: Settle fills and exits on one-minute bars where they are stored.
    use_m1_for_fills: bool = True
    costs: CostConfig = CostConfig()
    walk_forward: WalkForwardConfig = WalkForwardConfig()


class BrokerConfig(_StrictModel):
    """Broker-specific settings that are not secrets."""

    #: ``auto``, ``UTC``, a fixed offset, an IANA zone or ``NY+7``.
    server_timezone: str = AUTO

    @field_validator("server_timezone", mode="before")
    @classmethod
    def _valid_server_timezone(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(
                'server_timezone must be quoted text such as "auto", "UTC", "+02:00", '
                '"Europe/Athens" or "NY+7"'
            )
        text = value.strip()
        if text.lower() == AUTO:
            return AUTO
        ServerTimeRule.parse(text)  # raises ValueError with a helpful message
        return text


class ExecutionConfig(_StrictModel):
    """The live loop (spec 7 and 7b). In PAPER mode orders are simulated, never sent."""

    #: Take signals that clear the auto-trade threshold (on paper in PAPER mode).
    auto_trade: bool = True
    #: Seconds to wait after each bar close before reading the bar from the broker.
    bar_delay_seconds: int = Field(default=2, ge=0, le=60)
    #: Keep asking this long when the broker has not published the bar yet.
    bar_wait_seconds: int = Field(default=30, ge=0, le=240)
    #: LIVE: every order carries this number, so the bot knows its own trades.
    magic_number: int = Field(default=7_341_926, ge=1, le=2_147_483_647)
    #: LIVE: the largest price difference accepted on a market order, in points.
    deviation_points: int = Field(default=30, ge=0, le=10_000)
    #: LIVE: never more than this many lots open at once (positions and orders).
    max_total_lots: float = Field(default=1.0, gt=0, le=1000)
    #: LIVE: refuse an order that would push the margin level below this (spec 7b).
    min_margin_level_pct: float = Field(default=1000, ge=100, le=1_000_000)
    #: LIVE: Phase 6 trades demo accounts. A real-money account also needs this.
    allow_real_account: bool = False
    #: LIVE: how often orders and positions are compared with the broker (seconds).
    sync_seconds: int = Field(default=5, ge=1, le=60)
    #: Write signals.json for the MT5 chart panel (GoldSweepPanel.mq5).
    panel_enabled: bool = True
    panel_signals: int = Field(default=10, ge=1, le=50)


class AlertsConfig(_StrictModel):
    """Telegram messages (spec section 8)."""

    #: Send to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
    telegram_enabled: bool = True
    #: Also alert on setups the risk rules blocked (with the reason).
    send_blocked: bool = True
    send_scenarios: bool = True
    #: Warn this many minutes before each high-impact release.
    pre_news_warning_min: int = Field(default=15, ge=1, le=240)
    daily_summary_time: ClockTime = dt.time(21, 0)
    daily_summary_tz: ZoneName = "America/New_York"
    #: A quiet "still running" message this often (0 = never).
    heartbeat_minutes: int = Field(default=60, ge=0, le=1440)
    #: The watchdog (python main.py --watchdog) alerts when the bot has not been seen
    #: for this long while the gold market is open.
    watchdog_minutes: int = Field(default=15, ge=5, le=1440)


class HousekeepingConfig(_StrictModel):
    """Daily upkeep (Phase 8): database backups and log clean-up."""

    #: A copy of the database is made every day at this UTC time; this many are kept.
    backup_time_utc: ClockTime = dt.time(0, 15)
    backups_kept: int = Field(default=14, ge=0, le=365)
    #: Log rows older than this are removed from the database (log files rotate daily).
    log_days: int = Field(default=30, ge=1, le=3650)


class ModelConfig(_StrictModel):
    """One setup model (D85): whether it looks for setups, and whether it may trade."""

    enabled: bool = True
    #: False: its setups are sent as alerts only (and left out of backtests).
    trade: bool = False


class ModelsConfig(_StrictModel):
    """Phase 9 stage 3 (D85): the setup models. Each runs as its own signal engine on
    the same bars; their trades share one set of risk limits. A new model alerts first
    and trades only once its own backtest supports it and the owner approves."""

    #: The spec's model: sweep of external liquidity, market-structure shift, gap.
    sweep_mss: ModelConfig = ModelConfig(trade=True)
    #: The same, confirmed by a change in the state of delivery instead of the shift.
    cisd: ModelConfig = ModelConfig()
    #: Sweeps of internal (minor) pools, only in the direction of the H1 trend.
    continuation: ModelConfig = ModelConfig()
    #: Breaks of the session ranges and the previous day that hold (D86).
    breakout: ModelConfig = ModelConfig()


ModelName = Literal["sweep_mss", "cisd", "continuation", "breakout"]
#: Confluence tags (D85) and trend/structure context tags (D86) a rule may ask for.
SignalTag = Literal[
    "smt",
    "unicorn",
    "bpr",
    "judas",
    "silver_bullet",
    "ote",
    "d1_trend",
    "h4_trend",
    "ema_trend",
    "weekly_value",
    "high_vol",
]


class PlaybookRule(_StrictModel):
    """Which setups a session playbook takes (D86): one model, a minimum score
    (raised by the neutral-bias extra, as everywhere), and the tags it needs."""

    model: ModelName = "sweep_mss"
    min_score: float = Field(default=60, ge=0, le=100)
    #: At least one of these tags (none listed: no such condition).
    tags_any: tuple[SignalTag, ...] = ()
    #: At least this many confluence tags (smt, unicorn, bpr, judas, silver_bullet, ote).
    min_tags: int = Field(default=0, ge=0, le=6)
    #: Every one of these tags (e.g. a trend condition such as d1_trend).
    tags_all: tuple[SignalTag, ...] = ()


class SessionPlaybook(_StrictModel):
    """One session's playbook (D86)."""

    #: What auto-trades in this session now (None: nothing).
    trading: PlaybookRule | None = None
    #: The A+ playbook found by the analysis: monitored on a shadow book before it
    #: may trade, and graded A+ in alerts (None: no candidate).
    candidate: PlaybookRule | None = None


class PlaybooksConfig(_StrictModel):
    """Phase 10 (D86): a strategy per session, A+ setups, monitored before trading."""

    asia: SessionPlaybook = SessionPlaybook()
    london: SessionPlaybook = SessionPlaybook(trading=PlaybookRule())
    newyork: SessionPlaybook = SessionPlaybook(trading=PlaybookRule())
    #: Finished monitored trades before the owner is asked to approve a candidate.
    monitor_trades: int = Field(default=20, ge=1, le=500)


class SmtConfig(_StrictModel):
    """Gold-silver SMT divergence, reported as a confluence tag (D85)."""

    enabled: bool = True
    symbol_candidates: tuple[str, ...] = ("XAGUSD", "XAGUSD.m", "XAGUSDm", "SILVER")
    #: Trigger bars before the sweep that set the level each metal had to break.
    lookback_bars: int = Field(default=24, ge=3, le=500)


class ConfluenceConfig(_StrictModel):
    """Extra evidence recorded on every signal as tags; it does not change the score."""

    smt: SmtConfig = SmtConfig()


ScalpStrategy = Literal["sweep_reclaim", "trail_rejection"]
ScalpSession = Literal["asia", "london", "newyork", "overlap"]


class StepTrailConfig(_StrictModel):
    """The scalper's step-trail line: hl2 +/- multiplier x ATR, moving only with the trend."""

    atr_period: int = Field(default=10, ge=2, le=200)
    multiplier: float = Field(default=2.0, gt=0, le=10)


class ScalperRiskConfig(_StrictModel):
    """The scalper's own limits (D91); the account-wide ones come in S2."""

    #: All three children together lose this share of equity at the stop.
    risk_per_scalp_pct: float = Field(default=0.25, gt=0, le=1.0)
    max_scalps_per_day: int = Field(default=8, ge=1, le=50)
    daily_loss_limit_pct: float = Field(default=1.0, gt=0, le=10)
    #: Losses in a row that pause the scalper (24 hours, as the main strategy).
    max_consecutive_losses: int = Field(default=3, ge=1, le=20)
    #: No new scalp in the same direction this long after a stop-out.
    cooldown_sec: int = Field(default=300, ge=0, le=86_400)
    #: A scalp not yet at TP1 and below ``time_stop_min_r`` after this long is closed
    #: (0 turns the time stop off).
    time_stop_sec: int = Field(default=900, ge=0, le=86_400)
    time_stop_min_r: float = Field(default=0.3, ge=0, le=5)
    max_spread_multiple: float = Field(default=1.5, ge=1, le=10)
    #: TP1 must be at least this many times the round-trip cost away.
    cost_multiple: float = Field(default=2.0, ge=0, le=20)


ScalperMode = Literal["off", "tick", "intraday", "swing"]
StrategyChoice = Literal["main", "scalper", "both"]


def _off_if_false(value: Any) -> Any:
    """YAML reads an unquoted ``off`` as False; take it to mean "off"."""
    return "off" if value is False else value


class SwitchConfig(_StrictModel):
    """Which strategies may open trades, and the scalper's mode (D103).

    These are the defaults at startup; ``/mode`` and ``/scalper`` on Telegram (or the
    dashboard) change them while the bot runs, and the change is kept in the database.
    """

    strategies: StrategyChoice = "main"
    scalper_mode: ScalperMode = "off"

    @field_validator("scalper_mode", mode="before")
    @classmethod
    def _unquoted_off(cls, value: Any) -> Any:
        return _off_if_false(value)


class TicksConfig(_StrictModel):
    """The tick recorder (D106): every gold tick saved, for testing the tick mode."""

    record: bool = True
    #: Days of recorded ticks kept (0 keeps them all).
    keep_days: int = Field(default=0, ge=0, le=3650)


class ScalperConfig(_StrictModel):
    """The scalper add-on's settings (docs/SCALPER_SPEC.md, D88-D107).

    Whether it runs, and in which mode, is the ``switch:`` block's choice.
    """

    timeframe: Literal["M1", "M5"] = "M1"
    strategy: ScalpStrategy = "sweep_reclaim"
    #: Session windows (as in ``sessions:``) in which scalps may open.
    sessions: tuple[ScalpSession, ...] = ("asia", "london", "newyork", "overlap")
    #: Scalp only with the H1 bias (or when it is neutral).
    htf_filter: bool = True
    atr_period: int = Field(default=14, ge=2, le=200)
    step_trail: StepTrailConfig = StepTrailConfig()
    #: Bars on each side that confirm a micro swing point.
    swing_bars: int = Field(default=2, ge=1, le=10)
    #: Micro swing points formed within this many bars are liquidity.
    micro_lookback: int = Field(default=15, ge=3, le=500)
    sweep_min_atr: float = Field(default=0.05, ge=0, le=5)
    #: The reclaim must come within this many bars of the sweep.
    sweep_window: int = Field(default=6, ge=1, le=100)
    twin_bars_tolerance_atr: float = Field(default=0.05, ge=0, le=1)
    sl_buffer_atr: float = Field(default=0.10, ge=0, le=5)
    sl_min_atr: float = Field(default=0.5, ge=0, le=20)
    sl_max_atr: float = Field(default=3.0, gt=0, le=50)
    #: TP steps in R: 0.5 gives TP1/TP2/TP3 at 0.5/1.0/1.5 R ("tight"), 2.0 "wide".
    tp_step_r: float = Field(default=0.5, gt=0, le=10)
    tp_step_r_max: float = Field(default=2.0, gt=0, le=10)
    #: Shares of the scalp's lots for the three children (TP1, TP2, TP3).
    split: tuple[float, ...] = (0.4, 0.3, 0.3)
    #: Breakeven+ after TP1, the stop to TP1 after TP2, and the step-trail line.
    adaptive_trail: bool = True
    risk: ScalperRiskConfig = ScalperRiskConfig()

    @field_validator("split")
    @classmethod
    def _split_sums_to_one(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not 1 <= len(value) <= 3 or any(share <= 0 for share in value):
            raise ValueError("scalper.split needs one to three positive shares")
        if abs(sum(value) - 1.0) > 1e-6:
            raise ValueError("scalper.split must add up to 1")
        return value

    @model_validator(mode="after")
    def _ranges_in_order(self) -> Self:
        if self.sl_min_atr >= self.sl_max_atr:
            raise ValueError("scalper.sl_min_atr must be below scalper.sl_max_atr")
        if self.tp_step_r > self.tp_step_r_max:
            raise ValueError("scalper.tp_step_r must not exceed scalper.tp_step_r_max")
        if len(set(self.sessions)) != len(self.sessions):
            raise ValueError("scalper.sessions contains duplicates")
        return self


class AppConfig(_StrictModel):
    """Everything in config.yaml."""

    symbol_candidates: tuple[str, ...] = ("XAUUSD", "XAUUSD.m", "XAUUSDm", "GOLD")
    mode: Mode = Mode.PAPER
    timeframes: TimeframesConfig = TimeframesConfig()
    sessions: SessionsConfig = SessionsConfig()
    strategy: StrategyConfig = StrategyConfig()
    news: NewsConfig = NewsConfig()
    risk: RiskConfig = RiskConfig()
    pressure: PressureConfig = PressureConfig()
    backtest: BacktestConfig = BacktestConfig()
    execution: ExecutionConfig = ExecutionConfig()
    alerts: AlertsConfig = AlertsConfig()
    housekeeping: HousekeepingConfig = HousekeepingConfig()
    broker: BrokerConfig = BrokerConfig()
    models: ModelsConfig = ModelsConfig()
    confluence: ConfluenceConfig = ConfluenceConfig()
    playbooks: PlaybooksConfig = PlaybooksConfig()
    scalper: ScalperConfig = ScalperConfig()
    switch: SwitchConfig = SwitchConfig()
    ticks: TicksConfig = TicksConfig()
    display_timezone: ZoneName = "Africa/Kampala"

    @model_validator(mode="after")
    def _grid_within_strategy(self) -> Self:
        lowest = min(self.backtest.walk_forward.grid.min_rr)
        if lowest < self.strategy.min_rr:
            raise ValueError(
                f"backtest.walk_forward.grid.min_rr starts at {lowest:g}, below "
                f"strategy.min_rr ({self.strategy.min_rr:g}): the engine never produces "
                "those setups, so they cannot be tested"
            )
        return self

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_case_insensitive(cls, value: Any) -> Any:
        return _upper_if_text(value)

    @field_validator("symbol_candidates")
    @classmethod
    def _clean_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(name.strip() for name in value)
        if not cleaned or any(not name for name in cleaned):
            raise ValueError("symbol_candidates needs at least one non-empty symbol name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("symbol_candidates contains duplicates")
        return cleaned


# --------------------------------------------------------------------------- #
# .env / environment
# --------------------------------------------------------------------------- #


class EnvSettings(BaseSettings):
    """Secrets and machine-specific values, read from the environment and ``.env``.

    Blank values (``MT5_LOGIN=``) count as "not set". Process environment
    variables take priority over the ``.env`` file.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
        str_strip_whitespace=True,
        frozen=True,
    )

    mode: Mode = Mode.PAPER
    mt5_login: int | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_path: Path | None = None
    #: anthropic (default), groq, or openai_compatible (any OpenAI-style endpoint).
    llm_provider: LlmProviderName | None = None
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    #: Empty means the provider's default model.
    llm_model: str | None = None
    finnhub_api_key: SecretStr | None = None
    financecalendar_api_key: SecretStr | None = None
    fred_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    goldsweep_data_dir: Path | None = None
    goldsweep_log_dir: Path | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_case_insensitive(cls, value: Any) -> Any:
        return _upper_if_text(value)

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _chat_id_is_a_number(cls, value: Any) -> Any:
        if isinstance(value, str) and re.fullmatch(r"\s*\d{5,}:[\w-]{20,}\s*", value):
            raise ValueError(
                "this is a bot token, not a chat id: put it in TELEGRAM_BOT_TOKEN. "
                "TELEGRAM_CHAT_ID is the number of your chat with the bot "
                "(send the bot a message, then see README: Telegram setup)"
            )
        return value

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _provider_lower_case(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


# --------------------------------------------------------------------------- #
# Combined settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """Validated configuration plus environment for one run."""

    config: AppConfig
    env: EnvSettings
    project_root: Path
    config_path: Path
    env_file: Path | None
    data_dir: Path
    log_dir: Path

    @property
    def mode(self) -> Mode:
        """Effective execution mode.

        LIVE only when BOTH ``MODE`` in the environment and ``mode`` in
        config.yaml say LIVE. Any disagreement resolves to PAPER. A typed
        confirmation at startup is required on top of this before live orders
        can be sent (added with the live executor).
        """
        if self.env.mode is Mode.LIVE and self.config.mode is Mode.LIVE:
            return Mode.LIVE
        return Mode.PAPER

    @property
    def mode_summary(self) -> str:
        """The two mode switches, for display."""
        return f".env MODE={self.env.mode.value}, config.yaml mode={self.config.mode.value}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "goldsweep.sqlite3"

    @property
    def mock_db_path(self) -> Path:
        """Database used with the mock broker.

        Synthetic prices and spreads must never be stored next to real ones: they
        would be read back later as market history.
        """
        return self.data_dir / "goldsweep-mock.sqlite3"

    @property
    def display_zone(self) -> ZoneInfo:
        return ZoneInfo(self.config.display_timezone)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicated mapping keys.

    Plain PyYAML silently keeps the last duplicate, which could hide the value a
    user believes is in force (for example two ``risk_per_trade_pct`` lines).
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":  # "<<" keys are expanded by PyYAML
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable key: PyYAML reports it below
                continue
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _format_validation_error(source: str, exc: ValidationError) -> str:
    lines = [f"{source} is invalid:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_config(path: Path) -> AppConfig:
    """Read and validate config.yaml.

    Raises:
        ConfigError: file missing, not valid YAML, or failing validation.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        # utf-8-sig tolerates the byte-order mark some Windows editors add.
        raw = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)  # noqa: S506
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain key: value settings at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path.name, exc)) from exc


def _resolve_dir(
    explicit: str | os.PathLike[str] | None, from_env: Path | None, default: Path
) -> Path:
    chosen = explicit if explicit is not None else from_env
    if chosen is None:
        return default
    path = Path(chosen).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_settings(
    config_path: str | os.PathLike[str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    *,
    read_env_file: bool = True,
    data_dir: str | os.PathLike[str] | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> Settings:
    """Load config.yaml and the environment into one :class:`Settings`.

    Args:
        config_path: config file. Defaults to ``$GOLDSWEEP_CONFIG`` or
            ``config.yaml`` next to ``main.py``.
        env_file: dotenv file. Defaults to ``.env`` next to ``main.py`` when it exists.
        read_env_file: ``False`` ignores dotenv files entirely (used by tests).
        data_dir: where the SQLite database lives. Defaults to
            ``$GOLDSWEEP_DATA_DIR`` or ``data/``.
        log_dir: where log files go. Defaults to ``$GOLDSWEEP_LOG_DIR`` or ``logs/``.

    Raises:
        ConfigError: anything missing or invalid, with one line per problem.
    """
    chosen_config = config_path or os.environ.get("GOLDSWEEP_CONFIG") or DEFAULT_CONFIG_PATH
    resolved_config = Path(chosen_config).expanduser()
    config = load_config(resolved_config)

    env_path: Path | None = None
    if read_env_file:
        if env_file is not None:
            env_path = Path(env_file).expanduser()
            if not env_path.is_file():
                raise ConfigError(f"env file not found: {env_path}")
        elif DEFAULT_ENV_PATH.is_file():
            env_path = DEFAULT_ENV_PATH
    try:
        env = EnvSettings(_env_file=env_path)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(_format_validation_error("the environment / .env", exc)) from exc

    return Settings(
        config=config,
        env=env,
        project_root=PROJECT_ROOT,
        config_path=resolved_config,
        env_file=env_path,
        data_dir=_resolve_dir(data_dir, env.goldsweep_data_dir, PROJECT_ROOT / "data"),
        log_dir=_resolve_dir(log_dir, env.goldsweep_log_dir, PROJECT_ROOT / "logs"),
    )

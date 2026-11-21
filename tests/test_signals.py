"""The signal engine: sweep -> market-structure shift -> entry zone -> scored signal.

Every test replays one hand-made market, bar by bar, the way the live loop will:

* Monday 16 November 2026 and Tuesday's Asian session trade quietly around 2030.
  Four spikes set the levels: Monday's high (2048) and low (2016) become the
  previous day's high and low; Tuesday's spikes at 03:00 and 04:00 UTC set the
  Asian range (2036 / 2027), which is also the range of the 02:00-06:00 UTC
  four-hour candle, so the Asian low doubles as a CRT range low.
* At the London open (07:00 UTC, 07:00 in London in November) price drifts down,
  makes a lower high at 2029.3, raids the Asian low with a wick to 2026.4 and
  closes back above it. A displacement candle then closes above the lower high
  (the market-structure shift) and leaves a fair value gap from 2027.9 to 2029.9.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd
import pytest

from goldsweep.analysis.bias import Bias
from goldsweep.analysis.liquidity import PoolKind
from goldsweep.analysis.signals import Rejection, Signal, SignalEngine
from goldsweep.analysis.structure import Direction
from goldsweep.analysis.zones import ZoneKind
from goldsweep.core.clock import LONDON, SessionCalculator
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.candles import aggregate_candles
from tests.helpers import Row, bars, mirror, quiet, utc

LONG, SHORT = Direction.LONG, Direction.SHORT
POINT = 0.01  # XAUUSD on a two-digit quote
SPREAD = 20  # points (0.20), the spread of every fixture bar
MONDAY = utc(2026, 11, 16)
LONDON_OPEN = utc(2026, 11, 17, 7, 0)
BAR = dt.timedelta(minutes=5)
# Feeding starts here; the first call warms up over the 18 bars before it, so the
# engine also replays the 04:00 spike (which sweeps the earlier four-hour low).
FEED_FROM = utc(2026, 11, 17, 4, 30)
LEVEL = 2030.0

RAID: list[Row] = [
    (2030.0, 2030.1, 2028.9, 2029.0),  # 0  07:00 London opens lower
    (2029.0, 2029.1, 2028.3, 2028.4),  # 1
    (2028.4, 2028.6, 2028.0, 2028.3),  # 2
    (2028.3, 2028.7, 2028.1, 2028.6),  # 3
    (2028.6, 2029.3, 2028.5, 2029.1),  # 4  lower high 2029.3
    (2029.1, 2029.2, 2028.2, 2028.3),  # 5
    (2028.3, 2028.4, 2027.6, 2027.7),  # 6
    (2027.7, 2027.8, 2026.4, 2027.3),  # 7  07:35 wick through the Asian low, close back above
    (2027.3, 2027.9, 2027.1, 2027.8),  # 8
    (2027.8, 2030.6, 2027.7, 2030.4),  # 9  displacement closes above 2029.3: the shift
    (2030.4, 2031.2, 2029.9, 2031.0),  # 10 07:50 completes the gap 2027.9 .. 2029.9
]
SIGNAL_TIME = LONDON_OPEN + len(RAID) * BAR  # close of the last raid bar: 07:55


def history(
    *,
    pdh: float = 2048.0,
    pdl: float = 2016.0,
    asian_high: float = 2036.0,
    asian_low: float = 2027.0,
) -> list[Row]:
    """Monday 00:00 to Tuesday 07:00 UTC: quiet trade around 2030 and four spikes."""
    rows = quiet(int((LONDON_OPEN - MONDAY) / BAR), LEVEL, half_range=0.5)

    def at(moment: dt.datetime) -> int:
        return int((moment - MONDAY) / BAR)

    rows[at(utc(2026, 11, 16, 10))] = (LEVEL, pdh, LEVEL - 0.5, LEVEL)
    rows[at(utc(2026, 11, 16, 14))] = (LEVEL, LEVEL + 0.5, pdl, LEVEL)
    rows[at(utc(2026, 11, 17, 3))] = (LEVEL, asian_high, LEVEL - 0.5, LEVEL)
    rows[at(utc(2026, 11, 17, 4))] = (LEVEL, LEVEL + 0.5, asian_low, LEVEL)
    return rows


def with_raid(raid: Sequence[Row] = RAID, **levels: float) -> list[Row]:
    return history(**levels) + list(raid)


def engine_for(
    settings: Settings, *, sessions: SessionCalculator | None = None, **changes: object
) -> SignalEngine:
    """An engine on the shipped configuration, with ``changes`` to the strategy section."""
    config = settings.config
    return SignalEngine(
        config.strategy.model_copy(update=changes),
        config.timeframes,
        sessions or SessionCalculator(config.sessions),
        point=POINT,
    )


@dataclass
class Run:
    signals: list[Signal]
    rejections: list[Rejection]


def feed(
    engine: SignalEngine,
    rows: Sequence[Row],
    *,
    start: dt.datetime = FEED_FROM,
    news: float = 0.5,
    spread: float = SPREAD,
    median: float | None = None,
    news_windows: Sequence[tuple[dt.datetime, dt.datetime]] = (),
) -> Run:
    """Give the engine one more closed bar per call, from ``start`` on, as the live loop will.

    The liquidity and hourly frames are built once from *all* bars; the engine
    must cut them to what was closed at each step itself.
    """
    frame = bars(rows, MONDAY, spread=SPREAD)
    liquidity = aggregate_candles(frame, Timeframe.M15)
    hourly = aggregate_candles(frame, Timeframe.H1)
    first = int(frame.index.searchsorted(pd.Timestamp(start)))
    run = Run([], [])
    for end in range(first + 1, len(frame) + 1):
        run.signals += engine.on_bar(
            frame.iloc[:end],
            liquidity,
            hourly,
            spread_points=spread,
            news_bias=news,
            median_spread_points=median,
            news_windows=news_windows,
        )
        run.rejections += engine.rejections
    return run


def only_signal(run: Run) -> Signal:
    assert len(run.signals) == 1, [r.reason for r in run.rejections]
    return run.signals[0]


def reasons(run: Run) -> list[str]:
    """Why setups were turned down from the London open on."""
    return [rejection.reason for rejection in run.rejections if rejection.time > LONDON_OPEN]


# --------------------------------------------------------------------------- #
# The textbook setup
# --------------------------------------------------------------------------- #


def test_raid_of_the_asian_low_becomes_a_long_signal(settings: Settings) -> None:
    signal = only_signal(feed(engine_for(settings), with_raid()))

    assert signal.direction is LONG
    assert signal.id == "2026-11-17-LDN-01"
    assert signal.symbol == "XAUUSD"
    assert signal.time == SIGNAL_TIME
    assert signal.session == LONDON

    # What was swept: the Asian low, which is also a major swing low and the CRT low.
    pool = signal.pool
    assert (pool.kind, pool.price, pool.swept) == (PoolKind.ASIAN_LOW, 2027.0, True)
    assert set(pool.sources) == {PoolKind.ASIAN_LOW, PoolKind.SWING_LOW, PoolKind.CRT_LOW}
    assert signal.sweep.extreme == 2026.4
    assert signal.sweep.bars_to_reclaim == 0
    assert signal.sweep.pierce_time == LONDON_OPEN + 7 * BAR

    # The shift: a close above the lower high, carried by a displacement candle.
    assert signal.mss.level == 2029.3
    assert signal.mss.time == LONDON_OPEN + 9 * BAR
    assert signal.mss.displacement_atr > settings.config.strategy.displacement_atr

    # The entry: the middle of the gap the displacement left.
    zone = signal.zone
    assert zone is not None
    assert (zone.kind, zone.bottom, zone.top) == (ZoneKind.FVG, 2027.9, 2029.9)
    assert signal.entry_mode == "fvg_mid"
    assert signal.entry == pytest.approx(2028.9)

    # The stop sits beyond the sweep wick, by a tenth of an ATR plus the spread.
    assert signal.spread == pytest.approx(0.20)
    assert signal.stop_loss == pytest.approx(2026.4 - 0.1 * signal.atr - 0.20)
    assert signal.risk == pytest.approx(signal.entry - signal.stop_loss)

    # The targets: the next external buy-side liquidity, nearest first.
    assert signal.target_1.kind is PoolKind.ASIAN_HIGH
    assert signal.take_profit_1 == 2036.0
    assert signal.target_2 is not None and signal.target_2.kind is PoolKind.PREVIOUS_DAY_HIGH
    assert signal.take_profit_2 == 2048.0
    assert signal.risk_reward_1 == pytest.approx((2036.0 - signal.entry) / signal.risk)
    assert signal.risk_reward_1 >= settings.config.strategy.min_rr
    assert signal.risk_reward_2 == pytest.approx((2048.0 - signal.entry) / signal.risk)

    assert signal.bias.bias is Bias.LONG
    assert signal.expires_at == SIGNAL_TIME + 6 * BAR
    assert any("swept Asian low" in note for note in signal.notes)
    assert any("CRT range extreme" in note for note in signal.notes)


def test_score_of_the_textbook_setup(settings: Settings) -> None:
    # The spec's thresholds, whatever the shipped config trades from (D81).
    engine = engine_for(settings, alert_threshold=60, auto_trade_threshold=75)
    signal = only_signal(feed(engine, with_raid()))
    assert dict(signal.components) == pytest.approx(
        {
            # 20 x (0.6 x strength 1.0 + 0.2 x reclaimed on the same bar + 0.2 x 0.9/1.4 wick)
            "sweep_quality": 18.57,
            "displacement": 15.0,  # far beyond 2.2 ATR
            "entry_zone": 10.0,  # a fair value gap
            "fib": 0.0,  # the gap's middle is a 48% pullback, not 62-79%
            "htf_bias": 15.0,  # LONG bias, long trade
            "news_bias": 7.5,  # news +0.5
            "kill_zone": 10.0,  # London
            "spread": 5.0,  # the usual spread
            "premium_discount": 5.0,  # entry below the previous day's midpoint (2032)
            "crt": 5.0,  # the swept level is a CRT range low
        }
    )
    assert signal.score == pytest.approx(91.1)
    assert signal.alert_threshold == 60 and signal.auto_trade_threshold == 75
    assert signal.meets_alert and signal.meets_auto_trade


def test_mirror_image_gives_the_same_short(settings: Settings) -> None:
    signal = only_signal(feed(engine_for(settings), mirror(with_raid(), LEVEL), news=-0.5))
    assert signal.direction is SHORT
    assert signal.id == "2026-11-17-LDN-01"
    assert signal.pool.kind is PoolKind.ASIAN_HIGH and signal.pool.price == 2033.0
    assert signal.entry == pytest.approx(2031.1)
    assert signal.stop_loss == pytest.approx(2033.6 + 0.1 * signal.atr + 0.20)
    # A short is closed on the ask, one spread above the chart: the target is lifted.
    assert signal.take_profit_1 == pytest.approx(2024.0 + 0.20)
    assert signal.take_profit_2 == pytest.approx(2012.0 + 0.20)
    assert signal.bias.bias is Bias.SHORT
    assert signal.score == pytest.approx(91.1)


# --------------------------------------------------------------------------- #
# Replays: live and catch-up runs agree, nothing is reported twice or late
# --------------------------------------------------------------------------- #


def test_one_call_with_all_bars_gives_the_same_signal(settings: Settings) -> None:
    live = only_signal(feed(engine_for(settings), with_raid()))
    catch_up = only_signal(feed(engine_for(settings), with_raid(), start=SIGNAL_TIME - BAR))
    assert catch_up == live


def test_missed_bars_are_replayed_before_the_newest_one(settings: Settings) -> None:
    engine = engine_for(settings)
    rows = with_raid()
    assert feed(engine, rows[:-4]).signals == []  # up to the sweep bar
    later = feed(engine, rows, start=SIGNAL_TIME - BAR)  # four bars arrive at once
    assert only_signal(later) == only_signal(feed(engine_for(settings), rows))


def test_setup_completed_on_a_missed_bar_is_not_reported_late(settings: Settings) -> None:
    engine = engine_for(settings)
    rows = with_raid()
    feed(engine, rows[:-4])
    late = rows + quiet(1, 2031.0)  # the signal bar and one more arrive together
    assert feed(engine, late, start=SIGNAL_TIME).signals == []


def test_signal_is_reported_once(settings: Settings) -> None:
    engine = engine_for(settings)
    rows = with_raid()
    only_signal(feed(engine, rows))
    assert feed(engine, rows, start=SIGNAL_TIME - BAR).signals == []  # the same bar again
    more = rows + quiet(10, 2031.0, half_range=0.3)
    assert feed(engine, more, start=SIGNAL_TIME).signals == []
    assert all(pool.price != 2027.0 for pool in engine.pools)  # that liquidity is gone


def test_reset_forgets_everything(settings: Settings) -> None:
    engine = engine_for(settings)
    first = only_signal(feed(engine, with_raid()))
    assert engine.day_levels is not None and engine.pools
    engine.reset()
    assert (engine.pools, engine.day_levels, engine.bias, engine.crt_setups) == ([], None, None, [])
    again = only_signal(feed(engine, with_raid()))
    assert again.id == first.id  # the numbering starts again too


# --------------------------------------------------------------------------- #
# Raids that do not become signals
# --------------------------------------------------------------------------- #


def test_no_market_structure_shift_no_signal(settings: Settings) -> None:
    drifting = RAID[:9] + quiet(14, 2027.8)  # bounces, but never closes above 2029.3
    run = feed(engine_for(settings), with_raid(drifting))
    assert run.signals == []
    assert "no market-structure shift within 12 bars" in reasons(run)


def test_level_that_keeps_breaking_is_not_a_sweep(settings: Settings) -> None:
    accepted = (
        RAID[:7] + [(2027.7, 2027.8, 2026.4, 2026.6)] + [(2026.6, 2026.9, 2026.3, 2026.5)] * 4
    )
    run = feed(engine_for(settings), with_raid(accepted))
    assert run.signals == []
    assert "the level broke: no close back inside in time" in reasons(run)


def test_price_running_back_through_the_wick_cancels_the_setup(settings: Settings) -> None:
    collapse = RAID[:10] + [(2030.4, 2030.6, 2026.2, 2026.5)]
    run = feed(engine_for(settings), with_raid(collapse))
    assert run.signals == []
    assert "price ran back through the sweep extreme" in reasons(run)


def test_no_entry_zone_no_signal(settings: Settings) -> None:
    filled = RAID[:10] + [(2030.4, 2031.2, 2027.8, 2031.0)]  # trades back into the gap at once
    run = feed(engine_for(settings, entry_zone_priority=("fvg",)), with_raid(filled))
    assert run.signals == []
    assert "no usable fair value gap, inversion gap or order block" in reasons(run)


def test_reward_below_the_minimum_is_rejected(settings: Settings) -> None:
    run = feed(engine_for(settings, min_rr=3.0), with_raid())
    assert run.signals == []
    (reason,) = [text for text in reasons(run) if text.startswith("R:R to")]
    assert "Asian high" in reason and "below the minimum 3" in reason


def test_trade_against_the_bias_is_rejected(settings: Settings) -> None:
    strategy = settings.config.strategy
    news_led = strategy.bias.model_copy(
        update={"structure_weight": 0.5, "location_weight": 0.0, "news_weight": 0.5}
    )
    run = feed(engine_for(settings, bias=news_led), with_raid(), news=-1.0)
    assert run.signals == []
    assert "against the higher-timeframe bias (SHORT)" in reasons(run)


def test_neutral_bias_raises_the_thresholds(settings: Settings) -> None:
    engine = engine_for(settings, alert_threshold=60, auto_trade_threshold=75)
    signal = only_signal(feed(engine, with_raid(), news=0.0))
    assert signal.bias.bias is Bias.NEUTRAL
    assert (signal.alert_threshold, signal.auto_trade_threshold) == (65, 80)
    assert signal.components["htf_bias"] == 7.5
    assert signal.components["news_bias"] == 5.0
    assert any("NEUTRAL" in note for note in signal.notes)


def test_setup_outside_the_trading_windows_is_rejected(settings: Settings) -> None:
    sessions = settings.config.sessions
    no_london = sessions.model_copy(
        update={"london": sessions.london.model_copy(update={"trade": False})}
    )
    engine = engine_for(settings, sessions=SessionCalculator(no_london))
    run = feed(engine, with_raid())
    assert run.signals == []
    assert "outside the tradeable session windows" in reasons(run)


def test_post_news_window_allows_a_setup_outside_the_kill_zones(settings: Settings) -> None:
    sessions = settings.config.sessions
    no_london = SessionCalculator(
        sessions.model_copy(update={"london": sessions.london.model_copy(update={"trade": False})})
    )
    # A release at 07:30 UTC: the post-news window runs 07:45 to 09:00.
    window = (LONDON_OPEN + 9 * BAR, LONDON_OPEN + 24 * BAR)
    run = feed(engine_for(settings, sessions=no_london), with_raid(), news_windows=[window])
    signal = only_signal(run)
    assert (signal.session, signal.id) == ("post_news", "2026-11-17-NEWS-01")
    assert signal.components["kill_zone"] == 10.0
    too_early = (LONDON_OPEN + 11 * BAR, LONDON_OPEN + 24 * BAR)  # starts after the bar opens
    late = feed(engine_for(settings, sessions=no_london), with_raid(), news_windows=[too_early])
    assert late.signals == []


def test_post_news_window_never_overrides_a_hard_block(settings: Settings) -> None:
    sessions = settings.config.sessions
    # A rollover block over the London open: no window may override it.
    blocked = sessions.model_copy(
        update={
            "rollover_block": sessions.rollover_block.model_copy(
                update={"tz": "UTC", "start": dt.time(7, 30), "end": dt.time(8, 30)}
            )
        }
    )
    window = (LONDON_OPEN, LONDON_OPEN + dt.timedelta(hours=2))
    run = feed(
        engine_for(settings, sessions=SessionCalculator(blocked)),
        with_raid(),
        news_windows=[window],
    )
    assert run.signals == []
    assert any("rollover" in reason for reason in reasons(run))


def test_sweep_of_a_weak_pool_starts_no_setup(settings: Settings) -> None:
    strategy = settings.config.strategy
    plain = strategy.liquidity.model_copy(
        update={"swing_points": False, "min_sweep_pool_strength": 0.95}
    )
    crt_off = strategy.crt.model_copy(update={"enabled": False})
    engine = engine_for(settings, liquidity=plain, crt=crt_off)
    run = feed(engine, with_raid())
    assert run.signals == [] and reasons(run) == []  # taken quietly: no setup to reject
    assert all(pool.price != 2027.0 for pool in engine.pools)


def test_one_signal_per_raid(settings: Settings) -> None:
    # The previous day's low sits just under the Asian low: one wick takes both.
    engine = engine_for(settings)
    signal = only_signal(feed(engine, with_raid(pdl=2026.7)))
    assert signal.pool.kind is PoolKind.ASIAN_LOW  # the better of the two (it is also the CRT low)
    assert all(pool.price not in (2027.0, 2026.7) for pool in engine.pools)


# --------------------------------------------------------------------------- #
# Entry options
# --------------------------------------------------------------------------- #


def test_market_entry_signals_on_the_shift_itself(settings: Settings) -> None:
    signal = only_signal(feed(engine_for(settings, entry_mode="market", min_rr=1.0), with_raid()))
    assert signal.time == SIGNAL_TIME - BAR  # no waiting for the gap
    assert signal.entry == 2030.4  # the close of the shift candle
    assert signal.zone is not None and signal.zone.kind is ZoneKind.IFVG  # scored, not used


def test_inversion_gap_entry(settings: Settings) -> None:
    engine = engine_for(settings, entry_zone_priority=("ifvg", "fvg", "order_block"))
    signal = only_signal(feed(engine, with_raid()))
    zone = signal.zone
    assert zone is not None
    # The down-leg left two bearish gaps that the shift closed through: 2029.1 .. 2029.5
    # (the London-open candle) and 2028.4 .. 2028.5. The higher one is nearer to price.
    assert (zone.kind, zone.bottom, zone.top) == (ZoneKind.IFVG, 2029.1, 2029.5)
    assert signal.entry == pytest.approx(2029.3)
    assert signal.components["entry_zone"] == 8.0


def test_order_block_entry(settings: Settings) -> None:
    signal = only_signal(
        feed(engine_for(settings, entry_zone_priority=("order_block",)), with_raid())
    )
    zone = signal.zone
    assert zone is not None
    # The last down candle before the displacement: the sweep candle itself.
    assert (zone.kind, zone.bottom, zone.top) == (ZoneKind.ORDER_BLOCK, 2026.4, 2027.8)
    assert signal.entry == pytest.approx(2027.1)
    assert signal.components["entry_zone"] == 5.0


def test_entry_at_the_edge_of_the_gap(settings: Settings) -> None:
    # The top of the gap is the earliest fill but the worst price: 1.6R to the Asian high.
    top_engine = engine_for(settings, entry_mode="fvg_top", min_rr=1.5)
    top = only_signal(feed(top_engine, with_raid()))
    bottom = only_signal(feed(engine_for(settings, entry_mode="fvg_bottom"), with_raid()))
    assert (top.entry, bottom.entry) == (2029.9, 2027.9)


def test_wide_spread_moves_the_stop_and_costs_points(settings: Settings) -> None:
    wide = only_signal(feed(engine_for(settings), with_raid(), spread=40))
    assert wide.stop_loss == pytest.approx(2026.4 - 0.1 * wide.atr - 0.40)
    assert wide.components["spread"] == 0.0  # twice the usual spread
    usual = only_signal(feed(engine_for(settings), with_raid(), spread=40, median=40))
    assert usual.components["spread"] == 5.0  # the caller says 40 is normal today


# --------------------------------------------------------------------------- #
# Construction and edge cases
# --------------------------------------------------------------------------- #


def test_engine_from_settings(settings: Settings) -> None:
    engine = SignalEngine.from_settings(settings, point=POINT)
    assert only_signal(feed(engine, with_raid())).direction is LONG
    assert engine.bias is not None and engine.bias.bias is Bias.LONG
    assert engine.day_levels is not None
    assert (engine.day_levels.previous_high, engine.day_levels.previous_low) == (2048.0, 2016.0)
    assert engine.crt_setups  # Monday's four-hour candles left a few
    assert engine.pools == sorted(engine.pools, key=lambda pool: pool.price)


def test_engine_needs_a_positive_point(settings: Settings) -> None:
    with pytest.raises(ValueError, match="point"):
        SignalEngine.from_settings(settings, point=0.0)


def test_too_little_data_gives_nothing(settings: Settings) -> None:
    engine = engine_for(settings)
    empty = bars([], MONDAY)
    assert engine.on_bar(empty, empty, empty, spread_points=SPREAD) == []
    few = bars(quiet(10, LEVEL), MONDAY)
    assert engine.on_bar(few, few, few, spread_points=SPREAD) == []

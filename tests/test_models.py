"""Phase 9 stage 3 (D85): the setup models and the confluence tags.

The CISD, breaker and SMT detectors on small hand-made bars; each model on the
textbook raid of tests/test_signals.py (an Asian-low sweep at 07:55 UTC).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from goldsweep.analysis.signals import MODELS, SignalEngine, in_silver_bullet
from goldsweep.analysis.smt import smt_divergence
from goldsweep.analysis.structure import Direction, DisplacementRule, find_cisd
from goldsweep.analysis.zones import ZoneKind, find_breaker, overlaps
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import Settings
from tests.helpers import Row, bars, utc
from tests.test_signals import POINT, feed, with_raid

START = utc(2026, 11, 16, 7, 0)
ONES = np.ones(16)  # an ATR of 1.0 on every bar
RULE = DisplacementRule()
LONG, SHORT = Direction.LONG, Direction.SHORT

# A drop delivered by three down-closing candles (the first opens at 100.20) into a
# low of 97.00, then a turn: 08:05 closes at 100.70, above that opening price.
DROP_AND_TURN: list[Row] = [
    (100.0, 100.5, 99.5, 100.2),  # 0 the last up-closing candle before the drop
    (100.2, 100.3, 99.0, 99.2),  # 1 the down-run starts: it opened at 100.20
    (99.2, 99.3, 98.0, 98.2),  # 2
    (98.2, 98.4, 97.0, 97.5),  # 3 the low
    (97.5, 99.0, 97.4, 98.9),  # 4
    (98.9, 100.8, 98.8, 100.7),  # 5 body 1.80: displacement, closes above 100.20 and 100.50
]


def test_cisd_is_a_close_through_the_open_of_the_run_into_the_low() -> None:
    frame = bars(DROP_AND_TURN, START)
    event = find_cisd(
        frame, ONES[: len(frame)], LONG, extreme_index=3, reclaim_index=4, window=5, rule=RULE
    )
    assert event is not None
    assert (event.index, event.level, event.swing_index) == (5, 100.2, 1)
    assert event.displacement_index == 5
    # Without a displacement candle in the leg there is no confirmation.
    weak = [*DROP_AND_TURN[:5], (98.9, 100.5, 98.8, 100.3)]  # body 1.40 ... still displacement
    gentle = [*DROP_AND_TURN[:4], (97.5, 98.0, 97.4, 97.9), (97.9, 100.4, 97.8, 100.3)]
    assert find_cisd(
        bars(weak, START), ONES[:6], LONG, extreme_index=3, reclaim_index=4, window=5, rule=RULE
    )
    soft = DisplacementRule(atr_multiple=5.0, body_ratio=1.0, min_range_atr=5.0)
    assert (
        find_cisd(
            bars(gentle, START),
            ONES[:6],
            LONG,
            extreme_index=3,
            reclaim_index=4,
            window=5,
            rule=soft,
        )
        is None
    )


def test_breaker_is_the_last_up_candle_before_the_drop_once_closed_through() -> None:
    frame = bars(DROP_AND_TURN, START)
    breaker = find_breaker(frame, LONG, extreme_index=3, earliest=0, upto=5)
    assert breaker is not None and breaker.kind is ZoneKind.BREAKER
    assert (breaker.bottom, breaker.top, breaker.origin_index, breaker.known_index) == (
        99.5,
        100.5,
        0,
        5,  # 08:05 is the first close above its 100.50 high
    )
    assert find_breaker(frame, LONG, extreme_index=3, earliest=0, upto=4) is None  # not yet
    assert overlaps(breaker, breaker)


def frames(gold_sweep_low: float, silver_sweep_low: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """24 quiet bars, then the sweep bar: gold at 2000 (low 1999), silver at 24.20
    (low 24.00)."""
    gold = [(2000.0, 2000.5, 1999.0, 2000.0)] * 24 + [(2000.0, 2000.2, gold_sweep_low, 1999.8)]
    silver = [(24.2, 24.3, 24.0, 24.2)] * 24 + [(24.2, 24.25, silver_sweep_low, 24.1)]
    return bars(gold, START), bars(silver, START)


def smt(gold: pd.DataFrame, silver: pd.DataFrame, direction: Direction = LONG) -> bool | None:
    sweep = START + 24 * dt.timedelta(minutes=5)
    return smt_divergence(
        direction,
        gold,
        silver,
        sweep_start=sweep,
        sweep_end=sweep,
        lookback_bars=24,
        bar=dt.timedelta(minutes=5),
    )


def test_smt_when_gold_sweeps_and_silver_holds() -> None:
    assert smt(*frames(1998.0, 24.05)) is True  # gold below 1999, silver above 24.00
    assert smt(*frames(1998.0, 23.90)) is False  # silver broke too: no divergence
    assert smt(*frames(1999.5, 24.05)) is False  # gold did not make a lower low
    gold, silver = frames(1998.0, 24.05)
    assert smt(gold, silver.iloc[:5]) is None  # too few silver bars to judge


def test_silver_bullet_hours_are_new_york_time() -> None:
    assert in_silver_bullet(utc(2026, 11, 17, 15, 30))  # 10:30 New York (EST)
    assert not in_silver_bullet(utc(2026, 11, 17, 7, 50))  # 02:50 New York
    assert in_silver_bullet(utc(2026, 7, 14, 18, 15))  # 14:15 New York (EDT)


def model_run(settings: Settings, name: str):  # noqa: ANN201
    config = settings.config
    engine = SignalEngine(
        config.strategy,
        config.timeframes,
        SessionCalculator(config.sessions),
        point=POINT,
        model=MODELS[name],
    )
    return feed(engine, with_raid())


def test_each_model_on_the_textbook_raid(settings: Settings) -> None:
    (spec,) = model_run(settings, "sweep_mss").signals
    assert (spec.model, spec.id, spec.mss.level) == ("sweep_mss", "2026-11-17-LDN-01", 2029.3)
    (cisd,) = model_run(settings, "cisd").signals
    # The CISD level (the open of the run into the low) sits below the swing high.
    assert (cisd.model, cisd.id, cisd.mss.level) == ("cisd", "2026-11-17-LDN-CISD-01", 2029.1)
    assert model_run(settings, "continuation").signals == []  # the Asian low is external
    # The Silver Bullet model was replaced by session playbooks; breakout was added (D86).
    assert set(MODELS) == {"sweep_mss", "cisd", "continuation", "breakout"}


def test_confluence_tags_on_the_textbook_raid(settings: Settings) -> None:
    (signal,) = model_run(settings, "sweep_mss").signals
    # The gap sits on the breaker (Unicorn) and on an opposite gap left by the drop
    # (balanced price range); the raid ran below the New York midnight open (Judas).
    assert signal.tags == ("unicorn", "bpr", "judas")
    assert signal.score == pytest.approx(91.1)  # tags never change the score

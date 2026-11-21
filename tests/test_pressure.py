"""The buyer/seller pressure meter."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from goldsweep.analysis.pressure import (
    BALANCED,
    BUYERS,
    SELLERS,
    STRONG_BUYERS,
    STRONG_SELLERS,
    candle_reading,
    pressure_meter,
    structure_reading,
    volume_reading,
)
from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import PressureConfig, PressureWeights
from goldsweep.data.candles import empty_candles
from tests.helpers import Row, bars, utc
from tests.test_structure import STORY

START = utc(2026, 11, 17, 7, 0)
M15 = dt.timedelta(minutes=15)
UP: Row = (100.0, 101.0, 99.0, 100.8)  # closes at 90% of its range: location +0.8
DOWN: Row = (100.0, 101.0, 99.0, 99.2)  # closes at 10% of its range: location -0.8
DOJI: Row = (100.0, 100.0, 100.0, 100.0)  # no range at all

ONLY_CHARTS = PressureConfig(
    weights=PressureWeights(volume=0.5, candles=0.5, structure=0.0, news=0.0)
)


def quarter_hours(rows: list[Row], **kwargs: int) -> pd.DataFrame:
    return bars(rows, START, minutes=15, **kwargs)


def test_volume_reading_weights_close_location_by_tick_volume() -> None:
    assert volume_reading(quarter_hours([UP] * 4)) == pytest.approx(0.8)
    assert volume_reading(quarter_hours([DOWN] * 4)) == pytest.approx(-0.8)
    busy_up = bars([UP], START, minutes=15, volume=300)
    quiet_down = bars([DOWN], START + M15, minutes=15, volume=100)
    # (0.8 x 300 - 0.8 x 100) / 400: the busy bar outweighs the quiet one.
    assert volume_reading(pd.concat([busy_up, quiet_down])) == pytest.approx(0.4)
    assert volume_reading(quarter_hours([DOJI])) == 0.0
    assert volume_reading(quarter_hours([UP], volume=0)) == 0.0


def test_candle_reading_compares_bodies() -> None:
    assert candle_reading(quarter_hours([UP, UP, DOWN])) == pytest.approx(0.8 / 2.4)
    assert candle_reading(quarter_hours([DOJI])) == 0.0


def test_structure_reading_follows_the_h1_trend() -> None:
    assert structure_reading(bars(STORY[:12], START, minutes=60), 1) == 1.0
    assert structure_reading(bars(STORY, START, minutes=60), 1) == -1.0
    assert structure_reading(None, 1) == 0.0
    assert structure_reading(empty_candles(), 1) == 0.0


def test_buyers_in_control() -> None:
    reading = pressure_meter(quarter_hours([UP] * 30), ONLY_CHARTS)
    assert reading is not None
    # 0.5 x 0.8 (closes near the highs) + 0.5 x 1.0 (only up-bodies) = 0.9
    assert reading.score == pytest.approx(0.9)
    assert (reading.buyers, reading.sellers) == (95.0, 5.0)
    assert reading.label == STRONG_BUYERS
    assert reading.bars == 24  # the configured window, not all 30 bars
    assert reading.time == START + 30 * M15  # close of the last bar
    assert reading.leaning is Direction.LONG
    assert reading.agrees_with(Direction.LONG) is True
    assert reading.agrees_with(Direction.SHORT) is False
    assert reading.describe() == "Buyers 95% / Sellers 5% (strong buyers)"


def test_sellers_and_balance() -> None:
    sellers = pressure_meter(quarter_hours([DOWN] * 24), ONLY_CHARTS)
    assert sellers is not None and sellers.label == STRONG_SELLERS
    assert sellers.leaning is Direction.SHORT
    balanced = pressure_meter(quarter_hours([UP, DOWN] * 12), ONLY_CHARTS)
    assert balanced is not None
    assert balanced.label == BALANCED and balanced.score == pytest.approx(0.0)
    assert balanced.leaning is None and balanced.agrees_with(Direction.LONG) is None


@pytest.mark.parametrize(
    ("news", "label"),
    [(0.3, BUYERS), (-0.3, SELLERS), (0.05, BALANCED), (5.0, STRONG_BUYERS)],
)
def test_news_moves_the_meter(news: float, label: str) -> None:
    news_only = PressureConfig(
        weights=PressureWeights(volume=0.0, candles=0.0, structure=0.0, news=1.0)
    )
    reading = pressure_meter(quarter_hours([DOJI] * 5), news_only, news_bias=news)
    assert reading is not None and reading.label == label
    assert -1.0 <= reading.components["news"] <= 1.0


def test_default_weights_and_no_bars() -> None:
    reading = pressure_meter(quarter_hours([UP] * 24), PressureConfig())
    assert reading is not None
    # 0.40 x 0.8 + 0.25 x 1.0; no hourly bars for structure, no news yet.
    assert reading.score == pytest.approx(0.57)
    assert set(reading.components) == {"volume", "candles", "structure", "news"}
    uptrend = bars(STORY[:12], START, minutes=60)
    with_structure = pressure_meter(
        quarter_hours([UP] * 24), PressureConfig(), hourly=uptrend, swing_lookback=1
    )
    assert with_structure is not None and with_structure.score == pytest.approx(0.72)
    assert pressure_meter(empty_candles(), PressureConfig()) is None

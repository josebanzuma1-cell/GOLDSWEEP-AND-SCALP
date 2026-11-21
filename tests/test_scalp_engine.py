"""The scalper's setups (S1, prompt section 4.3), on hand-made one-minute bars.

The SELL fixture: a falling market (step trail down, ATR periods of 3 for a short
fixture); a swing high of 2007.50 forms at 07:35, is swept to 2007.80 at 07:37 and
closed back below on that bar; at 07:38 a bearish bar closes at 2005.20, below the
sweep candle's low of 2005.70: SELL at the 07:39 close of that bar.
"""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import ScalperConfig, StepTrailConfig
from goldsweep.scalping.engine import MODES, SWEEP_RECLAIM, TRAIL_REJECTION, ScalpEngine, ScalpSetup
from tests.helpers import mirror, utc

START = utc(2026, 11, 17, 7, 30)
MINUTE = dt.timedelta(minutes=1)
SELL_ROWS = [
    (2010.0, 2010.5, 2009.0, 2009.2),
    (2009.2, 2009.6, 2008.0, 2008.2),
    (2008.2, 2008.6, 2007.0, 2007.2),
    (2007.2, 2007.6, 2006.0, 2006.2),
    (2006.2, 2007.0, 2005.8, 2006.8),
    (2006.8, 2007.5, 2006.3, 2006.6),  # 07:35 swing high 2007.50
    (2006.6, 2006.9, 2005.6, 2005.8),
    (2005.8, 2007.8, 2005.7, 2006.9),  # 07:37 swept to 2007.80, closed back below
    (2006.9, 2007.0, 2005.0, 2005.2),  # 07:38 closes below 2005.70: the shift
]


def config(**changes: object) -> ScalperConfig:
    values: dict[str, object] = {
        "atr_period": 3,
        "step_trail": StepTrailConfig(atr_period=3, multiplier=1.0),
        "swing_bars": 1,
    }
    return ScalperConfig(**(values | changes))  # type: ignore[arg-type]


def run(rows: list, **changes: object) -> list[ScalpSetup]:
    modes = changes.pop("modes", MODES)
    engine = ScalpEngine(config(**changes), bar=MINUTE, modes=modes)  # type: ignore[arg-type]
    found: list[ScalpSetup] = []
    for i, (open_, high, low, close) in enumerate(rows):
        found += engine.on_bar(START + i * MINUTE, open_, high, low, close)
    return found


def test_a_sweep_closed_back_inside_then_a_shift_is_a_sell() -> None:
    (setup,) = run(SELL_ROWS)
    assert setup.index == 8 and setup.time == utc(2026, 11, 17, 7, 39)
    assert (setup.direction, setup.mode) == (Direction.SHORT, SWEEP_RECLAIM)
    assert (setup.reference, setup.extreme, setup.level_price) == (2005.2, 2007.8, 2007.5)
    assert setup.level_kind == "swing" and setup.sweep_time == utc(2026, 11, 17, 7, 37)
    assert setup.atr == pytest.approx(1.8)
    assert setup.trail_line == pytest.approx(2007.4833, abs=1e-4)
    assert not setup.flipped
    assert setup.score == 60.0
    assert (
        setup.reason == "swing high 2007.50 swept to 2007.80, closed back inside, structure shift"
    )


def test_the_mirror_image_is_a_buy() -> None:
    (setup,) = run(mirror(SELL_ROWS, 2000.0))
    assert setup.direction is Direction.LONG and setup.index == 8
    assert (setup.reference, setup.extreme) == (pytest.approx(1994.8), pytest.approx(1992.2))
    assert setup.reason.startswith("swing low 1992.50 swept to 1992.20")


def test_no_shift_no_setup() -> None:
    rows = [*SELL_ROWS[:8], (2006.9, 2007.0, 2005.9, 2006.0)]  # closes above 2005.70
    assert run(rows) == []


def test_the_shift_must_come_within_the_sweep_window() -> None:
    pause = (2006.9, 2007.1, 2006.2, 2006.5)
    rows = [*SELL_ROWS[:8], pause, SELL_ROWS[8]]
    assert run(rows, sweep_window=1) == []
    (setup,) = run(rows, sweep_window=6)
    assert setup.index == 9


def test_one_setup_per_sweep() -> None:
    rows = [*SELL_ROWS, (2005.2, 2005.4, 2004.0, 2004.1)]  # bearish again, lower again
    assert [setup.index for setup in run(rows)] == [8]


def test_a_rejection_of_the_step_line_in_a_downtrend() -> None:
    rejection = (2006.0, 2007.6, 2005.4, 2005.5)  # wick to the line 2007.48, body below
    rows = [*SELL_ROWS[:7], rejection]
    (setup,) = run(rows)
    assert (setup.mode, setup.direction, setup.index) == (TRAIL_REJECTION, Direction.SHORT, 7)
    assert setup.extreme == 2007.6 and setup.level_kind is None
    assert setup.reason == "rejected the step trail at 2007.60, structure shift"
    assert run(rows, modes=(SWEEP_RECLAIM,)) == []  # only the configured mode runs


def test_later_bars_never_change_earlier_setups() -> None:
    more = [*SELL_ROWS, (2005.2, 2006.0, 2004.8, 2005.9), (2005.9, 2006.5, 2005.5, 2006.3)]
    assert run(more)[:1] == run(SELL_ROWS)


def test_unknown_modes_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown scalper modes"):
        ScalpEngine(config(), bar=MINUTE, modes=("momentum_burst",))

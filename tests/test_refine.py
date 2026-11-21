"""M1 refinement ("sniper" entries, Phase 9 stage 2) on hand-made one-minute bars.

The setup is an M5 long whose gap runs from 2027.90 to 2029.90 (price comes back
to its top first), with the M5 stop at 2026.00 and targets 2045 / 2050. The M1
pullback makes a lower high at 2031.40 (08:02), touches the gap at 08:05 on the
ask, bottoms at 2028.50 (08:06) and turns: a close above 2031.40 at 08:09 is the
change of character. Bars carry a 0.20 spread.
"""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.refine import RefineSetup, RefineStatus, refine
from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import RefinementConfig
from tests.helpers import Row, bars, mirror, quiet, utc

SETUP_TIME = utc(2026, 11, 17, 8, 0)
M1 = dt.timedelta(minutes=1)
CONTEXT = quiet(30, 2031.0, half_range=0.1)  # 07:30-07:59: no swings, no gap
PULLBACK: list[Row] = [
    (2031.0, 2031.1, 2030.4, 2030.5),  # 08:00
    (2030.5, 2030.6, 2030.0, 2030.1),  # 08:01
    (2030.1, 2031.4, 2030.0, 2031.2),  # 08:02 the last lower high: 2031.40
    (2031.2, 2031.3, 2030.6, 2030.7),  # 08:03
    (2030.7, 2030.8, 2029.8, 2029.9),  # 08:04 ask low 2030.00: not yet in the gap
    (2029.9, 2030.0, 2029.2, 2029.4),  # 08:05 ask low 2029.40: touches the gap
    (2029.4, 2029.6, 2028.5, 2028.7),  # 08:06 the pullback's low, 2028.50
    (2028.7, 2029.9, 2028.6, 2029.8),  # 08:07
    (2029.8, 2031.0, 2029.7, 2030.9),  # 08:08
    (2030.9, 2031.8, 2030.8, 2031.6),  # 08:09 closes above 2031.40: the turn
    (2031.6, 2032.0, 2031.4, 2031.9),  # 08:10
]
#: No ATR in the stop, so the numbers are easy to follow: stop = 2028.50 - 0.20 spread.
PLAIN = RefinementConfig(mode="m1", sl_buffer_atr=0.0, min_stop_atr=0.0)


def cfg(**changes: object) -> RefinementConfig:
    return PLAIN.model_copy(update=changes)


def setup(**changes: object) -> RefineSetup:
    values: dict[str, object] = {
        "id": "R-01",
        "time": SETUP_TIME,
        "direction": Direction.LONG,
        "session": "london",
        "zone_top": 2029.9,
        "zone_bottom": 2027.9,
        "stop_loss": 2026.0,
        "take_profit_1": 2045.0,
        "take_profit_2": 2050.0,
        "expires_at": SETUP_TIME + 2 * dt.timedelta(hours=1),
        "score": 80.0,
        "spread": 0.2,
    }
    values.update(changes)
    return RefineSetup(**values)  # type: ignore[arg-type]


def m1(rows: list[Row]):  # noqa: ANN201
    return bars(CONTEXT + rows, SETUP_TIME - 30 * M1, minutes=1)


def test_a_change_of_character_inside_the_gap_gives_a_market_entry() -> None:
    result = refine(setup(), m1(PULLBACK), PLAIN)
    assert result.status is RefineStatus.READY
    assert result.time == utc(2026, 11, 17, 8, 10)  # the close of the 08:09 bar
    assert result.entry_mode == "market"
    assert result.entry == pytest.approx(2031.8)  # the 2031.60 close, on the ask
    assert result.stop_loss == pytest.approx(2028.3)  # beyond the 2028.50 low, by the spread
    # 3.50 at risk instead of the M5 setup's 2.90 (2028.90 - 2026.00) from a worse
    # price: the refined trade waited for proof that the gap held.


def test_the_turn_is_never_judged_on_bars_that_have_not_closed() -> None:
    assert refine(setup(), m1(PULLBACK[:9]), PLAIN).status is RefineStatus.WAITING
    waiting = refine(setup(), m1(PULLBACK[:4]), PLAIN)
    assert (waiting.status, waiting.detail) == (
        RefineStatus.WAITING,
        "waiting for price to come back to the gap",
    )
    assert refine(setup(), m1(PULLBACK[:7]), PLAIN).detail == "waiting for the turn"
    # The same answer however many later bars arrive: the decision is in the past.
    assert refine(setup(), m1(PULLBACK + quiet(5, 2032.0)), PLAIN) == refine(
        setup(), m1(PULLBACK), PLAIN
    )


def test_cisd_turns_earlier_than_the_change_of_character() -> None:
    rows = list(PULLBACK)
    rows[4] = (2030.7, 2030.8, 2029.8, 2030.75)  # 08:04 closes up: the down-run starts at 08:05
    rows[5] = (2030.75, 2030.8, 2029.2, 2029.4)
    cisd = refine(setup(), m1(rows), cfg(confirm="cisd"))
    # The run into the low opened at 2030.75; the 08:08 close of 2030.90 clears it.
    assert (cisd.status, cisd.time) == (RefineStatus.READY, utc(2026, 11, 17, 8, 9))
    assert refine(setup(), m1(rows), PLAIN).time == utc(2026, 11, 17, 8, 10)


def test_limit_entry_at_the_middle_of_the_m1_gap() -> None:
    result = refine(setup(), m1(PULLBACK), cfg(entry="fvg_mid"))
    # The 08:08 candle left a gap from 2029.90 (08:07 high) to 2030.80 (08:09 low).
    assert (result.entry_mode, result.entry) == ("fvg_mid", pytest.approx(2030.35))
    assert result.order_expires_at == utc(2026, 11, 17, 8, 20)  # 10 M1 bars
    assert result.stop_loss == pytest.approx(2028.3)


def test_the_stop_has_a_floor_and_never_exceeds_the_m5_stop() -> None:
    wide_floor = cfg(min_stop_spreads=30)  # at least 6.00
    result = refine(setup(), m1(PULLBACK), wide_floor)
    assert result.stop_loss == pytest.approx(2026.0)  # 2025.80 would pass the M5 stop
    floor = cfg(min_stop_spreads=20)  # at least 4.00
    assert refine(setup(), m1(PULLBACK), floor).stop_loss == pytest.approx(2031.8 - 4.0)


def test_no_trade_when_the_setup_fails_or_runs_away() -> None:
    expired = refine(setup(expires_at=utc(2026, 11, 17, 8, 7)), m1(PULLBACK), PLAIN)
    assert expired.status is RefineStatus.EXPIRED
    failed = refine(setup(stop_loss=2028.6), m1(PULLBACK), PLAIN)
    assert (failed.status, failed.detail) == (
        RefineStatus.FAILED,
        "price traded through the M5 stop",
    )
    runaway = refine(setup(take_profit_1=2031.05), m1(PULLBACK), PLAIN)
    assert runaway.status is RefineStatus.TP1_FIRST
    too_little = refine(setup(take_profit_1=2034.0), m1(PULLBACK), PLAIN)
    assert too_little.status is RefineStatus.FAILED and "below 2" in too_little.detail


def test_a_short_mirrors_the_long() -> None:
    short = setup(
        direction=Direction.SHORT,
        zone_top=2032.1,
        zone_bottom=2030.1,
        stop_loss=2034.0,
        take_profit_1=2015.0,
        take_profit_2=2010.0,
    )
    result = refine(
        short, bars(mirror(CONTEXT + PULLBACK, 2030.0), SETUP_TIME - 30 * M1, minutes=1), PLAIN
    )
    assert (result.status, result.time) == (RefineStatus.READY, utc(2026, 11, 17, 8, 10))
    assert result.entry == pytest.approx(2028.4)  # sold on the bid
    assert result.stop_loss == pytest.approx(2031.7)  # above the 2031.50 high, by the spread


def test_setup_state_round_trips() -> None:
    original = setup()
    assert RefineSetup.from_state(original.to_state()) == original
    assert original.near == 2029.9

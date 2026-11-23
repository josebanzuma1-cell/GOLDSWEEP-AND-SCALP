"""The scalper's position rules in the shared trade book (S1, prompt section 4.5).

Breakeven+ after TP1, the stop to TP1 after TP2, the step-trail stop that only ever
tightens (the owner's video 2: 4067.64 -> ... -> 4059.30), and the time stop.
"""

from __future__ import annotations

import datetime as dt

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.core.settings import Settings
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import (
    Bar,
    EventKind,
    ExitReason,
    Management,
    TradeBook,
    TradePlan,
    breakeven_plus_stop,
    ladder_stop,
    step_trailed_stop,
    time_stop_due,
    to_ns,
)
from goldsweep.scalping.plan import scalp_risk
from tests.helpers import utc

MINUTE = dt.timedelta(minutes=1)
T0 = utc(2026, 11, 17, 7, 39)

# -- the rules ---------------------------------------------------------------- #


def test_the_video_two_stop_only_ever_tightens() -> None:
    stop = 4067.64
    seen = [stop]
    for line in (4066.15, 4066.50, 4065.66, 4064.91, 4065.20, 4063.71, 4060.53, 4059.62, 4059.30):
        moved = step_trailed_stop(
            long=False, stop=stop, line=line, buffer=0.0, close=4055.0, spread=0.0
        )
        if moved is not None:
            stop = moved
            seen.append(stop)
    # 4066.50 and 4065.20 would have loosened it: ignored.
    assert seen == [4067.64, 4066.15, 4065.66, 4064.91, 4063.71, 4060.53, 4059.62, 4059.30]
    assert all(later < earlier for earlier, later in zip(seen, seen[1:], strict=False))


def test_step_trail_stop_sides_and_buffers() -> None:
    long = step_trailed_stop(
        long=True, stop=1990.0, line=1995.0, buffer=0.5, close=2000.0, spread=0.2
    )
    assert long == 1994.5
    short = step_trailed_stop(
        long=False, stop=2010.0, line=2005.0, buffer=0.5, close=2000.0, spread=0.2
    )
    assert short == pytest.approx(2005.7)  # + buffer + spread: a short is stopped on the ask
    # Never on the winning side of the price, never back, never from a missing line.
    assert (
        step_trailed_stop(long=True, stop=1990.0, line=2001.0, buffer=0.0, close=2000.0, spread=0.0)
        is None
    )
    assert (
        step_trailed_stop(long=True, stop=1996.0, line=1995.0, buffer=0.0, close=2000.0, spread=0.0)
        is None
    )
    assert (
        step_trailed_stop(
            long=True, stop=1990.0, line=float("nan"), buffer=0.0, close=2000.0, spread=0.0
        )
        is None
    )


def test_breakeven_plus_and_the_stop_to_tp1() -> None:
    assert breakeven_plus_stop(
        long=True, stop=1997.0, fill_price=2000.0, offset=0.17, close=2001.0, spread=0.2
    ) == pytest.approx(2000.17)
    assert breakeven_plus_stop(
        long=False, stop=2003.0, fill_price=2000.0, offset=0.17, close=1999.0, spread=0.2
    ) == pytest.approx(1999.83)
    # Price back through the level: it cannot be placed.
    assert (
        breakeven_plus_stop(
            long=True, stop=1997.0, fill_price=2000.0, offset=0.17, close=2000.1, spread=0.2
        )
        is None
    )
    assert (
        ladder_stop(long=False, stop=1999.83, take_profit_1=1998.6, close=1997.0, spread=0.2)
        == 1998.6
    )
    assert (
        ladder_stop(long=False, stop=1998.0, take_profit_1=1998.6, close=1997.0, spread=0.2) is None
    )


def test_the_time_stop() -> None:
    common = {"fill_price": 2000.0, "initial_stop": 1998.0, "spread": 0.2, "min_r": 0.3}
    after = dt.timedelta(minutes=15)
    held = dt.timedelta(minutes=15)
    assert time_stop_due(long=True, close=2000.5, held=held, after=after, **common)  # +0.25 R
    assert not time_stop_due(long=True, close=2000.7, held=held, after=after, **common)  # +0.35 R
    assert not time_stop_due(long=True, close=1999.0, held=held - MINUTE, after=after, **common)
    # A short closes on the ask: 1999.5 + 0.2 is only +0.15 R.
    short = common | {"initial_stop": 2002.0}
    assert time_stop_due(long=False, close=1999.5, held=held, after=after, **short)
    assert not time_stop_due(long=True, close=1990.0, held=held, after=dt.timedelta(0), **common)


# -- the book ------------------------------------------------------------------ #


class FixedLine:
    def __init__(self, line: float, atr: float = 1.0) -> None:
        self.line, self.atr = line, atr

    def line_at(self, close_ns: int) -> tuple[float, float]:
        return self.line, self.atr


def book(settings: Settings, rules: Management) -> TradeBook:
    config = settings.config
    return TradeBook(
        default_gold_spec(),
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=10_000.0,
        slippage_points=0.0,
        commission_per_lot=0.0,
        trail_after_tp1=False,
        management=rules,
    )


def bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    opened = to_ns(T0 + minute * MINUTE)
    return Bar(opened, opened + 60 * 10**9, open_, high, low, close, 0.0)


SHORT = TradePlan(
    id="SC-00001",
    time=T0,
    direction=Direction.SHORT,
    session="london",
    entry_mode="market",
    entry=2005.2,
    stop_loss=2007.98,
    take_profit_1=2003.81,
    take_profit_2=2002.42,
    take_profit_3=2001.03,
    expires_at=T0 + MINUTE,
)
LADDER = Management(split=(0.4, 0.3, 0.3), breakeven_offset=0.1, stop_to_tp1_after_tp2=True)


def test_a_short_scalp_tp1_breakeven_plus_tp2_stop_to_tp1(settings: Settings) -> None:
    scalps = book(settings, LADDER)
    placed = scalps.submit(SHORT, spread_points=0.0, median_spread_points=None)
    assert placed.kind is EventKind.PLACED and placed.lots == 0.08  # 25.00 / 278 per lot
    events = []
    events += scalps.on_bar(bar(0, 2005.2, 2005.5, 2004.6, 2004.8))  # filled at the open
    events += scalps.on_bar(bar(1, 2004.8, 2004.9, 2003.7, 2003.9))  # TP1: child 1 out
    events += scalps.on_bar(bar(2, 2003.9, 2004.0, 2002.3, 2002.5))  # TP2: child 2 out
    events += scalps.on_bar(bar(3, 2002.5, 2004.0, 2002.2, 2003.9))  # back up to TP1
    moves = [(e.detail, e.price) for e in events if e.kind is EventKind.STOP_MOVED]
    assert moves == [("breakeven", pytest.approx(2005.1)), ("trail", 2003.81)]
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    trade = closed.trade
    assert trade is not None
    assert [(leg.lots, leg.reason) for leg in trade.legs] == [
        (0.03, ExitReason.TP1),  # 0.08 lots in whole steps, largest remainders first
        (0.03, ExitReason.TP2),
        (0.02, ExitReason.TRAIL),
    ]
    assert trade.net_pnl > 0  # the last child kept TP1's gain


def test_the_step_line_trails_the_stop_from_the_first_bar(settings: Settings) -> None:
    scalps = book(
        settings, Management(split=(0.4, 0.3, 0.3), step_trail=True, trail_buffer_atr=0.1)
    )
    scalps.submit(SHORT, spread_points=0.0, median_spread_points=None)
    scalps.on_bar(bar(0, 2005.2, 2005.5, 2004.6, 2004.8), line=FixedLine(2007.5))
    events = scalps.on_bar(bar(1, 2004.8, 2005.0, 2004.2, 2004.4), line=FixedLine(2006.0))
    (moved,) = [e for e in events if e.kind is EventKind.STOP_MOVED]
    assert (moved.detail, moved.price) == ("trail", pytest.approx(2006.1))
    events = scalps.on_bar(bar(2, 2004.4, 2004.6, 2004.0, 2004.3), line=FixedLine(2007.0))
    assert not [e for e in events if e.kind is EventKind.STOP_MOVED]  # never loosened
    assert scalps.positions[0]["stop"] == pytest.approx(2006.1)


def test_the_time_stop_closes_a_stalled_scalp_once(settings: Settings) -> None:
    rules = Management(
        split=(0.4, 0.3, 0.3), time_stop=dt.timedelta(minutes=3), time_stop_min_r=0.3
    )
    scalps = book(settings, rules)
    scalps.submit(SHORT, spread_points=0.0, median_spread_points=None)
    scalps.on_bar(bar(0, 2005.2, 2005.5, 2004.6, 2005.0))
    scalps.on_bar(bar(1, 2005.0, 2005.4, 2004.8, 2005.1))
    events = scalps.on_bar(bar(2, 2005.1, 2005.3, 2004.7, 2004.9))  # 3 minutes held, +0.11 R
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed.trade is not None and closed.trade.exit_reason is ExitReason.TIME
    assert closed.price == pytest.approx(2004.9)  # bought back on the ask (spread 0 here)

    working = book(settings, rules)
    working.submit(SHORT, spread_points=0.0, median_spread_points=None)
    working.on_bar(bar(0, 2005.2, 2005.5, 2004.6, 2005.0))
    working.on_bar(bar(1, 2005.0, 2005.1, 2004.3, 2004.4))
    working.on_bar(bar(2, 2004.4, 2004.5, 2004.1, 2004.2))  # +0.36 R at the look: kept
    working.on_bar(bar(3, 2004.2, 2005.1, 2004.1, 2005.0))  # back to +0.07 R: one look only
    assert len(working.positions) == 1
    state = working.to_state()["positions"][0]
    assert state["time_checked"] is True and state["tp2_done"] is False
    restored = book(settings, rules)
    restored.restore(working.to_state())
    assert restored.to_state() == working.to_state()


def test_a_book_without_management_behaves_as_before(settings: Settings) -> None:
    plain = book(settings, Management())
    plain.submit(SHORT, spread_points=0.0, median_spread_points=None)
    plain.on_bar(bar(0, 2005.2, 2005.5, 2004.6, 2004.8))
    events = plain.on_bar(bar(1, 2004.8, 2004.9, 2003.7, 2003.9), line=FixedLine(2005.5))
    (moved,) = [e for e in events if e.kind is EventKind.STOP_MOVED]
    assert moved.price == 2005.2  # plain breakeven at TP1, no step trail, no offset

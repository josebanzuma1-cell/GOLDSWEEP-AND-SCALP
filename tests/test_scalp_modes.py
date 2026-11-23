"""The scalper's intraday (M5) and swing-scalping (M15) modes (S6, D112).

The building blocks first (higher timeframes from M1, the H1 trend, the swing engine,
the plans and the flat-by times), then the modes running in the bot on the hand-made
M1 market of tests/test_scalper_runner.py, on paper and on the demo account, and the
mode backtest that replays M1 bars through the same objects.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.backtest.data import save_symbol_spec
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, ConfigError, Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.mock_broker import default_gold_spec, generate_m1_candles
from goldsweep.scalping import mode_backtest
from goldsweep.scalping.bars import BarAggregator, Candle, HourlyTrend
from goldsweep.scalping.logic import (
    HFT,
    INTRADAY,
    SWING,
    Candidate,
    Gate,
    ModeLogic,
    anchored_plan,
    before_weekend,
    make_logic,
    mode_management,
    next_rollover,
)
from goldsweep.scalping.plan import LadderCosts
from goldsweep.scalping.swing_scalp import EMA_PULLBACK, RANGE_BREAKOUT, SwingScalpEngine
from goldsweep.storage.db import Database
from tests.helpers import utc, without_grade_filter
from tests.test_scalper_runner import Harness, demo_harness
from tests.test_settings import load, write_text

MINUTE = dt.timedelta(minutes=1)
SIGNAL = utc(2026, 11, 17, 8, 0)  # the close of the 07:59 M1 bar, inside London


# --------------------------------------------------------------------------- #
# Higher timeframes and the H1 trend
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """S9's A/A+ filter is off here: these tests follow what a scalp does once placed."""
    return without_grade_filter(settings)


def test_m1_bars_become_m5_bars_at_their_last_minute() -> None:
    five = BarAggregator(5)
    start = utc(2026, 11, 17, 8, 0)
    out: list[Candle] = []
    for minute in range(10):
        price = 100.0 + minute
        out += five.add(start + minute * MINUTE, price, price + 1, price - 1, price + 0.5, 2.0)
        if minute == 3:
            assert out == []  # the 08:00 bar is not finished before its 08:04 minute
    first, second = out
    assert (first.opened, first.open, first.high, first.low, first.close) == (
        start,
        100.0,
        105.0,
        99.0,
        104.5,
    )
    assert (first.volume, first.closed, second.opened) == (
        10.0,
        utc(2026, 11, 17, 8, 5),
        start + 5 * MINUTE,
    )


def test_a_bar_without_its_last_minute_ends_with_the_next_bucket() -> None:
    fifteen = BarAggregator(15)
    start = utc(2026, 11, 17, 8, 0)
    assert fifteen.add(start, 1, 2, 0.5, 1.5) == []
    assert fifteen.add(start + 10 * MINUTE, 1.5, 3, 1, 2.5) == []  # no trade at 08:14
    (done,) = fifteen.add(start + 16 * MINUTE, 2.5, 2.6, 2.4, 2.5)
    assert (done.opened, done.high, done.close) == (start, 3, 2.5)
    with pytest.raises(ValueError, match="divide 60"):
        BarAggregator(7)


def feed_hours(trend: HourlyTrend, start: dt.datetime, prices: Sequence[float]) -> dt.datetime:
    """One M1 bar a minute, each hour closing at the next price."""
    moment = start
    for price in prices:
        for _ in range(60):
            trend.add(moment, price, price + 0.1, price - 0.1, price)
            moment += MINUTE
    return moment


def test_the_h1_trend() -> None:
    trend = HourlyTrend(ema_hours=5, slope_hours=2)
    start = utc(2026, 11, 16, 0, 0)
    moment = feed_hours(trend, start, [100.0 + h for h in range(4)])
    assert trend.direction is None and math.isnan(trend.ema)  # not ready: 4 of 5 hours
    moment = feed_hours(trend, moment, [104.0 + h for h in range(6)])
    assert trend.direction is Direction.LONG
    moment = feed_hours(trend, moment, [109.0 - 2 * h for h in range(8)])
    assert trend.direction is Direction.SHORT
    feed_hours(trend, moment, [trend.ema] * 1)
    assert trend.direction is None  # the close sits on the EMA: no trend


# --------------------------------------------------------------------------- #
# The swing-scalping engine (M15)
# --------------------------------------------------------------------------- #


@pytest.fixture
def small(settings: Settings) -> Settings:
    """Short look-backs, so a handful of hand-made bars warms the engine up."""
    scalper = settings.config.scalper
    swing = scalper.swing_scalp.model_copy(
        update={"ema_bars": 3, "pullback_bars": 2, "channel_bars": 3}
    )
    scalper = scalper.model_copy(update={"swing_scalp": swing, "atr_period": 3})
    return dataclasses.replace(
        settings, config=settings.config.model_copy(update={"scalper": scalper})
    )


def m15(opened: dt.datetime, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(opened, o, h, low, c, 1.0, 15)


RISING = [(c - 0.5, c + 0.5, c - 1.0, c) for c in (100.0, 101.0, 102.0, 103.0, 104.0)]
PULLBACK = [
    (104.0, 104.2, 102.0, 102.5),  # the low reaches the EMA (about 103.06)
    (102.5, 105.0, 102.4, 104.9),  # back above it and above the last high: LONG
    (104.9, 105.4, 104.0, 105.3),  # no new touch: nothing
]


def run_swing(
    engine: SwingScalpEngine,
    rows: list[tuple[float, float, float, float]],
    start: dt.datetime,
    trend: Direction | None,
) -> list[Any]:
    found = []
    for i, row in enumerate(rows):
        found += engine.on_bar(m15(start + i * 15 * MINUTE, *row), trend)
    return found


def test_the_ema_pullback(small: Settings, sessions: SessionCalculator) -> None:
    start = utc(2026, 11, 17, 8, 0)
    engine = SwingScalpEngine(small.config.scalper, sessions, rules=(EMA_PULLBACK,))
    (setup,) = run_swing(engine, RISING + PULLBACK, start, Direction.LONG)
    assert (setup.rule, setup.direction, setup.reference, setup.anchor) == (
        EMA_PULLBACK,
        Direction.LONG,
        104.9,
        102.0,  # the lowest low of the pullback's two bars
    )
    assert setup.time == start + 7 * 15 * MINUTE  # the close of the seventh bar
    assert "closed above it, H1 trend up" in setup.detail
    # The channel: the lowest low (highest high) of the last three bars.
    assert engine.line_for(0, True) == (102.0, engine.atr)
    assert engine.line_for(0, False) == (105.4, engine.atr)
    # Without the H1 trend nothing trades; against it, neither.
    flat = SwingScalpEngine(small.config.scalper, sessions, rules=(EMA_PULLBACK,))
    assert run_swing(flat, RISING + PULLBACK, start, None) == []
    against = SwingScalpEngine(small.config.scalper, sessions, rules=(EMA_PULLBACK,))
    # Against an H1 downtrend no long is taken (the dip through the EMA is a short there).
    shorts = run_swing(against, RISING + PULLBACK, start, Direction.SHORT)
    assert all(s.direction is Direction.SHORT for s in shorts)


def test_the_range_breakout(small: Settings, sessions: SessionCalculator) -> None:
    asia = [(100.0, 101.0, 99.0, 100.0)] * 24  # 00:00-06:00 UTC: the Tokyo window
    after = [
        (100.0, 100.5, 99.5, 100.2),  # 06:00: inside
        (100.2, 102.5, 100.1, 102.4),  # 06:15: closes above 101 + 0.1 ATR: LONG
        (102.4, 103.5, 102.3, 103.4),  # 06:30: already done today
    ]
    engine = SwingScalpEngine(small.config.scalper, sessions, rules=(RANGE_BREAKOUT,))
    (setup,) = run_swing(engine, asia + after, utc(2026, 11, 17, 0, 0), Direction.LONG)
    assert (setup.rule, setup.anchor, setup.time) == (
        RANGE_BREAKOUT,
        100.0,  # the stop goes beyond the range's middle
        utc(2026, 11, 17, 6, 30),
    )
    assert "above the Asian range 99.00-101.00" in setup.detail


# --------------------------------------------------------------------------- #
# Plans, management and the flat-by times
# --------------------------------------------------------------------------- #


def candidate(**changes: Any) -> Candidate:
    values: dict[str, Any] = {
        "time": SIGNAL,
        "direction": Direction.LONG,
        "rule": "sweep_reclaim",
        "reference": 2030.0,
        "anchor": 2027.0,
        "atr": 2.0,
        "reason": "test",
        "close_by": utc(2026, 11, 17, 21, 45),
    }
    values.update(changes)
    return Candidate(**values)


def test_the_anchored_plan() -> None:
    costs = LadderCosts(spread=0.2, slippage=0.1, commission=0.0)
    options = {
        "ladder": (1.0, 2.0, 3.0),
        "sl_buffer_atr": 0.1,
        "sl_min_atr": 0.5,
        "sl_max_atr": 3.0,
        "cost_multiple": 2.0,
        "plan_id": "2026-11-17-INT-001",
        "session": "london",
    }
    plan = anchored_plan(candidate(), costs, **options).plan
    assert plan is not None
    assert (plan.stop_loss, plan.take_profit_1, plan.take_profit_2, plan.take_profit_3) == (
        pytest.approx(2026.8),
        pytest.approx(2033.2),
        pytest.approx(2036.4),
        pytest.approx(2039.6),
    )
    assert (plan.close_by, plan.expires_at, plan.entry_mode) == (
        utc(2026, 11, 17, 21, 45),
        SIGNAL + MINUTE,
        "market",
    )
    short = anchored_plan(
        candidate(direction=Direction.SHORT, anchor=2033.0), costs, **options
    ).plan
    assert short is not None and short.stop_loss == pytest.approx(2033.4)  # + the spread
    assert anchored_plan(candidate(anchor=2029.9), costs, **options).reason == (
        "no stop within 0.5-3 ATR"
    )
    assert anchored_plan(candidate(anchor=2031.0), costs, **options).reason == (
        "the stop is on the wrong side"
    )
    tight = {**options, "sl_min_atr": 0.0}
    assert anchored_plan(candidate(anchor=2029.7), costs, **tight).reason == (
        "TP1 does not clear the costs"
    )


def test_each_mode_manages_its_own_way(settings: Settings) -> None:
    scalper = settings.config.scalper
    hft = mode_management(HFT, scalper, 0.1)
    intraday = mode_management(INTRADAY, scalper, 0.1)
    swing = mode_management(SWING, scalper, 0.1)
    assert hft.time_stop == dt.timedelta(minutes=15) and hft.step_trail
    assert (intraday.time_stop, intraday.step_trail, intraday.swap) == (None, False, False)
    assert intraday.stop_to_tp1_after_tp2 and intraday.breakeven_offset == 0.1
    assert (swing.time_stop, swing.step_trail, swing.swap) == (None, True, True)
    with pytest.raises(ValueError, match="unknown scalper mode"):
        mode_management("tick", scalper, 0.1)


def test_flat_by_times(settings: Settings, sessions: SessionCalculator) -> None:
    # The New York rollover block starts at 16:45 New York: 21:45 UTC in November.
    assert next_rollover(sessions, utc(2026, 11, 17, 8, 0)) == utc(2026, 11, 17, 21, 45)
    assert next_rollover(sessions, utc(2026, 11, 17, 22, 0)) == utc(2026, 11, 18, 21, 45)
    weekend = settings.config.sessions.weekend
    hour = dt.timedelta(hours=1)
    # The Friday close is 17:00 New York (22:00 UTC); an hour before it, 21:00 UTC.
    assert before_weekend(weekend, utc(2026, 11, 18, 12), hour) == utc(2026, 11, 20, 21)
    assert before_weekend(weekend, utc(2026, 11, 20, 21, 30), hour) == utc(2026, 11, 27, 21)
    swing = make_logic(SWING, settings.config, sessions, 0.1)
    # Two days at most, and never into the weekend.
    assert swing._close_by(utc(2026, 11, 18, 12)) == utc(2026, 11, 20, 12)  # noqa: SLF001
    assert swing._close_by(utc(2026, 11, 19, 20)) == utc(2026, 11, 20, 21)  # noqa: SLF001
    late = candidate(time=utc(2026, 11, 20, 8), close_by=utc(2026, 11, 20, 8, 30))
    assert swing.gate(late).reason == "too close to the weekend"


def test_the_intraday_gate_follows_the_h1_trend(
    settings: Settings, sessions: SessionCalculator
) -> None:
    intraday = make_logic(INTRADAY, settings.config, sessions, 0.1)
    assert intraday.gate(candidate()) == Gate("london")  # no H1 trend yet: allowed
    feed_hours(intraday.trend, utc(2026, 11, 14, 0), [2000.0 - h for h in range(60)])
    assert intraday.trend.direction is Direction.SHORT
    assert intraday.gate(candidate()).reason == "against the H1 trend"
    assert intraday.gate(candidate(direction=Direction.SHORT, anchor=2033.0)).passed
    assert intraday.gate(candidate(time=utc(2026, 11, 17, 5, 0))).reason == (
        "outside the active sessions"
    )
    near = candidate(direction=Direction.SHORT, close_by=SIGNAL + 2 * MINUTE)
    assert intraday.gate(near).reason == "too close to the rollover"


def test_each_mode_has_its_own_magic_number(tmp_path: Path) -> None:
    config = load(write_text(tmp_path, "")).config
    assert [config.scalper.magic_for(m) for m in (HFT, INTRADAY, SWING)] == [
        7341927,
        7341928,
        7341929,
    ]
    with pytest.raises(ConfigError, match="one per mode"):
        load(write_text(tmp_path, "execution:\n  magic_number: 7341929\n"))


# --------------------------------------------------------------------------- #
# The modes in the bot (paper and the demo account)
# --------------------------------------------------------------------------- #


class Scripted:
    """A mode whose candidates are given; its gate, plan and rules are the real ones."""

    def __init__(self, real: ModeLogic, candidates: Sequence[Candidate]) -> None:
        self.real = real
        self.name, self.prefix, self.management = real.name, real.prefix, real.management
        self._at = {c.time - MINUTE: [c] for c in candidates}

    @property
    def line(self) -> Any:
        return self.real.line

    def on_bar(self, opened: dt.datetime, *values: float) -> list[Candidate]:
        self.real.on_bar(opened, *values)
        return self._at.get(opened, [])

    def gate(self, c: Candidate) -> Gate:
        return self.real.gate(c)

    def plan(self, c: Candidate, costs: LadderCosts, **options: Any) -> Any:
        return self.real.plan(c, costs, **options)


def script(h: Harness, mode: str, *candidates: Candidate) -> None:
    slot = h.trader.scalper.slots[mode]
    slot.logic = Scripted(slot.logic, candidates)  # type: ignore[assignment]


def test_an_intraday_scalp_on_paper_is_flat_by_its_time(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    flat_by = utc(2026, 11, 17, 8, 30)
    script(h, INTRADAY, candidate(anchor=2028.3, close_by=flat_by, reason="M5: test sweep"))
    h.say("mode", "both")
    assert h.say("scalper", "intraday") == (
        "✅ Scalper mode: intraday (M5, flat by the New York close): on paper."
    )
    h.run_until(utc(2026, 11, 17, 8, 40))
    placed, closed = h.notes.sent
    # The stop 0.1 ATR beyond the anchor; targets 1, 2 and 3 R away (D112).
    assert placed == (
        "⚡ Scalp INTRADAY LONG 2026-11-17-INT-001 (paper, 0.13 lots): market | SL 2028.10 | "
        "TP 2031.90 / 2033.80 / 2035.70\nM5: test sweep"
    )
    # TP1 on the rise; the rest closed at its flat-by time.
    assert closed.startswith(
        "🏁 Scalp INTRADAY 2026-11-17-INT-001 closed (session_end, paper) after 30m: +$"
    )
    (row,) = db.query("SELECT id, exit_reason, legs FROM scalp_trades")
    assert row[:2] == ("2026-11-17-INT-001", "session_end")
    assert [leg["reason"] for leg in json.loads(row[2])] == ["tp1", "session_end", "session_end"]
    assert h.trader.scalper.slots[INTRADAY].paper.open_positions == []


def test_a_swing_scalp_on_paper(settings: Settings, db: Database) -> None:
    h = Harness(settings, db)
    real = h.trader.scalper.slots[SWING].logic
    close_by = real._close_by(SIGNAL)  # noqa: SLF001
    script(h, SWING, candidate(close_by=close_by, rule="ema_pullback", reason="M15: test"))
    h.say("mode", "scalper")
    h.say("scalper", "swing")
    h.run_until(utc(2026, 11, 17, 8, 10))
    (placed,) = h.notes.sent
    assert placed.startswith("⚡ Scalp SWING LONG 2026-11-17-SWS-001 (paper, 0.07 lots): market")
    assert "SL 2026.80 | TP 2033.20 / 2036.40 / 2039.60" in placed
    (position,) = h.trader.scalper.slots[SWING].paper.open_positions
    assert position["id"] == "2026-11-17-SWS-001"
    assert "Scalp open (swing, paper): LONG 2026-11-17-SWS-001" in h.trader.status()
    assert close_by == utc(2026, 11, 19, 8, 0)  # two days at most


def test_a_mode_switched_off_still_manages_its_open_scalp(settings: Settings, db: Database) -> None:
    from tests.test_scalper_runner import SIGNAL_BAR, setup_at

    h = Harness(settings, db, [setup_at(SIGNAL_BAR)])
    h.scalper_on()
    h.run_until(utc(2026, 11, 17, 8, 1))  # the HFT scalp is open
    h.say("scalper", "intraday")  # another mode selected: HFT opens nothing new...
    h.run_until(utc(2026, 11, 17, 8, 10))
    assert "closed (tp5, paper)" in h.notes.sent[-1]  # ...but its scalp ran to the end


def test_an_approved_intraday_mode_trades_on_the_demo_account_with_its_own_magic(
    settings: Settings, db: Database
) -> None:
    demo, h = demo_harness(settings, db)
    script(h, INTRADAY, candidate(anchor=2028.3, reason="M5: test sweep"))
    h.say("mode", "both")
    h.say("scalper", "intraday")
    assert h.say("approve", "scalper intraday").startswith(
        "✅ The scalper's intraday mode sends its orders to the demo account"
    )
    h.run_until(utc(2026, 11, 17, 8, 0))
    (placed,) = h.notes.sent
    assert placed.startswith("⚡ Scalp INTRADAY LONG 2026-11-17-INT-001 (demo account")
    positions = sorted(demo.fake.open_positions.values(), key=lambda p: p.ticket)
    assert {p.magic for p in positions} == {settings.config.scalper.magic_for(INTRADAY)}
    assert [p.comment for p in positions][0] == "GS-SI|2026-11-17-INT-001|1"
    assert all(p.sl and p.tp for p in positions)
    assert {round(p.tp, 2) for p in positions} == {2031.9, 2033.8, 2035.7}
    # The HFT executor knows nothing of them; the kill switch closes them all the same.
    assert demo.scalps[HFT].open_positions == []
    assert "1 scalp(s) closed" in h.say("stop")
    assert demo.fake.open_positions == {}


# --------------------------------------------------------------------------- #
# The mode backtest
# --------------------------------------------------------------------------- #

WEEK = generate_m1_candles(utc(2026, 11, 9), utc(2026, 11, 21), seed=11, start_price=2650.0)


@pytest.mark.parametrize("mode", [INTRADAY, SWING, HFT])
def test_the_mode_backtest_replays_the_live_logic(settings: Settings, mode: str) -> None:
    spec = default_gold_spec()
    start, end = utc(2026, 11, 16), utc(2026, 11, 21)
    run = mode_backtest.simulate(settings, WEEK, spec, mode, start, end)
    again = mode_backtest.simulate(settings, WEEK, spec, mode, start, end)
    assert [t.net_pnl for t in run.trades] == [t.net_pnl for t in again.trades]  # repeatable
    assert run.outcomes["traded"] == len(run.trades)  # every trade finished (end of data)
    assert all(start <= t.plan.time < end for t in run.trades)
    assert all(
        t.plan.id.startswith(
            make_logic(
                mode, settings.config, SessionCalculator(settings.config.sessions), 0.0
            ).prefix
        )
        for t in run.trades
    )
    if mode == INTRADAY:
        assert all(t.plan.close_by is not None for t in run.trades)
        assert all(t.exit_time <= t.plan.close_by + MINUTE for t in run.trades)
    gross = mode_backtest.simulate(settings, WEEK, spec, mode, start, end, costs=False)
    assert sum(t.commission for t in gross.trades) == 0


def test_the_mode_backtest_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    spec = default_gold_spec()
    with Database(tmp_path / "data" / "goldsweep.sqlite3") as db:
        save_symbol_spec(db, spec)
        db.upsert_candles(spec.name, Timeframe.M1, WEEK)
    args = ["--mode", "intraday", "--from", "2026-11-16", "--to", "2026-11-20"]
    args += ["--config", str(DEFAULT_CONFIG_PATH)]
    out = io.StringIO()
    assert mode_backtest.main([*args, "--out", str(tmp_path / "a")], stdout=out) == 0
    text = out.getvalue()
    assert "GoldSweep scalper intraday mode XAUUSD 2026-11-16 .. 2026-11-20" in text
    assert "Reported, not a gate (D111)" in text and "Every scalp can lose" in text
    assert mode_backtest.main([*args, "--out", str(tmp_path / "b")], stdout=io.StringIO()) == 0
    first = json.loads((tmp_path / "a" / "summary.json").read_text(encoding="utf-8"))
    second = json.loads((tmp_path / "b" / "summary.json").read_text(encoding="utf-8"))
    assert first.pop("command") != second.pop("command")
    assert first == second  # reproducible
    assert first["mode"] == "intraday" and "news blackout" in first["limitations"][0]
    assert (tmp_path / "a" / "trades.csv").read_text(encoding="utf-8") == (
        tmp_path / "b" / "trades.csv"
    ).read_text(encoding="utf-8")

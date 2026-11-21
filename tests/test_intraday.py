"""The scalper's intraday mode (S3, D109): rules, plans, the flat-by rule, the backtest.

Hand-made M5 bars on Tuesday 2026-11-17 (winter: London = UTC, New York = UTC-5):
Asia 00:00-06:00 UTC, the London window 07:00-10:00, the New York open 14:30, the
rollover from 21:45. ATR periods of 3 keep the fixtures short.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from goldsweep.analysis.structure import Direction
from goldsweep.backtest.data import save_symbol_spec
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, IntradayConfig, Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import (
    Bar,
    EventKind,
    ExitReason,
    Management,
    TradeBook,
    TradePlan,
    close_by_due,
    to_ns,
)
from goldsweep.scalping.intraday import (
    ASIAN_BREAKOUT,
    OPENING_RANGE,
    VWAP_TREND,
    IntradayEngine,
    IntradaySetup,
    intraday_management,
    intraday_plan,
)
from goldsweep.scalping.intraday_backtest import judge, main, prepare, simulate
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.storage.db import Database
from tests.helpers import utc

FIVE = dt.timedelta(minutes=5)
DAY = utc(2026, 11, 17)
ROLLOVER = utc(2026, 11, 17, 21, 45)
Row = tuple[float, float, float, float]


def small(settings: Settings) -> Settings:
    scalper = settings.config.scalper.model_copy(update={"atr_period": 3})
    return dataclasses.replace(
        settings, config=settings.config.model_copy(update={"scalper": scalper})
    )


def engine(settings: Settings, rule: str) -> IntradayEngine:
    config = small(settings).config
    return IntradayEngine(
        config.scalper, SessionCalculator(config.sessions), bar=FIVE, rules=(rule,)
    )


def feed(machine: IntradayEngine, start: dt.datetime, rows: list[Row]) -> list[IntradaySetup]:
    found: list[IntradaySetup] = []
    for i, row in enumerate(rows):
        found += machine.on_bar(start + i * FIVE, *row, volume=100.0)
    return found


#: Asia 00:00-06:00: a 2000-2004 range; 06:00-07:00 quiet inside it.
ASIA: list[Row] = [(2001.0, 2004.0, 2000.0, 2003.0), (2003.0, 2004.0, 2000.0, 2001.0)] * 36
QUIET: list[Row] = [(2002.0, 2002.5, 2001.5, 2002.0)] * 12
BREAKOUT: Row = (2002.0, 2006.0, 2001.8, 2005.5)  # 07:00 closes above 2004 + 0.1 ATR


def test_the_asian_range_breakout_in_the_london_window(settings: Settings) -> None:
    (setup,) = feed(engine(settings, ASIAN_BREAKOUT), DAY, [*ASIA, *QUIET, BREAKOUT, BREAKOUT])
    assert (setup.rule, setup.direction, setup.session) == (
        ASIAN_BREAKOUT,
        Direction.LONG,
        "london",
    )
    assert setup.time == utc(2026, 11, 17, 7, 5) and setup.reference == 2005.5
    assert setup.anchor == 2002.0  # the range's middle
    assert setup.close_by == ROLLOVER  # flat by the New York rollover (16:45 New York)
    assert setup.detail == (
        "closed above the Asian range 2000.00-2004.00; stop beyond its middle 2002.00"
    )


def test_no_asian_breakout_without_a_range_or_outside_london(settings: Settings) -> None:
    assert feed(engine(settings, ASIAN_BREAKOUT), DAY, [*ASIA[:10], *QUIET]) == []  # a gap
    late = DAY + dt.timedelta(hours=6) + 72 * FIVE  # 12:00: after the London window
    machine = engine(settings, ASIAN_BREAKOUT)
    feed(machine, DAY, ASIA)
    assert feed(machine, late, [BREAKOUT]) == []


def test_the_new_york_opening_range_breakout(settings: Settings) -> None:
    before = [(2008.0, 2009.0, 2007.0, 2008.0)] * 6  # 14:00-14:25
    opening = [(2008.0, 2010.0, 2006.0, 2009.0)] * 3  # 14:30-14:40: range 2006-2010
    up = (2009.0, 2011.5, 2008.8, 2011.0)  # 14:45 closes above 2010 + 0.1 ATR
    machine = engine(settings, OPENING_RANGE)
    (setup,) = feed(machine, utc(2026, 11, 17, 14), [*before, *opening, up, up])
    assert (setup.direction, setup.session, setup.anchor) == (Direction.LONG, "newyork", 2008.0)
    assert setup.time == utc(2026, 11, 17, 14, 50) and setup.close_by == ROLLOVER
    late = engine(settings, OPENING_RANGE)  # breakouts count until 12:00 New York only
    feed(late, utc(2026, 11, 17, 14), [*before, *opening])
    assert feed(late, utc(2026, 11, 17, 17, 5), [up]) == []


def vwap_morning() -> list[Row]:
    rows: list[Row] = [(2000.0, 2000.6, 1999.4, 2000.0)] * 6  # 06:30-06:55, before London
    price = 2000.0
    for _ in range(10):  # 07:00-07:45: a steady rise, the VWAP rising behind it
        rows.append((price, price + 1.3, price - 0.2, price + 1.0))
        price += 1.0
    rows.append((2010.0, 2010.2, 2005.8, 2006.0))  # 07:50 a dip
    rows.append((2006.0, 2006.2, 2004.6, 2004.8))  # 07:55 closes below the VWAP (2005.5)
    rows.append((2004.8, 2009.4, 2004.7, 2009.0))  # 08:00 back above, the VWAP still rising
    return rows


def test_a_close_back_above_a_rising_vwap(settings: Settings) -> None:
    machine = engine(settings, VWAP_TREND)
    (setup,) = feed(machine, utc(2026, 11, 17, 6, 30), vwap_morning())
    assert (setup.rule, setup.direction, setup.session) == (VWAP_TREND, Direction.LONG, "london")
    assert setup.time == utc(2026, 11, 17, 8, 5) and setup.reference == 2009.0
    assert setup.anchor == 2004.6  # the lowest low of the six bars before
    assert setup.detail.startswith("closed above the VWAP 2005.")
    assert 2005.0 < machine.vwap < 2006.0


def test_unknown_rules_are_refused(settings: Settings) -> None:
    config = settings.config
    with pytest.raises(ValueError, match="unknown intraday rules"):
        IntradayEngine(config.scalper, SessionCalculator(config.sessions), bar=FIVE, rules=("orb",))


# -- the plan and the management ----------------------------------------------------- #


def setup_of(direction: Direction = Direction.LONG, anchor: float = 2002.0) -> IntradaySetup:
    return IntradaySetup(
        index=0,
        time=utc(2026, 11, 17, 7, 5),
        direction=direction,
        rule=ASIAN_BREAKOUT,
        reference=2005.5 if direction is Direction.LONG else 1998.5,
        anchor=anchor,
        atr=2.0,
        session="london",
        close_by=ROLLOVER,
        detail="",
    )


def test_the_intraday_plan() -> None:
    config = IntradayConfig()
    costs = LadderCosts(spread=0.24, slippage=0.01, commission=0.0)
    made = intraday_plan(
        setup_of(), config, (1.0, 2.0, 3.0), costs, cost_multiple=2.0, plan_id="ID-1",
        bar=dt.timedelta(minutes=1),
    )  # fmt: skip
    plan = made.plan
    assert plan is not None
    assert plan.stop_loss == pytest.approx(2001.8)  # the middle less 0.1 ATR
    assert [plan.take_profit_1, plan.take_profit_2, plan.take_profit_3] == pytest.approx(
        [2009.2, 2012.9, 2016.6]
    )
    assert plan.close_by == ROLLOVER and plan.session == "london" and plan.entry_mode == "market"
    short = intraday_plan(
        setup_of(Direction.SHORT, anchor=2002.0), config, (1.0,), costs, cost_multiple=2.0,
        plan_id="ID-2", bar=dt.timedelta(minutes=1),
    ).plan  # fmt: skip
    assert short is not None and short.stop_loss == pytest.approx(2002.44)  # + the spread
    assert short.take_profit_2 is None
    far = intraday_plan(
        setup_of(anchor=1990.0), config, (1.0,), costs, cost_multiple=2.0, plan_id="ID-3",
        bar=dt.timedelta(minutes=1),
    )  # fmt: skip
    assert far.plan is None and far.reason.startswith("stop 7.85 ATR away")
    dear = intraday_plan(
        setup_of(), config, (0.1,), costs, cost_multiple=2.0, plan_id="ID-4",
        bar=dt.timedelta(minutes=1),
    )  # fmt: skip
    assert dear.plan is None and dear.reason == "TP1 does not clear the costs"


def test_the_intraday_management(settings: Settings) -> None:
    rules = intraday_management(settings.config.scalper, 0.01, trail=True)
    assert (rules.split, rules.breakeven_offset, rules.stop_to_tp1_after_tp2) == (
        (0.4, 0.3, 0.3),
        0.01,
        True,
    )
    assert rules.step_trail and rules.time_stop is None
    assert not intraday_management(settings.config.scalper, 0.01, trail=False).step_trail


def test_a_position_is_flat_by_its_close_by_time(settings: Settings) -> None:
    config = settings.config
    book = TradeBook(
        default_gold_spec(),
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=10_000.0,
        slippage_points=0.0,
        commission_per_lot=0.0,
        trail_after_tp1=False,
        management=Management(),
    )
    start = utc(2026, 11, 17, 21, 40)
    plan = TradePlan(
        id="ID-1", time=start, direction=Direction.LONG, session="newyork",
        entry_mode="market", entry=2005.0, stop_loss=2001.0, take_profit_1=2013.0,
        take_profit_2=None, expires_at=start + dt.timedelta(minutes=1),
        close_by=utc(2026, 11, 17, 21, 45),
    )  # fmt: skip
    assert TradePlan.from_state(plan.to_state()) == plan
    assert not close_by_due(plan, utc(2026, 11, 17, 21, 44))
    assert close_by_due(plan, ROLLOVER)
    book.submit(plan, spread_points=0.0, median_spread_points=None)
    events = []
    for minute in range(5):
        opened = to_ns(start + dt.timedelta(minutes=minute))
        bar = Bar(opened, opened + 60 * 10**9, 2005.0, 2005.6, 2004.6, 2005.3, 0.0)
        events += book.on_bar(bar)
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    assert closed.trade is not None and closed.trade.exit_reason is ExitReason.SESSION_END
    assert closed.trade.exit_time == ROLLOVER and closed.price == 2005.3


# -- the backtest ------------------------------------------------------------------- #


def m1_of(rows: list[Row], start: dt.datetime) -> pd.DataFrame:
    """One-minute bars whose five-minute buckets are ``rows`` (the move in the first minute)."""
    minutes: list[Row] = []
    for open_, high, low, close in rows:
        minutes.append((open_, high, low, close))
        minutes += [(close, close, close, close)] * 4
    index = pd.date_range(start, periods=len(minutes), freq="1min", name="time")
    frame = pd.DataFrame(minutes, columns=["open", "high", "low", "close"], index=index)
    frame["tick_volume"] = 100
    frame["spread"] = 20
    return frame


RISE: list[Row] = [(2005.5 + i, 2007.0 + i, 2005.0 + i, 2006.5 + i) for i in range(12)]
MARKET = m1_of([*ASIA, *QUIET, BREAKOUT, *RISE, *[(2017.5, 2017.6, 2017.4, 2017.5)] * 40], DAY)


def test_the_breakout_backtests_as_one_winning_trade(settings: Settings) -> None:
    tuned = small(settings)
    data = prepare(tuned, MARKET, default_gold_spec(), DAY, DAY + dt.timedelta(days=1))
    assert [(s.rule, s.time) for s in data.setups if s.rule == ASIAN_BREAKOUT] == [
        (ASIAN_BREAKOUT, utc(2026, 11, 17, 7, 5))
    ]
    run = simulate(tuned, data, ASIAN_BREAKOUT, (1.0, 2.0, 3.0), costs=True)
    (trade,) = run.trades
    # The next open (2005.50) plus the spread (0.20) and the slippage (0.10).
    assert trade.fill_time == utc(2026, 11, 17, 7, 5)
    assert trade.fill_price == pytest.approx(2005.8)
    assert [leg.reason for leg in trade.legs] == [ExitReason.TP1, ExitReason.TP2, ExitReason.TP3]
    assert trade.r_multiple > 0 and run.outcomes["traded"] == 1
    verdict, runs = judge(tuned, data, ASIAN_BREAKOUT)
    assert len(runs) == 2 and not verdict.passed  # one trade: far from the gate
    assert "fewer than 100" in verdict.reason and verdict.gross.trades[0].commission == 0


def test_the_command_writes_its_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    data["scalper"]["atr_period"] = 3
    data["backtest"]["warmup_days"] = 3
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    spec = default_gold_spec()
    with Database(tmp_path / "data" / "goldsweep.sqlite3") as db:
        save_symbol_spec(db, spec)
        db.upsert_candles(spec.name, Timeframe.M1, MARKET)
    out = io.StringIO()
    args = ["--from", "2026-11-17", "--to", "2026-11-17", "--config", str(config)]
    assert main([*args, "--out", str(tmp_path / "report")], stdout=out) == 0
    text = out.getvalue()
    assert "asian_breakout: failed (1 trades, fewer than 100" in text
    summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert [v["rule"] for v in summary["verdicts"]] == [ASIAN_BREAKOUT, OPENING_RANGE, VWAP_TREND]
    assert not any(v["passed"] for v in summary["verdicts"])
    rows = (tmp_path / "report" / "trades.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("rule,id,direction") and rows[1].startswith("asian_breakout,ID-")

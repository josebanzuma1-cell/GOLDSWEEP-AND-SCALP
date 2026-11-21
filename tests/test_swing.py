"""The scalper's swing mode (S4, D110): swap, rules, plans and the backtest."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from goldsweep.analysis.structure import Direction
from goldsweep.backtest.data import SPEC_STATE_KEY, load_symbol_spec, save_symbol_spec
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import Settings, SwingConfig
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import BrokerError
from goldsweep.data.mock_broker import MockBroker, default_gold_spec
from goldsweep.execution.book import (
    Bar,
    EventKind,
    Management,
    TradeBook,
    TradePlan,
    swap_nights,
    to_ns,
)
from goldsweep.scalping.plan import LadderCosts, scalp_risk
from goldsweep.scalping.swing import (
    DONCHIAN,
    MOMENTUM,
    PULLBACK,
    SwingEngine,
    SwingSetup,
    swing_management,
    swing_plan,
)
from goldsweep.scalping.swing_backtest import (
    H4_FETCHED_KEY,
    daily_bars,
    judge,
    main,
    prepare,
    simulate,
    stored_h4,
)
from goldsweep.storage.db import Database
from tests.helpers import utc

#: XAUUSDm as the broker reports it (2026-11-22): longs pay 431.6 points a lot a night.
GOLD = default_gold_spec(
    "XAUUSDm",
    digits=3,
    point=0.001,
    tick_size=0.001,
    tick_value=0.1,
    swap_mode=1,
    swap_long=-431.6,
    swap_short=0.0,
    swap_rollover3days=3,
)


def test_the_swap_in_account_currency() -> None:
    assert GOLD.swap_per_lot(True) == pytest.approx(-43.16)  # 431.6 points x 0.10 a point
    assert GOLD.swap_per_lot(False) == 0.0
    in_money = dataclasses.replace(GOLD, swap_mode=4, swap_long=-12.5)
    assert in_money.swap_per_lot(True) == -12.5
    assert dataclasses.replace(GOLD, swap_mode=0).swap_per_lot(True) == 0.0
    assert dataclasses.replace(GOLD, swap_mode=5).swap_per_lot(True) is None  # not modelled


def test_nights_charged_seven_a_week() -> None:
    monday, wednesday = utc(2026, 11, 16, 10), utc(2026, 11, 18, 20)
    assert swap_nights(monday, monday + dt.timedelta(hours=10), 3) == 0  # same day
    assert swap_nights(monday, utc(2026, 11, 17, 10), 3) == 1
    assert swap_nights(wednesday, utc(2026, 11, 19, 4), 3) == 3  # Wednesday's triple
    assert swap_nights(utc(2026, 11, 20, 20), utc(2026, 11, 23, 4), 3) == 1  # Friday night only
    assert swap_nights(monday, utc(2026, 11, 23, 10), 3) == 7  # a whole week


def test_the_book_charges_swap_for_each_night_held(settings: Settings) -> None:
    config = settings.config
    book = TradeBook(
        GOLD,
        scalp_risk(config),
        config.news,
        config.strategy.liquidity.day_boundary,
        equity=10_000.0,
        slippage_points=0.0,
        commission_per_lot=0.0,
        trail_after_tp1=False,
        management=Management(swap=True),
    )
    start = utc(2026, 11, 16, 0)
    plan = TradePlan(
        id="SW-1", time=start, direction=Direction.LONG, session=None, entry_mode="market",
        entry=2000.0, stop_loss=1990.0, take_profit_1=2100.0, take_profit_2=None,
        expires_at=start + dt.timedelta(hours=4),
    )  # fmt: skip
    placed = book.submit(plan, spread_points=0.0, median_spread_points=None)
    assert placed.lots == 0.02  # 25.00 / (10 x 100 a lot) = 0.025, rounded down
    events = []
    for k in range(18):  # Monday 00:00 to Thursday 00:00 in H4 bars
        opened = to_ns(start + dt.timedelta(hours=4 * k))
        events += book.on_bar(Bar(opened, opened + 4 * 3600 * 10**9, 2000, 2001, 1999, 2000, 0))
    events += book.close_everything(utc(2026, 11, 19, 0), 2000.0, 2000.0, _manual(), "test")
    (closed,) = [e for e in events if e.kind is EventKind.CLOSED]
    trade = closed.trade
    assert trade is not None
    # Monday, Tuesday and Wednesday nights (the third one triple): 5 nights x 0.02 lots.
    assert trade.swap == pytest.approx(-43.16 * 0.02 * 5)
    assert trade.net_pnl == pytest.approx(trade.swap)  # flat price: the swap is the result


def _manual():  # type: ignore[no-untyped-def]
    from goldsweep.execution.book import ExitReason

    return ExitReason.MANUAL


# -- the rules ---------------------------------------------------------------------- #


def swing_settings(settings: Settings, **changes: object) -> Settings:
    swing = settings.config.scalper.swing.model_copy(update=changes)
    scalper = settings.config.scalper.model_copy(update={"swing": swing})
    return dataclasses.replace(
        settings, config=settings.config.model_copy(update={"scalper": scalper})
    )


def run(settings: Settings, rule: str, closes: list[float], start: dt.datetime) -> list:
    engine = SwingEngine(settings.config.scalper, rules=(rule,))
    found = []
    day = start
    for close in closes:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        found += engine.on_day(day, close, close + 1.0, close - 1.0, close)
        day += dt.timedelta(days=1)
    return found


def test_a_donchian_breakout(settings: Settings) -> None:
    tuned = swing_settings(settings, breakout_days=5)
    flat = [2000.0] * 25
    (setup,) = run(tuned, DONCHIAN, [*flat, 2003.0], utc(2026, 1, 5))
    assert (setup.rule, setup.direction, setup.reference) == (DONCHIAN, Direction.LONG, 2003.0)
    assert setup.detail == "closed above the 5-day high 2001.00" and setup.close_by is None
    (short,) = run(tuned, DONCHIAN, [*flat, 1997.0], utc(2026, 1, 5))
    assert short.direction is Direction.SHORT


def test_momentum_decides_once_a_month(settings: Settings) -> None:
    tuned = swing_settings(settings, momentum_months=2)
    closes = [2000.0 + day for day in range(90)]  # a steady rise from January
    setups = run(tuned, MOMENTUM, closes, utc(2026, 1, 1))
    # Three month-ends (January to March) are needed first; then each month's first day.
    assert [s.time.date() for s in setups] == [dt.date(2026, 4, 2), dt.date(2026, 5, 2)]
    first = setups[0]
    assert first.direction is Direction.LONG and first.close_by == utc(2026, 5, 1)
    assert first.detail.startswith("2-month return +")


def test_a_pullback_in_an_uptrend(settings: Settings) -> None:
    # The trend average must be longer than the 50-day one, as the 200-day is.
    tuned = swing_settings(settings, trend_days=60, pullback_days=5)
    rise = [2000.0 + 2 * day for day in range(70)]
    top = rise[-1]
    dip = [top - 6.0, top - 9.0]  # back under the 5-day average
    found = run(tuned, PULLBACK, [*rise, *dip, top + 2.0], utc(2026, 1, 5))
    assert [(s.rule, s.direction) for s in found] == [(PULLBACK, Direction.LONG)]
    assert found[0].reference == top + 2.0


def test_unknown_rules_are_refused(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown swing rules"):
        SwingEngine(settings.config.scalper, rules=("carry",))


def test_the_swing_plan() -> None:
    setup = SwingSetup(0, utc(2026, 1, 6), Direction.SHORT, MOMENTUM, 2000.0, 20.0,
                       utc(2026, 2, 1), "")  # fmt: skip
    costs = LadderCosts(spread=0.24, slippage=0.01, commission=0.0)
    made = swing_plan(
        setup, SwingConfig(), (2.0, 4.0, 6.0), costs, cost_multiple=2.0, plan_id="SW-1",
        bar=dt.timedelta(hours=4),
    )  # fmt: skip
    plan = made.plan
    assert plan is not None and plan.stop_loss == pytest.approx(2040.24)  # 2 ATR + the spread
    assert plan.take_profit_1 == pytest.approx(2000.0 - 2 * 40.24)
    assert plan.close_by == utc(2026, 2, 1) and plan.session is None
    rules = swing_management(default_settings().config.scalper, 0.01, trail=True)
    assert rules.swap and rules.step_trail and rules.time_stop is None


def default_settings() -> Settings:
    from goldsweep.core.settings import DEFAULT_CONFIG_PATH, load_settings

    return load_settings(DEFAULT_CONFIG_PATH, read_env_file=False)


# -- the backtest ------------------------------------------------------------------- #


def trend_h4(start: dt.datetime, days: int, *, seed: int = 3) -> pd.DataFrame:
    """H4 bars of a rising market with noise (weekdays only)."""
    rng = np.random.default_rng(seed)
    rows, index = [], []
    price = 2000.0
    moment = start
    while len(index) < days * 6:
        if moment.weekday() < 5:
            open_ = price
            close = open_ + 0.6 + float(rng.normal(0, 3.0))
            high = max(open_, close) + float(rng.uniform(0, 2))
            low = min(open_, close) - float(rng.uniform(0, 2))
            rows.append((open_, high, low, close))
            index.append(moment)
            price = close
        moment += dt.timedelta(hours=4)
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                         index=pd.DatetimeIndex(index, name="time"))  # fmt: skip
    frame["tick_volume"] = 100
    frame["spread"] = 240
    return frame


H4 = trend_h4(utc(2024, 1, 1), 520)


def test_a_rising_market_backtests_with_swap_paid(settings: Settings) -> None:
    data = prepare(settings, H4, GOLD, utc(2025, 1, 1), utc(2026, 1, 1))
    assert {s.rule for s in data.setups} == {DONCHIAN, MOMENTUM, PULLBACK}
    assert not math.isnan(data.channel_low[-1])
    run_ = simulate(settings, data, DONCHIAN, (2.0, 4.0, 6.0), costs=True)
    # Every placed order fills and finishes, but one placed on the last bar never fills.
    assert 0 < run_.metrics.trades <= run_.outcomes["traded"] <= run_.metrics.trades + 1
    assert run_.swap < 0  # longs pay the swap
    free = simulate(settings, data, DONCHIAN, (2.0, 4.0, 6.0), costs=False)
    assert free.swap == 0.0
    verdict, runs = judge(settings, data, MOMENTUM)
    assert len(runs) == 2 and verdict.chosen in runs
    assert all(t.plan.close_by is not None for t in verdict.chosen.trades)
    assert len(daily_bars(H4)) == 520


def test_the_history_is_fetched_once(tmp_path: Path, settings: Settings) -> None:
    clock = ManualClock(utc(2026, 11, 20, 12))
    minutes = pd.date_range(utc(2026, 11, 16), utc(2026, 11, 20), freq="1min", name="time")
    m1 = pd.DataFrame(
        {"open": 2000.0, "high": 2000.5, "low": 1999.5, "close": 2000.0,
         "tick_volume": 10, "spread": 240},
        index=minutes,
    )  # fmt: skip
    broker = MockBroker(clock=clock, candles_m1=m1, symbols=[GOLD])
    with Database(tmp_path / "db.sqlite3") as db:
        with pytest.raises(BrokerError, match="run once without --offline"):
            stored_h4(settings, db, utc(2026, 11, 16), utc(2026, 11, 20), offline=True)
        h4, spec = stored_h4(
            settings, db, utc(2026, 11, 1), utc(2026, 11, 20), offline=False, broker=broker
        )
        assert len(h4) == 24 and spec.swap_long == -431.6
        assert "swap_mode" in db.get_state(SPEC_STATE_KEY)
        assert db.get_state(H4_FETCHED_KEY) == utc(2026, 11, 1).isoformat()
        again, _ = stored_h4(settings, db, utc(2026, 11, 1), utc(2026, 11, 20), offline=True)
        assert len(again) == 24  # covered: no broker needed


def test_the_command_writes_its_report(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    with Database(tmp_path / "data" / "goldsweep.sqlite3") as db:
        save_symbol_spec(db, GOLD)
        db.upsert_candles(GOLD.name, Timeframe.H4, H4)
        db.set_state(H4_FETCHED_KEY, utc(2023, 1, 1).isoformat())
        assert load_symbol_spec(db) == GOLD
    out = io.StringIO()
    args = ["--from", "2025-03-01", "--to", "2025-11-30", "--offline"]
    assert main([*args, "--out", str(tmp_path / "report")], stdout=out) == 0
    text = out.getvalue()
    assert "swap per lot per night: long -43.16, short +0.00" in text
    summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert [v["rule"] for v in summary["verdicts"]] == [DONCHIAN, MOMENTUM, PULLBACK]
    assert summary["swap_per_lot_per_night"]["long"] == pytest.approx(-43.16)

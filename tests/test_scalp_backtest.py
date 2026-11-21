"""The scalper's M1 backtest end to end on the hand-made SELL (S1, D95).

The setup of tests/test_scalp_engine.py at 07:39 on a Tuesday (inside the London
kill zone), then a fall through TP1, TP2 and TP3: the scalp is entered at the next
bar's open and its three children close at their own targets.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest
import yaml

from goldsweep.backtest.data import save_symbol_spec
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, StepTrailConfig
from goldsweep.core.types import Timeframe
from goldsweep.data.mock_broker import default_gold_spec
from goldsweep.execution.book import ExitReason
from goldsweep.scalping.backtest import Variant, main, prepare, simulate, variants
from goldsweep.storage.db import Database
from tests.helpers import bars, quiet, utc
from tests.test_scalp_engine import SELL_ROWS

START = utc(2026, 11, 17, 7, 30)
FALL = [
    (2005.2, 2005.4, 2004.5, 2004.7),  # 07:39 the entry at the open
    (2004.7, 2004.8, 2003.5, 2003.7),  # TP1
    (2003.7, 2003.8, 2002.0, 2002.2),  # TP2
    (2002.2, 2002.3, 2000.3, 2000.5),  # TP3
    *quiet(20, 2000.5),
]
MARKET = bars(SELL_ROWS + FALL, START, minutes=1)
SMALL = {
    "atr_period": 3,
    "step_trail": StepTrailConfig(atr_period=3, multiplier=1.0),
    "swing_bars": 1,
}


@pytest.fixture
def small(settings: Settings) -> Settings:
    scalper = settings.config.scalper.model_copy(update=SMALL)
    return dataclasses.replace(
        settings, config=settings.config.model_copy(update={"scalper": scalper})
    )


def test_the_textbook_sell_is_one_winning_scalp(small: Settings) -> None:
    start, end = utc(2026, 11, 17), utc(2026, 11, 18)
    prepared = prepare(small, MARKET, default_gold_spec(), start, end)
    assert [(s.mode, s.index) for s in prepared.setups] == [("sweep_reclaim", 8)]
    run = simulate(small, prepared, Variant("sweep_reclaim", True, 0.5))
    assert run.outcomes == {"traded": 1}
    (trade,) = run.trades
    assert trade.plan.session == "london" and trade.plan.direction.value == "SHORT"
    assert trade.fill_time == utc(2026, 11, 17, 7, 39) and trade.fill_price == pytest.approx(2005.1)
    # stop: the X 2007.80 + 0.1 ATR (0.18) + the spread (0.20); R = 2.98 from the plan's 2005.20
    assert trade.stop_loss == pytest.approx(2008.18)
    assert [leg.reason for leg in trade.legs] == [ExitReason.TP1, ExitReason.TP2, ExitReason.TP3]
    assert trade.r_multiple > 0 and run.metrics.win_rate == 1.0
    assert run.days == 1 and run.per_day == 1.0
    assert trade.commission == 0  # this account charges none (D108): the spread is the cost
    gross = simulate(small, prepared, Variant("sweep_reclaim", True, 0.5, costs=False))
    (free,) = gross.trades
    # No spread, slippage or commission: the fill is the open, the stop has no spread in it.
    assert (free.fill_price, free.stop_loss, free.commission) == (2005.2, pytest.approx(2007.98), 0)
    assert simulate(small, prepared, Variant("trail_rejection", True, 0.5)).trades == []


def test_the_gates_before_an_order(small: Settings) -> None:
    start, end = utc(2026, 11, 17), utc(2026, 11, 18)
    prepared = prepare(small, MARKET, default_gold_spec(), start, end)
    scalper = small.config.scalper.model_copy(update={"sessions": ("asia",)})
    asia_only = dataclasses.replace(
        small, config=small.config.model_copy(update={"scalper": scalper})
    )
    assert simulate(asia_only, prepared, Variant("sweep_reclaim", True, 0.5)).outcomes == {
        "outside_sessions": 1
    }
    later = prepare(small, MARKET, default_gold_spec(), utc(2026, 11, 17, 8), end)
    assert simulate(small, later, Variant("sweep_reclaim", True, 0.5)).trades == []
    assert [v.name for v in variants(small.config)][:2] == [
        "sweep_reclaim, H1 bias, 0.5 R steps",
        "sweep_reclaim, H1 bias, 2 R steps",
    ]
    assert len(variants(small.config)) == 8


def test_the_command_writes_a_reproducible_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    data["scalper"].update(
        {"atr_period": 3, "step_trail": {"atr_period": 3, "multiplier": 1.0}, "swing_bars": 1}
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    spec = default_gold_spec()
    with Database(tmp_path / "data" / "goldsweep.sqlite3") as db:
        save_symbol_spec(db, spec)
        db.upsert_candles(spec.name, Timeframe.M1, MARKET)
    args = ["--from", "2026-11-17", "--to", "2026-11-17", "--config", str(config)]
    out = io.StringIO()
    assert main([*args, "--out", str(tmp_path / "first")], stdout=out) == 0
    text = out.getvalue()
    assert "sweep_reclaim, H1 bias, 0.5 R steps" in text and "Every scalp can lose" in text
    assert main([*args, "--out", str(tmp_path / "second")], stdout=io.StringIO()) == 0
    trades = (tmp_path / "first" / "trades.csv").read_text(encoding="utf-8")
    assert trades == (tmp_path / "second" / "trades.csv").read_text(encoding="utf-8")
    summary = json.loads((tmp_path / "first" / "summary.json").read_text(encoding="utf-8"))
    again = json.loads((tmp_path / "second" / "summary.json").read_text(encoding="utf-8"))
    assert summary.pop("command").endswith("first") and again.pop("command").endswith("second")
    assert summary == again  # nothing but the command line (its --out) differs
    chosen = summary["configured"]
    assert chosen["variant"] == "sweep_reclaim, H1 bias, 0.5 R steps"
    assert chosen["breakdowns"]["session"]["london"]["trades"] == 1
    assert any("news blackout" in note for note in summary["limitations"])
    rows = trades.splitlines()
    assert len(rows) == 2 and rows[1].startswith("SC-00001,SHORT,london,swing,")


def test_the_command_without_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    out = io.StringIO()
    assert main(["--from", "2026-11-17", "--to", "2026-11-17"], stdout=out) == 1
    assert "No stored history" in out.getvalue()

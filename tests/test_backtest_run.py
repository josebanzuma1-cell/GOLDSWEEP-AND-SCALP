"""History download and storage, the full backtest pipeline, the report and the CLI."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from goldsweep.backtest.cli import main
from goldsweep.backtest.data import (
    days_without_bars,
    download_history,
    fingerprint,
    load_symbol_spec,
    month_chunks,
    save_symbol_spec,
    summarize_gaps,
)
from goldsweep.backtest.engine import MarketData, rolling_median, run_backtest, spread_points
from goldsweep.backtest.report import write_report
from goldsweep.backtest.simulator import ExitReason, Outcome
from goldsweep.core.clock import ManualClock
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, CostConfig, Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.mock_broker import MockBroker, default_gold_spec
from goldsweep.storage.db import Database
from tests.helpers import Row, bars, make_candles, quiet, utc
from tests.test_signals import MONDAY, with_raid

# After the Phase 2 textbook signal (long at 2028.90, stop 2026.09, TP1 2036, TP2 2048):
# a dip fills the entry, price reaches +1R, TP1 and then TP2.
FOLLOW_UP: list[Row] = [
    (2031.0, 2031.1, 2029.4, 2029.6),  # 07:55
    (2029.6, 2029.7, 2028.6, 2028.9),  # 08:00 ask low 2028.80: the limit fills
    (2028.9, 2031.0, 2028.8, 2030.8),
    (2030.8, 2033.0, 2030.6, 2032.8),  # beyond +1R: stop to breakeven
    (2032.8, 2036.3, 2032.6, 2036.0),  # TP1
    (2036.0, 2040.0, 2035.9, 2039.8),
    (2039.8, 2044.0, 2039.7, 2043.8),
    (2043.8, 2048.3, 2043.7, 2048.0),  # TP2
    *quiet(12, 2048.0, half_range=0.3),
]
TUESDAY = utc(2026, 11, 17)


@pytest.fixture
def market() -> MarketData:
    return MarketData(
        symbol="XAUUSD",
        spec=default_gold_spec(),
        trigger=bars(with_raid() + FOLLOW_UP, MONDAY),
        fine=None,
        source="hand-made fixture",
    )


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_month_chunks() -> None:
    assert month_chunks(utc(2024, 1, 15), utc(2024, 3, 2)) == [
        (utc(2024, 1, 15), utc(2024, 2, 1)),
        (utc(2024, 2, 1), utc(2024, 3, 1)),
        (utc(2024, 3, 1), utc(2024, 3, 2)),
    ]
    assert month_chunks(utc(2024, 12, 20), utc(2025, 1, 5))[0][1] == utc(2025, 1, 1)
    assert month_chunks(utc(2024, 1, 1), utc(2024, 1, 1)) == []


def test_download_stores_history_month_by_month(db: Database) -> None:
    now = utc(2026, 1, 20)
    with MockBroker(clock=ManualClock(now), history_days=25) as broker:
        messages: list[str] = []
        report = download_history(
            broker, db, "XAUUSD", Timeframe.M5, utc(2025, 11, 1), now, progress=messages.append
        )
        expected = broker.get_rates_range("XAUUSD", Timeframe.M5, utc(2025, 11, 1), now)
        assert report.rows == len(expected) > 0
        assert report.empty_months == ("2025-11",)  # before the broker's history begins
        assert "nothing returned for 2025-11" in report.describe()
        assert len(messages) == 3
        # Downloading the same months again changes nothing: bars are upserted.
        download_history(broker, db, "XAUUSD", Timeframe.M5, utc(2026, 1, 1), now)
    assert db.count_candles("XAUUSD", Timeframe.M5) == len(expected)


def test_symbol_spec_round_trip(db: Database) -> None:
    assert load_symbol_spec(db) is None
    spec = default_gold_spec("GOLD")
    save_symbol_spec(db, spec)
    assert load_symbol_spec(db) == spec


def test_fingerprint_names_the_exact_bars() -> None:
    frame = make_candles(utc(2026, 1, 5), 50)
    same = make_candles(utc(2026, 1, 5), 50)
    changed = frame.copy()
    changed.iloc[10, changed.columns.get_loc("close")] += 0.01
    assert fingerprint(frame) == fingerprint(same)
    assert fingerprint(frame) != fingerprint(changed)
    assert fingerprint(frame, None) != fingerprint(frame)
    assert len(fingerprint(frame)) == 16


def test_days_without_bars() -> None:
    frame = make_candles(utc(2026, 1, 5), 12, minutes=60)  # Monday only
    missing = days_without_bars(frame, utc(2026, 1, 5), utc(2026, 1, 12))
    assert missing == [dt.date(2026, 1, d) for d in (6, 7, 8, 9)]  # the weekend is not missing
    assert summarize_gaps(missing, limit=2) == "2026-01-06, 2026-01-07 and 2 more"
    assert summarize_gaps([]) == "none"
    assert days_without_bars(bars([], MONDAY), utc(2026, 1, 5), utc(2026, 1, 6)) == [
        dt.date(2026, 1, 5)
    ]


def test_cost_model_spreads() -> None:
    frame = bars([(1.0, 1.0, 1.0, 1.0)] * 3, MONDAY, spread=20)
    assert spread_points(frame, CostConfig()).tolist() == [20.0, 20.0, 20.0]
    padded = CostConfig(extra_spread_points=5)
    assert spread_points(frame, padded).tolist() == [25.0, 25.0, 25.0]
    fixed = CostConfig(spread_source="fixed", fixed_spread_points=30)
    assert spread_points(frame, fixed).tolist() == [30.0, 30.0, 30.0]
    index = pd.date_range(MONDAY, periods=4, freq="1D")
    medians = rolling_median(np.array([10.0, 30.0, 20.0, 40.0]), index, days=2)
    assert medians.tolist() == [10.0, 20.0, 25.0, 30.0]  # each bar sees itself and one day back
    assert rolling_median(np.array([]), pd.DatetimeIndex([], tz="UTC"), 5).size == 0


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


def test_the_textbook_setup_becomes_one_winning_trade(
    settings: Settings, market: MarketData, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    result = run_backtest(
        settings,
        market,
        TUESDAY,
        TUESDAY + dt.timedelta(hours=12),
        run_walk_forward=False,
        cache_dir=cache,
    )
    assert not result.signals_cached
    (trade,) = result.result.trades
    signal = trade.signal
    assert signal.id == "2026-11-17-LDN-01"
    # No news in a backtest: the bias is NEUTRAL, the bar is 80, the score 81.1.
    assert signal.score == pytest.approx(81.1) and signal.meets_auto_trade
    assert trade.fill_price == pytest.approx(2028.9)
    assert trade.fill_time == utc(2026, 11, 17, 8, 0)
    assert trade.lots == 0.17  # 50.00 / (2.81 x 100) rounded down
    assert [(leg.lots, leg.exit_price, leg.reason) for leg in trade.legs] == [
        (0.08, 2036.0, ExitReason.TP1),
        (0.09, 2048.0, ExitReason.TP2),
    ]
    # 7.10 x 8 + 19.10 x 9, less 7.00 commission per lot.
    assert trade.net_pnl == pytest.approx(56.8 + 171.9 - 7.0 * 0.17)
    assert trade.r_multiple > 4.5
    assert trade.record.pressure is not None
    assert result.metrics.trades == 1 and result.metrics.win_rate == 1.0
    assert list(result.breakdowns["session"]) == ["london"]
    assert result.fingerprints["data"] == market.fingerprint
    assert any("economic calendar" in note for note in result.limitations)
    assert any("one-minute history" in note for note in result.limitations)
    assert [o.outcome for o in result.result.outcomes if o.record.signal.id == signal.id] == [
        Outcome.TRADED
    ]

    again = run_backtest(
        settings,
        market,
        TUESDAY,
        TUESDAY + dt.timedelta(hours=12),
        run_walk_forward=False,
        cache_dir=cache,
    )
    assert again.signals_cached
    assert [t.net_pnl for t in again.result.trades] == [trade.net_pnl]


def test_results_by_model_and_by_confluence(settings: Settings, market: MarketData) -> None:
    from goldsweep.backtest.engine import traded_models
    from goldsweep.core.settings import ModelConfig

    assert traded_models(settings) == ["sweep_mss"]  # the alert-only models stay out
    result = run_backtest(
        settings, market, TUESDAY, TUESDAY + dt.timedelta(hours=12), run_walk_forward=False
    )
    assert list(result.breakdowns["model"]) == ["sweep_mss"]
    assert {"unicorn", "bpr", "judas"} <= set(result.breakdowns["confluence"])

    models = settings.config.models.model_copy(update={"cisd": ModelConfig(trade=True)})
    both = dataclasses.replace(
        settings, config=settings.config.model_copy(update={"models": models})
    )
    assert traded_models(both) == ["sweep_mss", "cisd"]
    run = run_backtest(
        both, market, TUESDAY, TUESDAY + dt.timedelta(hours=12), run_walk_forward=False
    )
    assert {record.signal.model for record in run.records} == {"sweep_mss", "cisd"}
    assert len(run.result.trades) == 1  # one raid, one position at a time


def test_backtests_trade_the_session_playbooks(settings: Settings, market: MarketData) -> None:
    """D87: only what a session's rule takes is traded; --candidates trades the A+ rules."""
    from goldsweep.backtest.engine import OUTSIDE_PLAYBOOKS
    from goldsweep.core.settings import PlaybookRule, SessionPlaybook

    end = TUESDAY + dt.timedelta(hours=12)
    result = run_backtest(settings, market, TUESDAY, end, run_walk_forward=False)
    assert result.playbooks == {
        "Asia": "none",
        "London": "sweep_mss, score >= 60",
        "New York": "sweep_mss, score >= 60",
    }
    assert not result.candidates
    assert list(result.breakdowns["playbook"]) == ["London"]

    candidates = run_backtest(
        settings, market, TUESDAY, end, run_walk_forward=False, candidates=True
    )
    assert candidates.candidates
    assert candidates.playbooks["London"] == "sweep_mss, score >= 60, with judas"
    assert len(candidates.result.trades) == 1  # the textbook raid is a Judas swing

    def london(book: SessionPlaybook) -> Settings:
        playbooks = settings.config.playbooks.model_copy(update={"london": book})
        return dataclasses.replace(
            settings, config=settings.config.model_copy(update={"playbooks": playbooks})
        )

    smt_only = london(SessionPlaybook(trading=PlaybookRule(tags_any=("smt",))))
    none = run_backtest(smt_only, market, TUESDAY, end, run_walk_forward=False)
    assert none.result.trades == []
    assert {(o.outcome, o.reason) for o in none.result.outcomes} == {
        (Outcome.NOT_SELECTED, OUTSIDE_PLAYBOOKS)
    }
    # With --candidates a session without a candidate keeps its trading rule.
    plain = london(SessionPlaybook(trading=PlaybookRule()))
    kept = run_backtest(plain, market, TUESDAY, end, run_walk_forward=False, candidates=True)
    assert kept.playbooks["London"] == "sweep_mss, score >= 60"
    assert len(kept.result.trades) == 1


def test_entry_lifetime_reuses_the_signals_but_the_entry_price_does_not(
    settings: Settings, market: MarketData, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"

    def run(**strategy: object):  # noqa: ANN202
        config = settings.config.model_copy(
            update={"strategy": settings.config.strategy.model_copy(update=strategy)}
        )
        return run_backtest(
            dataclasses.replace(settings, config=config),
            market,
            TUESDAY,
            TUESDAY + dt.timedelta(hours=12),
            run_walk_forward=False,
            cache_dir=cache,
        )

    assert not run().signals_cached
    longer = run(entry_expiry_bars=24)  # how long an entry waits changes no signal
    assert longer.signals_cached and len(longer.result.trades) == 1
    assert not run(entry_mode="fvg_far").signals_cached  # the entry price does


def test_report_files_are_written_and_reproducible(
    settings: Settings, market: MarketData, tmp_path: Path
) -> None:
    result = run_backtest(settings, market, TUESDAY, TUESDAY + dt.timedelta(hours=12))
    first = write_report(result, tmp_path / "first", "python -m goldsweep.backtest ...")
    second = write_report(result, tmp_path / "second", "python -m goldsweep.backtest ...")
    for name, path in first.items():
        assert path.read_bytes() == second[name].read_bytes(), name
    trades = pd.read_csv(first["trades"])
    assert list(trades["exit_reason"]) == ["tp2"] and trades["id"][0] == "2026-11-17-LDN-01"
    summary = json.loads(first["summary"].read_text(encoding="utf-8"))
    assert summary["metrics"]["trades"] == 1
    assert summary["walk_forward"]["folds"] == []
    assert "no walk-forward test was possible" in summary["walk_forward"]["warnings"][0]
    page = first["report"].read_text(encoding="utf-8")
    for text in (
        "GoldSweep backtest",
        "What this backtest cannot see",
        "2026-11-17-LDN-01",
        "Overfitting and reliability warnings",
        "By buyer/seller pressure meter",
    ):
        assert text in page
    assert "guarantee" not in page.lower() or "do not predict" in page
    assert pd.read_csv(first["equity"])["equity"].iloc[-1] == pytest.approx(
        10_000 + result.result.trades[0].net_pnl, abs=0.01
    )


def test_backtest_needs_an_end_after_its_start(settings: Settings, market: MarketData) -> None:
    with pytest.raises(ValueError, match="end after its start"):
        run_backtest(settings, market, TUESDAY, TUESDAY)


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


@pytest.fixture
def quick_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shipped config with a 3-day warm-up, and data and logs in a temporary folder."""
    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    assert "warmup_days: 10" in text
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("warmup_days: 10", "warmup_days: 3"), encoding="utf-8")
    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", str(tmp_path / "logs"))
    return path


def run_cli(*args: str, **kwargs: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(args), stdout=out, stderr=err, **kwargs)  # type: ignore[arg-type]
    return code, out.getvalue(), err.getvalue()


def test_cli_mock_run(quick_config: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "report"
    code, out, err = run_cli(
        "--from",
        "2026-01-06",
        "--to",
        "2026-01-07",
        "--mock",
        "--no-walk-forward",
        "--config",
        str(quick_config),
        "--out",
        str(out_dir),
    )
    assert code == 0, err
    assert "SYNTHETIC PRICES" in out and "Strategy    :" in out
    assert "100%" in err
    assert (out_dir / "report.html").exists() and (out_dir / "summary.json").exists()
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic"] is True
    assert summary["command"].endswith(f"--out {out_dir}")


def test_cli_downloads_then_reuses_history(quick_config: Path, tmp_path: Path) -> None:
    now = utc(2026, 1, 20)
    calls: list[str] = []

    def broker(settings: Settings) -> MockBroker:
        calls.append("connect")
        return MockBroker(clock=ManualClock(now), history_days=25)

    args = (
        "--from",
        "2026-01-08",
        "--to",
        "2026-01-09",
        "--no-walk-forward",
        "--config",
        str(quick_config),
        "--out",
        str(tmp_path / "report"),
    )
    code, out, err = run_cli(*args, broker_factory=broker, clock=ManualClock(now))
    assert code == 0, err
    assert calls == ["connect"]
    assert "Downloading history from MetaTrader 5" in err
    assert "MetaTrader 5 history from" in out
    code, out, err = run_cli(*args, "--offline", broker_factory=broker, clock=ManualClock(now))
    assert code == 0, err
    assert calls == ["connect"]  # stored history: no second connection
    assert "(reused from cache)" in out


def test_cli_offline_without_history(quick_config: Path) -> None:
    code, _out, err = run_cli(
        "--from", "2026-01-08", "--to", "2026-01-09", "--offline", "--config", str(quick_config)
    )
    assert code == 1
    assert "--offline forbids downloading" in err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--from", "2026-01-09", "--to", "2026-01-08"), "--to must not be before --from"),
        (("--from", "2027-01-01", "--to", "2027-01-02"), "in the future"),
    ],
)
def test_cli_rejects_bad_periods(quick_config: Path, args: tuple[str, ...], message: str) -> None:
    code, _out, err = run_cli(
        *args, "--config", str(quick_config), clock=ManualClock(utc(2026, 11, 17))
    )
    assert code == 2 and message in err


def test_cli_bad_date_and_bad_config(quick_config: Path, tmp_path: Path) -> None:
    code, _out, _err = run_cli("--from", "2026-13-01", "--to", "2026-01-02")
    assert code == 2
    broken = tmp_path / "broken.yaml"
    broken.write_text("strategy: {min_rr: -1}\n", encoding="utf-8")
    code, _out, err = run_cli("--from", "2026-01-01", "--to", "2026-01-02", "--config", str(broken))
    assert code == 2 and "Configuration error" in err


def test_walk_forward_windows_in_the_console_and_the_report(
    settings: Settings, market: MarketData, tmp_path: Path
) -> None:
    from goldsweep.backtest.cli import console_summary
    from goldsweep.backtest.metrics import summarize
    from goldsweep.backtest.report import render_html
    from goldsweep.backtest.walkforward import Combo, Fold, FoldResult, WalkForwardResult

    result = run_backtest(settings, market, TUESDAY, TUESDAY + dt.timedelta(hours=12))
    trades = result.result.trades
    window = Fold(utc(2026, 5, 17), utc(2026, 11, 17), TUESDAY, TUESDAY + dt.timedelta(days=1))
    fold = FoldResult(
        window, Combo(85.0, 2.5), True, summarize(trades, 10_000), summarize(trades, 10_000), 0
    )
    wf = WalkForwardResult(
        folds=[fold],
        out_of_sample=result.result,
        oos_metrics=summarize(trades, 10_000),
        in_sample_expectancy_r=1.0,
        warnings=["Only 1 out-of-sample trades: far too few to tell skill from luck."],
        grid=[Combo(85.0, 2.5)],
    )
    shown = dataclasses.replace(result, walk_forward=wf)
    paths = write_report(shown, tmp_path / "wf", "command")
    lines = console_summary(shown, paths)
    assert any(line.startswith("Walk-forward: 1 windows") for line in lines)
    assert any("with score >= 85, R:R >= 2.5 (defaults): 1 trades" in line for line in lines)
    assert any(line.startswith("!!! WARNING") for line in lines)
    page = render_html(shown, "command")
    assert "Walk-forward test (out-of-sample)" in page
    assert "score &gt;= 85, R:R &gt;= 2.5 (defaults)" in page

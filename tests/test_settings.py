"""Settings: config.yaml validation, .env handling and the PAPER/LIVE safety rule."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from goldsweep.core import settings as settings_module
from goldsweep.core.settings import (
    DEFAULT_CONFIG_PATH,
    MAX_RISK_PER_TRADE_PCT,
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    Settings,
    load_settings,
)
from goldsweep.core.types import Mode, Timeframe

ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
DEFAULT_TEXT = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")

#: Strategy settings the owner changed from the spec on purpose, as
#: {setting: (spec value and built-in default, shipped value)}. Each needs a
#: decision in DECISIONS.md; anything else that drifts from the spec fails.
OWNER_CHANGES: dict[str, tuple[float, float]] = {
    "auto_trade_threshold": (75, 60),  # D81: demo account, to collect evidence
    "entry_expiry_bars": (6, 24),  # D82: entries wait 2 hours (Phase 9 backtest)
}
#: The same for the risk section.
OWNER_RISK_CHANGES: dict[str, tuple[float, float]] = {
    "breakeven_at_r": (1.0, 0.0),  # D84: breakeven only when TP1 is reached
}


def write_config(tmp_path: Path, mutate: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """Copy the shipped config.yaml into ``tmp_path``, optionally changing it first."""
    data = yaml.safe_load(DEFAULT_TEXT)
    if mutate is not None:
        mutate(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_text(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def load(path: Path, **kwargs: Any) -> Settings:
    return load_settings(path, read_env_file=False, **kwargs)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def test_shipped_config_matches_the_spec(settings: Settings) -> None:
    cfg = settings.config
    assert cfg.symbol_candidates == ("XAUUSD", "XAUUSD.m", "XAUUSDm", "GOLD")
    # PAPER is the built-in default (spec rule 3); config.yaml's own mode line is
    # the owner's switch, so this test does not depend on it.
    assert AppConfig().mode is Mode.PAPER
    assert cfg.mode in (Mode.PAPER, Mode.LIVE)
    assert cfg.display_timezone == "Africa/Kampala"
    assert (cfg.timeframes.bias, cfg.timeframes.liquidity, cfg.timeframes.trigger) == (
        Timeframe.H1,
        Timeframe.M15,
        Timeframe.M5,
    )

    s = cfg.sessions
    assert (s.asia.tz, s.asia.start, s.asia.end, s.asia.trade) == (
        "Asia/Tokyo",
        dt.time(9, 0),
        dt.time(15, 0),
        True,  # the spec has False; the owner switched Asia on (D87)
    )
    assert (s.london.tz, s.london.start, s.london.end, s.london.trade) == (
        "Europe/London",
        dt.time(7, 0),
        dt.time(10, 0),
        True,
    )
    assert (s.newyork.tz, s.newyork.start, s.newyork.end, s.newyork.trade) == (
        "America/New_York",
        dt.time(8, 0),
        dt.time(11, 0),
        True,
    )
    assert (s.rollover_block.tz, s.rollover_block.start, s.rollover_block.end) == (
        "America/New_York",
        dt.time(16, 45),
        dt.time(18, 15),
    )
    assert (s.overlap.london.start, s.overlap.london.end) == (dt.time(8, 0), dt.time(16, 30))
    assert (s.overlap.newyork.start, s.overlap.newyork.end) == (dt.time(8, 0), dt.time(17, 0))
    assert (s.friday_cutoff.tz, s.friday_cutoff.time) == ("America/New_York", dt.time(12, 0))

    spec_strategy = {
        "atr_period": 14,
        "swing_lookback": 3,
        "eq_tolerance_atr": 0.15,
        "min_sweep_atr": 0.10,
        "max_bars_back_inside": 3,
        "mss_window": 12,
        "displacement_atr": 1.2,
        "entry_mode": "fvg_mid",
        "sl_buffer_atr": 0.10,
        "min_rr": 2.0,
        "entry_expiry_bars": 6,
        "alert_threshold": 60,
        "auto_trade_threshold": 75,
    }
    strategy = cfg.strategy.model_dump()
    shipped_changes = {key: shipped for key, (_, shipped) in OWNER_CHANGES.items()}
    assert {key: strategy[key] for key in spec_strategy} == spec_strategy | shipped_changes
    news = cfg.news
    assert (news.blackout_before_min, news.blackout_after_min) == (30, 15)  # spec 5.6
    assert (news.post_news_start_min, news.post_news_end_min) == (15, 90)
    assert news.post_news_sweep_mode and news.llm_enabled
    assert news.headline_half_life_min == 120
    assert [source.name for source in news.calendar_sources] == ["financecalendar", "forexfactory"]
    shipped_risk = {key: shipped for key, (_, shipped) in OWNER_RISK_CHANGES.items()}
    assert cfg.risk.model_dump() == shipped_risk | {
        "risk_per_trade_pct": 0.5,
        "max_open_positions": 1,
        "max_trades_per_day": 3,
        "daily_loss_limit_pct": 2.0,
        "weekly_loss_limit_pct": 5.0,
        "max_consecutive_losses": 3,
        "partial_close_at_tp1_pct": 50,
        # Spec section 6 values that the section 11 starter file left out.
        "max_trades_per_session": 2,
        "max_spread_multiple": 2.0,
        "spread_median_days": 20,
        # Spec 7b: the three-way split, 50 / 30 / 20, TP3 at 3R.
        "partial_close_at_tp2_pct": 30,
        "tp3_r": 3.0,
        # D84: targets at fixed R multiples instead of the pools, off unless chosen.
        "target_mode": "pools",
        "fixed_targets_r": (3.0,),
    }
    assert cfg.broker.server_timezone == "auto"
    wf = cfg.backtest.walk_forward
    assert (wf.train_months, wf.test_months) == (6, 2)  # spec 9.3


def test_built_in_defaults_equal_the_shipped_config(settings: Settings, tmp_path: Path) -> None:
    """An empty config file must behave exactly like the shipped one (apart from the
    owner's mode switch, which an empty file leaves at PAPER, and the owner's
    recorded changes, which an empty file leaves at the spec's values)."""
    from goldsweep.core.settings import PlaybooksConfig

    assert settings.config.execution.max_total_lots == 2.0
    spec_values = {key: spec for key, (spec, _) in OWNER_CHANGES.items()}
    spec_risk = {key: spec for key, (spec, _) in OWNER_RISK_CHANGES.items()}
    windows = settings.config.sessions
    backtest = settings.config.backtest
    shipped = settings.config.model_copy(
        update={
            "mode": Mode.PAPER,
            "strategy": settings.config.strategy.model_copy(update=spec_values),
            "risk": settings.config.risk.model_copy(update=spec_risk),
            # D87: the owner switched Asia on; the spec (and an empty file) has it off.
            "sessions": windows.model_copy(
                update={"asia": windows.asia.model_copy(update={"trade": False})}
            ),
            # The candidates come from the playbook analysis (D86), not from defaults.
            "playbooks": settings.config.playbooks.model_copy(
                update={
                    name: getattr(settings.config.playbooks, name).model_copy(
                        update={"candidate": None}
                    )
                    for name in ("asia", "london", "newyork")
                }
            ),
            # D108: this account charges no commission; the built-in default (an
            # ECN-style 7.00 a lot) stays the cautious value for other accounts.
            "backtest": backtest.model_copy(
                update={"costs": backtest.costs.model_copy(update={"commission_per_lot": 7.0})}
            ),
            # D116: the owner raised the lot cap to 2; the built-in default stays 1.
            "execution": settings.config.execution.model_copy(update={"max_total_lots": 1.0}),
        }
    )
    assert shipped.playbooks == PlaybooksConfig()
    assert settings.config.backtest.costs.commission_per_lot == 0.0
    assert load(write_text(tmp_path, "")).config == shipped


def test_the_switch_and_the_tick_recorder_settings(settings: Settings, tmp_path: Path) -> None:
    switch, ticks = settings.config.switch, settings.config.ticks
    assert (switch.strategies, switch.scalper_mode) == ("main", "off")
    assert (ticks.record, ticks.keep_days) == (False, 0)  # set aside by the owner (D111)
    # YAML reads a bare off as False: it still means "off".
    unquoted = load(write_text(tmp_path, "switch:\n  scalper_mode: off\n")).config.switch
    assert unquoted.scalper_mode == "off"
    assert load(write_text(tmp_path, "switch:\n  scalper_mode: hft\n")).config.switch.scalper_mode
    with pytest.raises(ConfigError, match="scalper_mode"):
        load(write_text(tmp_path, "switch:\n  scalper_mode: tick\n"))  # renamed hft (D111)
    with pytest.raises(ConfigError, match="strategies"):
        load(write_text(tmp_path, "switch:\n  strategies: all\n"))


def test_config_is_immutable(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.config.risk.risk_per_trade_pct = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.data_dir = Path(".")  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# PAPER / LIVE
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("env_mode", "config_mode", "expected"),
    [
        (None, "PAPER", Mode.PAPER),
        ("LIVE", "PAPER", Mode.PAPER),
        (None, "LIVE", Mode.PAPER),
        ("PAPER", "LIVE", Mode.PAPER),
        ("LIVE", "LIVE", Mode.LIVE),
        ("live", "live", Mode.LIVE),
    ],
)
def test_live_needs_both_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_mode: str | None,
    config_mode: str,
    expected: Mode,
) -> None:
    if env_mode is not None:
        monkeypatch.setenv("MODE", env_mode)
    path = write_config(tmp_path, lambda data: data.update(mode=config_mode))
    assert load(path).mode is expected


def test_mode_summary_names_both_switches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "LIVE")
    summary = load(write_config(tmp_path)).mode_summary
    assert "MODE=LIVE" in summary and "mode=PAPER" in summary


def test_unknown_mode_stops_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "LIVEE")
    with pytest.raises(ConfigError, match="mode"):
        load(DEFAULT_CONFIG_PATH)


# --------------------------------------------------------------------------- #
# Strict validation of config.yaml
# --------------------------------------------------------------------------- #


def test_risk_per_trade_cannot_exceed_two_percent(tmp_path: Path) -> None:
    path = write_config(tmp_path, lambda data: data["risk"].update(risk_per_trade_pct=2.5))
    with pytest.raises(ConfigError, match=r"risk\.risk_per_trade_pct"):
        load(path)


def test_risk_per_trade_of_exactly_two_percent_is_allowed(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, lambda data: data["risk"].update(risk_per_trade_pct=MAX_RISK_PER_TRADE_PCT)
    )
    assert load(path).config.risk.risk_per_trade_pct == 2.0


def test_zero_risk_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, lambda data: data["risk"].update(risk_per_trade_pct=0))
    with pytest.raises(ConfigError, match=r"risk\.risk_per_trade_pct"):
        load(path)


def test_unknown_key_is_rejected_not_ignored(tmp_path: Path) -> None:
    path = write_config(tmp_path, lambda data: data["risk"].update(risk_per_trade=1.0))
    with pytest.raises(ConfigError, match=r"risk\.risk_per_trade: Extra inputs"):
        load(path)


def test_secrets_cannot_live_in_config_yaml(tmp_path: Path) -> None:
    path = write_config(tmp_path, lambda data: data.update(mt5_password="hunter2"))
    with pytest.raises(ConfigError, match="mt5_password"):
        load(path)


def test_duplicate_key_is_rejected(tmp_path: Path) -> None:
    path = write_text(tmp_path, DEFAULT_TEXT + "\nmode: PAPER\n")
    with pytest.raises(ConfigError, match="duplicate key"):
        load(path)


def test_unquoted_clock_time_is_rejected(tmp_path: Path) -> None:
    """YAML reads an unquoted 15:00 as the number 900; that must never become a time."""
    text = DEFAULT_TEXT.replace('end: "15:00"', "end: 15:00")
    assert text != DEFAULT_TEXT
    with pytest.raises(ConfigError, match="quoted"):
        load(write_text(tmp_path, text))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["sessions"]["london"].update(tz="Europe/Londn"), "unknown IANA time zone"),
        (
            lambda d: d["sessions"]["london"].update(start="10:00", end="10:00"),
            "start and end must differ",
        ),
        (lambda d: d["sessions"]["london"].update(start="25:00"), "quoted"),
        (lambda d: d.update(display_timezone="Africa/Kampalaa"), "unknown IANA time zone"),
        (
            lambda d: d["strategy"].update(alert_threshold=80, auto_trade_threshold=70),
            "auto_trade_threshold",
        ),
        (lambda d: d["strategy"].update(entry_mode="fvg_middle"), "entry_mode"),
        (lambda d: d["strategy"].update(min_rr=0), "min_rr"),
        (lambda d: d["timeframes"].update(trigger="H1"), "trigger <= liquidity <= bias"),
        (lambda d: d["timeframes"].update(trigger="M2"), "trigger"),
        (lambda d: d.update(symbol_candidates=[]), "symbol_candidates"),
        (lambda d: d.update(symbol_candidates=["XAUUSD", "XAUUSD"]), "duplicates"),
        (lambda d: d["broker"].update(server_timezone="Mars/Olympus"), "server time zone"),
        (lambda d: d["broker"].update(server_timezone=180), "quoted text"),
        (lambda d: d["news"].update(blackout_before_min=-5), "blackout_before_min"),
        (lambda d: d["strategy"].update(fib_low=0.8, fib_high=0.7), "fib_low must be below"),
        (lambda d: d["strategy"].update(entry_zone_priority=[]), "at least one zone type"),
        (lambda d: d["strategy"].update(entry_zone_priority=["fvg", "fvg"]), "duplicates"),
        (lambda d: d["strategy"].update(entry_zone_priority=["breaker"]), "entry_zone_priority"),
        (
            lambda d: d["strategy"]["liquidity"].update(round_number_major_step=25),
            "multiple of round_number_step",
        ),
        (
            lambda d: d["strategy"]["liquidity"].update(round_number_major_step=5),
            "multiple of round_number_step",
        ),
        (lambda d: d["strategy"]["bias"].update(news_weight=0.5), "add up to 1.0, got 1.2"),
        (lambda d: d["strategy"]["crt"].update(timeframe="D1"), "H1 or H4"),
        (lambda d: d["strategy"]["scoring"].update(crt=10), "add up to 100, got 105"),
        (lambda d: d["strategy"].update(atr_method="ema"), "atr_method"),
        (lambda d: d["pressure"]["weights"].update(news=0.5), "pressure weights must add up"),
        (
            lambda d: d["pressure"].update(balanced_band=0.5, strong_level=0.4),
            "strong_level must be above",
        ),
        (
            lambda d: d["backtest"]["walk_forward"]["grid"].update(min_rr=[]),
            "at least one value",
        ),
        (
            lambda d: d["backtest"]["walk_forward"]["grid"].update(auto_trade_threshold=[70, 70]),
            "duplicates",
        ),
        (
            lambda d: d["backtest"]["walk_forward"]["grid"].update(min_rr=[1.5, 2.0]),
            "below strategy.min_rr",
        ),
        (lambda d: d["backtest"]["costs"].update(spread_source="tick"), "spread_source"),
        (lambda d: d["risk"].update(max_spread_multiple=1.0), "max_spread_multiple"),
        (
            lambda d: d["news"].update(post_news_start_min=90, post_news_end_min=15),
            "post_news_end_min must be after",
        ),
        (
            lambda d: d["news"].update(
                calendar_sources=[{"name": "forexfactory"}, {"name": "forexfactory"}]
            ),
            "lists a source twice",
        ),
        (lambda d: d["news"].update(calendar_sources=[{"name": "bloomberg"}]), "calendar_sources"),
        (
            lambda d: d["news"].update(rss_feeds=[{"name": "Fed", "url": "http://fed.example"}]),
            "https://",
        ),
    ],
)
def test_invalid_values_are_reported(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load(write_config(tmp_path, mutate))


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("NY+7", "NY+7"),
        ("Europe/Athens", "Europe/Athens"),
        ("+02:00", "+02:00"),
        ("UTC", "UTC"),
    ],
)
def test_server_timezone_forms(tmp_path: Path, given: str, stored: str) -> None:
    path = write_config(tmp_path, lambda data: data["broker"].update(server_timezone=given))
    assert load(path).config.broker.server_timezone == stored


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load(tmp_path / "nope.yaml")


def test_broken_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(write_text(tmp_path, "risk: [unclosed\n"))


def test_top_level_must_be_settings(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="top level"):
        load(write_text(tmp_path, "- just\n- a list\n"))


def test_config_path_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, lambda data: data["risk"].update(max_trades_per_day=2))
    monkeypatch.setenv("GOLDSWEEP_CONFIG", str(path))
    settings = load_settings(read_env_file=False)
    assert settings.config_path == path
    assert settings.config.risk.max_trades_per_day == 2


# --------------------------------------------------------------------------- #
# .env and environment
# --------------------------------------------------------------------------- #


def test_env_file_is_loaded_and_secrets_are_masked(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODE=PAPER\n"
        "MT5_LOGIN=12345678\n"
        "MT5_PASSWORD=hunter2-secret\n"
        "MT5_SERVER=Demo-Server\n"
        "TELEGRAM_CHAT_ID=\n"
        "ANTHROPIC_API_KEY=sk-test-key\n"
        "SOME_OTHER_TOOL=ignored\n",
        encoding="utf-8",
    )
    settings = load_settings(DEFAULT_CONFIG_PATH, env_file)
    env = settings.env
    assert settings.env_file == env_file
    assert env.mt5_login == 12345678
    assert env.mt5_server == "Demo-Server"
    assert env.telegram_chat_id is None
    assert env.mt5_password is not None
    assert env.mt5_password.get_secret_value() == "hunter2-secret"
    for text in (repr(settings), str(env), repr(env)):
        assert "hunter2-secret" not in text
        assert "sk-test-key" not in text


def test_process_environment_overrides_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MT5_SERVER=FromFile\n", encoding="utf-8")
    monkeypatch.setenv("MT5_SERVER", "FromEnvironment")
    assert load_settings(DEFAULT_CONFIG_PATH, env_file).env.mt5_server == "FromEnvironment"


def test_blank_values_count_as_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_LOGIN", "")
    monkeypatch.setenv("MODE", "")
    env = load(DEFAULT_CONFIG_PATH).env
    assert env.mt5_login is None
    assert env.mode is Mode.PAPER


def test_env_example_has_every_key_and_defaults_to_paper() -> None:
    keys = [
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert keys == [
        "MODE",
        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
        "MT5_PATH",
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "FINNHUB_API_KEY",
        "FINANCECALENDAR_API_KEY",
        "FRED_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    env = load_settings(DEFAULT_CONFIG_PATH, ENV_EXAMPLE).env
    assert env.mode is Mode.PAPER
    assert str(env.mt5_path) == r"C:\Program Files\MetaTrader 5\terminal64.exe"
    assert env.llm_provider == "anthropic" and env.llm_model is None  # the provider's default
    assert env.mt5_login is None
    assert env.mt5_password is None
    assert env.anthropic_api_key is None
    assert env.telegram_chat_id is None


def test_named_env_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="env file not found"):
        load_settings(DEFAULT_CONFIG_PATH, tmp_path / "missing.env")


def test_data_and_log_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = load(DEFAULT_CONFIG_PATH)
    assert defaults.data_dir == PROJECT_ROOT / "data"
    assert defaults.log_dir == PROJECT_ROOT / "logs"
    assert defaults.db_path == PROJECT_ROOT / "data" / "goldsweep.sqlite3"
    assert defaults.mock_db_path == PROJECT_ROOT / "data" / "goldsweep-mock.sqlite3"

    explicit = load(DEFAULT_CONFIG_PATH, data_dir=tmp_path / "d", log_dir=tmp_path / "l")
    assert (explicit.data_dir, explicit.log_dir) == (tmp_path / "d", tmp_path / "l")

    monkeypatch.setenv("GOLDSWEEP_DATA_DIR", str(tmp_path / "from-env"))
    monkeypatch.setenv("GOLDSWEEP_LOG_DIR", "relative-logs")
    from_env = load(DEFAULT_CONFIG_PATH)
    assert from_env.data_dir == tmp_path / "from-env"
    assert from_env.log_dir == PROJECT_ROOT / "relative-logs"


def test_yaml_merge_keys_are_supported(tmp_path: Path) -> None:
    text = (
        "sessions:\n"
        '  london: &kill_zone {tz: Europe/London, start: "07:00", end: "10:00", trade: true}\n'
        '  newyork: {<<: *kill_zone, tz: America/New_York, start: "08:00", end: "11:00"}\n'
    )
    sessions = load(write_text(tmp_path, text)).config.sessions
    assert sessions.newyork.tz == "America/New_York"
    assert sessions.newyork.trade is True  # inherited through the merge key


def test_unusable_yaml_key_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(write_text(tmp_path, "? [a, b]\n: 1\n"))


def test_env_file_next_to_main_py_is_found_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MT5_SERVER=FromDefaultFile\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "DEFAULT_ENV_PATH", env_file)
    settings = load_settings(DEFAULT_CONFIG_PATH)
    assert settings.env_file == env_file
    assert settings.env.mt5_server == "FromDefaultFile"
    assert load(DEFAULT_CONFIG_PATH).env.mt5_server is None  # read_env_file=False skips it


def test_a_bot_token_in_the_chat_id_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1234567890:AAH" + "x" * 32)
    with pytest.raises(ConfigError, match="this is a bot token, not a chat id"):
        load_settings(DEFAULT_CONFIG_PATH, read_env_file=False, data_dir=tmp_path)

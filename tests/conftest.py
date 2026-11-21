"""Fixtures shared by the whole test suite."""

# ruff: noqa: E402 - the shipped config is swapped for a PAPER copy before the imports below

from __future__ import annotations

import atexit
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import goldsweep.core.settings as _settings_module


def _paper_copy_of_the_shipped_config() -> Path:
    """config.yaml as shipped, but always in PAPER mode.

    The owner switches ``mode:`` in config.yaml to LIVE for a demo run; that switch
    must never change a test result. Every test (and ``load_settings`` without a
    path) reads this copy instead, so it runs before any test module imports
    ``DEFAULT_CONFIG_PATH``.
    """
    text = _settings_module.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^mode:[ \t]*\S+[ \t]*$", "mode: PAPER", text, count=1)
    folder = Path(tempfile.mkdtemp(prefix="goldsweep-tests-"))
    atexit.register(shutil.rmtree, folder, True)
    path = folder / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


_settings_module.DEFAULT_CONFIG_PATH = _paper_copy_of_the_shipped_config()

from goldsweep.analysis.signals import Signal
from goldsweep.core.clock import SessionCalculator
from goldsweep.core.logging import shutdown_logging
from goldsweep.core.settings import DEFAULT_CONFIG_PATH, Settings, load_settings
from goldsweep.storage.db import Database

# Every environment variable GoldSweep reads.
ENV_KEYS = (
    "MODE",
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_PATH",
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER",
    "GROQ_API_KEY",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "FINNHUB_API_KEY",
    "FINANCECALENDAR_API_KEY",
    "FRED_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GOLDSWEEP_CONFIG",
    "GOLDSWEEP_DATA_DIR",
    "GOLDSWEEP_LOG_DIR",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests never see the developer's real environment, .env file or log handlers."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # The CLI reads .env next to main.py by default: the owner's real keys must not
    # leak into tests (nor change their outcome).
    monkeypatch.setattr(
        "goldsweep.core.settings.DEFAULT_ENV_PATH", Path(__file__).parent / "no-such-file.env"
    )
    yield
    shutdown_logging()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """The shipped config.yaml, no .env file, data and logs in a temporary folder."""
    return load_settings(
        DEFAULT_CONFIG_PATH,
        read_env_file=False,
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def sessions(settings: Settings) -> SessionCalculator:
    return SessionCalculator(settings.config.sessions)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


@pytest.fixture(scope="session")
def base_signal() -> Signal:
    """The textbook long from the Phase 2 engine fixture (tests/test_signals.py).

    Backtest tests reshape it with ``tests.test_simulator.make_signal``.
    """
    from tests.test_signals import engine_for, feed, only_signal, with_raid

    settings = load_settings(DEFAULT_CONFIG_PATH, read_env_file=False)
    return only_signal(feed(engine_for(settings), with_raid()))

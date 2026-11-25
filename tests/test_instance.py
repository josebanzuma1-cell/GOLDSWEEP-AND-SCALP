"""One running bot at a time (D119): a second copy says so and stops."""

from __future__ import annotations

from pathlib import Path

import pytest

from goldsweep.cli import EXIT_RUNNING, _alone
from goldsweep.core.instance import LOCK_NAME, AlreadyRunning, single_instance
from goldsweep.core.settings import Settings


def test_a_second_copy_is_refused_until_the_first_stops(tmp_path: Path) -> None:
    with single_instance(tmp_path) as path:
        assert path == tmp_path / LOCK_NAME and path.read_text(encoding="utf-8").isdigit()
        with pytest.raises(AlreadyRunning, match="already running"), single_instance(tmp_path):
            pass
    with single_instance(tmp_path):  # released when the first one ended
        pass


def test_the_lock_file_left_behind_never_blocks_a_start(tmp_path: Path) -> None:
    (tmp_path / LOCK_NAME).write_text("12345", encoding="utf-8")  # e.g. after a crash
    with single_instance(tmp_path):
        pass


def test_the_command_line_stops_a_second_bot(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[str] = []

    def run() -> int:
        ran.append("first")
        assert _alone(settings, lambda: ran.append("second") or 0) == EXIT_RUNNING
        return 0

    assert _alone(settings, run) == 0
    assert ran == ["first"]
    assert "GoldSweep is already running" in capsys.readouterr().out

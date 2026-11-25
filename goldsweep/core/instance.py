"""One running bot at a time (D119).

The scheduled tasks (``deploy/install-tasks.ps1``) start the bot at logon and again
every five minutes if it has stopped, so a copy started by hand on top of it would send
every signal twice and write the same state. The bot therefore holds an exclusive lock on
a file in the data folder while it runs; a second copy finds it held, says so and exits.
The operating system frees the lock when the process ends, even after a crash, so a stale
file never blocks a start.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

LOCK_NAME = "goldsweep-bot.lock"
#: The byte locked: past the process number written at the start, which stays readable.
LOCK_OFFSET = 1024


class AlreadyRunning(RuntimeError):
    """Another GoldSweep bot holds the lock."""


def _lock(handle: IO[str]) -> bool:
    """Take the lock without waiting; False when another process holds it."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - the bot runs on Windows; kept for other systems
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def single_instance(folder: Path) -> Iterator[Path]:
    """Hold the bot's lock in ``folder`` for the ``with`` block.

    Raises:
        AlreadyRunning: another bot holds it.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOCK_NAME
    handle = path.open("a+", encoding="utf-8")
    try:
        if not _lock(handle):
            raise AlreadyRunning(f"another GoldSweep bot is already running ({path})")
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield path
    finally:
        handle.close()  # closing the file releases the lock

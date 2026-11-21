"""``python main.py --dashboard``: start the Streamlit dashboard on this PC.

Streamlit runs as a child process, bound to ``localhost`` only: the page has a
kill-switch button, so it must not be reachable from other machines on the
network. Usage statistics are switched off, and the server runs headless (no
first-run e-mail prompt that would leave the window waiting); the browser is
opened here instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from goldsweep.core.settings import Settings

APP = Path(__file__).with_name("app.py")
PORT = 8501
URL = f"http://localhost:{PORT}"


def streamlit_command(app: Path = APP, port: int = PORT) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.address",
        "localhost",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--server.fileWatcherType",
        "none",
        "--browser.gatherUsageStats",
        "false",
        "--client.toolbarMode",
        "minimal",
    ]


def run_dashboard(
    settings: Settings,
    *,
    use_mock: bool = False,
    out: TextIO | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    open_browser: Callable[[str], object] = webbrowser.open,
    wait_seconds: float = 4.0,
) -> int:
    """Run the dashboard until its window is closed or Ctrl+C. Returns an exit code."""
    stream = out or sys.stdout
    database = settings.mock_db_path if use_mock else settings.db_path
    env = dict(os.environ)
    env["GOLDSWEEP_DASHBOARD_DB"] = str(database)
    env["GOLDSWEEP_REPORTS_DIR"] = str(settings.project_root / "reports")
    command: Sequence[str] = streamlit_command()
    print(f"Starting the GoldSweep dashboard at {URL} (reading {database})", file=stream)
    print(
        "Keep this window open while you use it; Ctrl+C or closing it stops the dashboard.",
        file=stream,
    )
    print("The trading bot is a separate program and keeps running.", file=stream, flush=True)
    process = popen(command, env=env)
    try:
        time.sleep(wait_seconds)
        if process.poll() is None:
            open_browser(URL)
        return int(process.wait())
    except KeyboardInterrupt:
        process.terminate()
        return 0

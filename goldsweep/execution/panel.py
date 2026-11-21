"""``signals.json`` for the on-chart panel (spec 7b.5).

Python writes the file; the MQL5 indicator ``mt5/GoldSweepPanel.mq5`` only
reads it and draws, for each recent signal, a BUY/SELL label, the entry, stop
and TP1/TP2/TP3 lines, a shaded risk and reward box and the swept level, plus
a corner box with the mode, the trading state, the pressure meter and the news
bias. The two talk through this file only.

The file lives in MetaTrader 5's shared ``Common\\Files\\GoldSweep`` folder,
which every terminal on the PC can read. Times are written in the chart's
clock (the broker's server time), so the indicator can place objects without
converting anything. Each signal sits on its own line, which keeps the MQL5
side a simple line-by-line reader.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.settings import Settings
from goldsweep.data.broker import BrokerClient, BrokerError
from goldsweep.execution.book import third_target
from goldsweep.storage.db import Database

log = get_logger(__name__)

PANEL_FOLDER = "GoldSweep"
PANEL_FILE = "signals.json"
SHOWN_ACTIONS = ("traded", "alert", "blocked")
LOOKBACK = dt.timedelta(hours=24)


def _utc_epoch(moment: dt.datetime) -> int:
    return int(ensure_utc(moment).timestamp())


@dataclass(frozen=True)
class PanelWriter:
    path: Path
    #: True UTC -> the chart's clock (broker server time) as epoch seconds.
    to_chart_time: Callable[[dt.datetime], int]
    tp3_r: float
    limit: int
    digits: int = 5  # prices are written rounded to the symbol's digits

    @classmethod
    def for_broker(cls, broker: BrokerClient, settings: Settings) -> PanelWriter | None:
        """Where the panel file goes for this broker, or ``None`` when the panel is off."""
        execution = settings.config.execution
        if not execution.panel_enabled:
            return None
        from goldsweep.data.mt5_client import MT5Client

        tp3_r, limit = settings.config.risk.tp3_r, execution.panel_signals
        try:
            digits = broker.resolve_symbol(settings.config.symbol_candidates).digits
            common = broker.terminal_info().common_data_path
        except BrokerError as exc:
            log.warning("panel off: the terminal did not answer", error=str(exc))
            return None
        if isinstance(broker, MT5Client):
            if not common:
                return None
            path = Path(common) / "Files" / PANEL_FOLDER / PANEL_FILE
            return cls(path, broker.utc_to_server_epoch, tp3_r, limit, digits)
        # The mock broker has no terminal: write next to the database instead.
        return cls(settings.data_dir / "panel" / PANEL_FILE, _utc_epoch, tp3_r, limit, digits)

    def content(
        self,
        db: Database,
        *,
        now: dt.datetime,
        mode: str,
        state: str,
        pressure: str | None,
        news_bias: float | None,
        open_ids: Collection[str] = (),
        waiting_ids: Collection[str] = (),
    ) -> str:
        since = _utc_epoch(now - LOOKBACK)
        rows = db.query(
            "SELECT id, time, direction, score, entry, stop_loss, take_profit_1, take_profit_2, "
            "action, detail FROM signals WHERE time >= ? AND action IN (?, ?, ?) "
            "ORDER BY time DESC LIMIT ?",
            (since, *SHOWN_ACTIONS, self.limit),
        )
        lines = []
        for signal_id, at, direction, score, entry, sl, tp1, tp2, action, detail in reversed(rows):
            extra = json.loads(detail or "{}")
            tp3 = third_target(Direction(direction), entry, sl, (tp1, tp2), self.tp3_r)

            def price(value: float | None) -> float:
                return round(float(value), self.digits) if value else 0

            status = action
            if signal_id in open_ids:
                status = "open"
            elif signal_id in waiting_ids:
                status = "waiting"
            item: dict[str, Any] = {
                "id": signal_id,
                "t": self.to_chart_time(dt.datetime.fromtimestamp(at, tz=dt.UTC)),
                "dir": "BUY" if direction == Direction.LONG.value else "SELL",
                "score": round(float(score), 1),
                "entry": price(entry),
                "sl": price(sl),
                "tp1": price(tp1),
                "tp2": price(tp2),
                "tp3": price(tp3),
                "pool": price(extra.get("pool_price")),
                "pool_label": str(extra.get("pool") or ""),
                "status": status,
            }
            lines.append(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
        head = {
            "version": 1,
            "mode": mode,
            "state": state,
            "updated": self.to_chart_time(now),
            "updated_utc": ensure_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
            "pressure": pressure or "",
            "news_bias": round(news_bias, 2) if news_bias is not None else 0,
        }
        opening = json.dumps(head, ensure_ascii=True, separators=(",", ":"))[:-1]
        # Windows line ends: MQL5 reads a text file line by line up to "\r\n".
        body = ",\r\n".join(lines) + ("\r\n" if lines else "")
        return opening + ',"signals":[\r\n' + body + "]}\r\n"

    def write(self, db: Database, **kwargs: Any) -> bool:
        """Replace the file; ``False`` (and a log line) when it could not be written."""
        try:
            text = self.content(db, **kwargs)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(text, encoding="ascii", newline="")
            for _attempt in range(5):
                try:
                    os.replace(temporary, self.path)  # readers never see half a file
                    return True
                except PermissionError:  # the indicator is reading it right now
                    time.sleep(0.1)
            self.path.write_text(text, encoding="ascii", newline="")
            temporary.unlink(missing_ok=True)
            return True
        except (OSError, ValueError) as exc:
            log.warning("panel file not written", path=str(self.path), error=str(exc))
            return False

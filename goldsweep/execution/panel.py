"""``signals.json`` for the on-chart panel (spec 7b.5; v2 in S8, D114).

Python writes the file; the MQL5 indicator ``mt5/GoldSweepPanel.mq5`` reads it and
draws, for each recent signal, a BUY/SELL tag with its grade (A+ / A / B), the entry,
stop and TP1/TP2/TP3 lines, a shaded risk and reward box, the swept level and the
sweep's "x", and the result once closed; setups still forming (A+ possible or not);
the nearest liquidity pools and open fair value gaps; and the analysis box (biases,
pressure, session, next release, the latest scenario brief). The two talk through
this file only; text values are plain ASCII without quotes.

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
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
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
SHOWN_ACTIONS = ("traded", "alert", "blocked", "armed")
_MARKS = ", ".join("?" for _ in SHOWN_ACTIONS)
LOOKBACK = dt.timedelta(hours=24)


def _utc_epoch(moment: dt.datetime) -> int:
    return int(ensure_utc(moment).timestamp())


#: Typography the AI briefs and labels use, in plain ASCII (the rest loses its accents).
_ASCII = str.maketrans(
    {
        "—": "-",
        "–": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "“": "'",
        "”": "'",
        "…": "...",
        "→": "->",
        "←": "<-",
        "•": "*",
        " ": " ",
        "±": "+/-",
    }
)


def ascii_text(value: object, limit: int = 240) -> str:
    """Plain ASCII for the MQL5 reader: dashes and quotes kept as ASCII, accents dropped,
    no double quotes or backslashes (its line reader stops at the first quote), one
    line, ``limit`` long."""
    text = str(value).translate(_ASCII)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.replace('"', "'").replace("\\", "/").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())[:limit]


def grade_letter(grade: object) -> str:
    """``"A+ (London playbook)"`` -> ``"A+"``; ``""`` when unknown."""
    text = str(grade or "").strip()
    return text.split(" ", 1)[0] if text else ""


def _item(kind: str, values: Mapping[str, Any]) -> str:
    return json.dumps({"k": kind, **values}, ensure_ascii=True, separators=(",", ":"))


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

    def _price(self, value: float | None) -> float:
        return round(float(value), self.digits) if value else 0

    def _chart(self, moment: dt.datetime | str | None) -> int:
        if moment is None:
            return 0
        if isinstance(moment, str):
            moment = dt.datetime.fromisoformat(moment)
        return self.to_chart_time(ensure_utc(moment))

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
        armed_ids: Collection[str] = (),
        monitored_ids: Collection[str] = (),
        results: Mapping[str, tuple[float, str, dt.datetime]] | None = None,
        analysis: Mapping[str, Any] | None = None,
        forming: Sequence[Mapping[str, Any]] = (),
        pools: Sequence[Mapping[str, Any]] = (),
        gaps: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """The whole file (v2, S8): the head with the analysis, then one item per line.

        Signal lines keep every v1 key (an old indicator still reads them) and add the
        grade, model, tags, playbook, the sweep's extreme and the result once closed.
        ``forming`` / ``pools`` / ``gaps`` become ``"k":"forming" | "pool" | "fvg"`` lines.
        """
        since = _utc_epoch(now - LOOKBACK)
        rows = db.query(
            "SELECT id, time, direction, score, entry, stop_loss, take_profit_1, take_profit_2, "
            f"action, detail FROM signals WHERE time >= ? AND action IN ({_MARKS}) "
            "ORDER BY time DESC LIMIT ?",
            (since, *SHOWN_ACTIONS, self.limit),
        )
        price = self._price
        done = results or {}
        lines = []
        for signal_id, at, direction, score, entry, sl, tp1, tp2, action, detail in reversed(rows):
            extra = json.loads(detail or "{}")
            tp3 = third_target(Direction(direction), entry, sl, (tp1, tp2), self.tp3_r)
            status = action
            if signal_id in open_ids:
                status = "open"
            elif signal_id in waiting_ids:
                status = "waiting"
            elif signal_id in armed_ids:
                status = "armed"
            elif signal_id in done:
                status = "closed"
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
                "pool_label": ascii_text(extra.get("pool") or ""),
                "status": status,
                # v2 (S8, D114)
                "grade": grade_letter(extra.get("grade")),
                "model": ascii_text(extra.get("model") or "sweep_mss"),
                "tags": ascii_text(",".join(extra.get("tags") or [])),
                "playbook": ascii_text(extra.get("playbook") or ""),
                "monitored": int(bool(extra.get("monitoring")) or signal_id in monitored_ids),
                "sweep_x": price(extra.get("sweep_x")),
                "sweep_t": self._chart(extra.get("sweep_t")),
            }
            if signal_id in done:
                r, reason, closed = done[signal_id]
                item.update({"r": round(r, 2), "exit": reason, "end": self._chart(closed)})
            lines.append(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
        lines += [_item("forming", self._forming(f)) for f in forming]
        lines += [
            _item(
                "pool",
                {
                    "p": price(p["price"]),
                    "label": ascii_text(p.get("label") or ""),
                    "side": p.get("side", ""),
                    "str": round(float(p.get("strength", 0.0)), 2),
                    "t": self._chart(p.get("created")),
                },
            )
            for p in pools
        ]
        lines += [
            _item(
                "fvg",
                {
                    "t": self._chart(g["time"]),
                    "top": price(g["top"]),
                    "bot": price(g["bottom"]),
                    "dir": "BUY" if g["direction"] == Direction.LONG.value else "SELL",
                },
            )
            for g in gaps
        ]
        head: dict[str, Any] = {
            "version": 2,
            "mode": mode,
            "state": state,
            "updated": self.to_chart_time(now),
            "updated_utc": ensure_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
            "pressure": ascii_text(pressure or ""),
            "news_bias": round(news_bias, 2) if news_bias is not None else 0,
        }
        for key, value in (analysis or {}).items():
            head[key] = ascii_text(value) if isinstance(value, str) else value
        opening = json.dumps(head, ensure_ascii=True, separators=(",", ":"))[:-1]
        # Windows line ends: MQL5 reads a text file line by line up to "\r\n".
        body = ",\r\n".join(lines) + ("\r\n" if lines else "")
        return opening + ',"signals":[\r\n' + body + "]}\r\n"

    def _forming(self, forming: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "t": self._chart(forming["since"]),
            "dir": "BUY" if forming["direction"] == Direction.LONG.value else "SELL",
            "level": self._price(forming["level"]),
            "label": ascii_text(forming.get("label") or ""),
            "x": self._price(forming.get("extreme")),
            "stage": forming.get("stage", ""),
            "text": ascii_text(forming.get("text") or ""),
            "aplus": int(bool(forming.get("a_plus"))),
            "model": ascii_text(forming.get("model") or ""),
        }

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

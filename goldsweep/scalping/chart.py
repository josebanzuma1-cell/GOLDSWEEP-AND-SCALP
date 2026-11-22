"""``scalp.json`` for the chart panel, and the panel's button commands (S7, D99, D113).

The bot rewrites ``scalp.json`` next to ``signals.json`` (MetaTrader 5's shared
``Common\\Files\\GoldSweep`` folder) after every scalper minute. ``GoldSweepPanel.mq5``
v2 draws from it: the HFT step line and its sweep "X" markers, every recent scalp's
box (risk and reward zones, SL / ENTRY / TP1-TP3 labels, TP-hit markers, the result
once closed) and the scalper's dashboard box (mode, status, market, the active scalp,
today's and 30 days' results). The format is the one ``signals.json`` uses: a head
object on the first line, then one item per line, so MQL5 reads it line by line. Times
are the chart's clock (broker server time); ``updated_gmt`` is true UTC, so the panel
can tell when the file has gone stale.

The panel's buttons write one line to ``scalp_commands.json``:
``{"id":<n>,"t":<UTC epoch>,"cmd":"pause"}``. The bot carries out a command once (a
higher id than the last one it did), only if it is at most two minutes old and one of
:data:`PANEL_COMMANDS`, and reports it on Telegram like a dashboard command. The panel
never trades.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goldsweep.core.clock import ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.scalping.logic import ChartMarks
from goldsweep.scalping.stats import ClosedScalp, Stats

log = get_logger(__name__)

SCALP_FILE = "scalp.json"
COMMANDS_FILE = "scalp_commands.json"
#: What the panel's buttons may ask for (D99): no orders, ever.
PANEL_COMMANDS = ("pause", "resume", "scalper:off")
#: Closed scalps drawn on the chart (their boxes and TP-hit markers).
SHOWN_CLOSED = 12
_TP_REASONS = {"tp1": "TP1", "tp2": "TP2", "tp3": "TP3"}


@dataclass(frozen=True, slots=True)
class PanelCommand:
    id: int
    time: dt.datetime  # UTC
    name: str


def _line(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True)
class ScalpPanel:
    """Where the scalper's chart file lives, and how times reach the chart's clock."""

    folder: Path
    to_chart_time: Callable[[dt.datetime], int]
    digits: int = 2

    @property
    def path(self) -> Path:
        return self.folder / SCALP_FILE

    @property
    def commands_path(self) -> Path:
        return self.folder / COMMANDS_FILE

    def _price(self, value: float | None) -> float:
        return round(float(value), self.digits) if value else 0.0

    def _t(self, moment: dt.datetime | str | None) -> int:
        if moment is None:
            return 0
        if isinstance(moment, str):
            moment = dt.datetime.fromisoformat(moment)
        return self.to_chart_time(ensure_utc(moment))

    # -- the file ------------------------------------------------------------------ #

    def content(
        self,
        *,
        now: dt.datetime,
        mode: str,
        where: str,
        state: str,
        strategies: str,
        status: str,
        marks: ChartMarks,
        open_scalps: Sequence[dict[str, Any]],
        closed: Sequence[ClosedScalp],
        today: Stats,
        month: Stats,
        session: str | None,
        spread: float | None,
        spread_median: float | None,
        news: str,
        price: float | None,
    ) -> str:
        """The whole file: the head on the first line, then one item per line."""
        head: dict[str, Any] = {
            "version": 2,
            "mode": mode,
            "where": where.upper(),
            "state": state,
            "strategies": strategies,
            "status": status,
            "tf": marks.timeframe,
            "trend": marks.trend,
            "atr": round(marks.atr, 2) if marks.atr is not None else 0,
            "session": session or "-",
            "spread": round(spread, 1) if spread is not None else 0,
            "spread_median": round(spread_median, 1) if spread_median is not None else 0,
            "news": news,
            "updated": self.to_chart_time(now),
            "updated_gmt": int(ensure_utc(now).timestamp()),
            "updated_utc": ensure_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
        }
        for prefix, stats in (("today", today), ("d30", month)):
            head.update(
                {
                    f"{prefix}_n": stats.trades,
                    f"{prefix}_win": round(stats.win_rate or 0.0, 3),
                    f"{prefix}_be": round(stats.breakeven_win_rate or 0.0, 3),
                    f"{prefix}_pf": round(stats.profit_factor or 0.0, 2),
                    f"{prefix}_r": round(stats.total_r, 2),
                    f"{prefix}_net": round(stats.net, 2),
                }
            )
        head.update(self._active(open_scalps, mode, now, price))
        items = [
            _line({"k": "step", "t": self._t(t), "v": self._price(value), "up": int(trend > 0)})
            for t, value, trend in marks.line
        ]
        items += [
            _line({"k": "x", "t": self._t(t), "p": self._price(p), "s": side})
            for t, p, side in marks.sweeps
        ]
        for scalp in closed[-SHOWN_CLOSED:]:
            items.append(_line(self._closed_item(scalp)))
            for leg in scalp.legs:
                label = _TP_REASONS.get(str(leg.get("reason")))
                if label:
                    items.append(
                        _line(
                            {
                                "k": "hit",
                                "id": scalp.id,
                                "t": self._t(str(leg["time"])),
                                "p": self._price(leg.get("price")),
                                "tp": label,
                            }
                        )
                    )
        items += [_line(self._open_item(p, now, price)) for p in open_scalps]
        opening = _line(head)[:-1]
        # Windows line ends: MQL5 reads a text file line by line up to "\r\n".
        body = ",\r\n".join(items) + ("\r\n" if items else "")
        return opening + ',"items":[\r\n' + body + "]}\r\n"

    def _closed_item(self, scalp: ClosedScalp) -> dict[str, Any]:
        return {
            "k": "scalp",
            "id": scalp.id,
            "mode": scalp.mode,
            "where": scalp.where,
            "dir": "BUY" if scalp.direction == "LONG" else "SELL",
            "t": self._t(scalp.fill_time),
            "end": self._t(scalp.exit_time),
            "entry": self._price(scalp.fill_price),
            "sl": self._price(scalp.stop_loss),
            "stop": self._price(scalp.stop_loss),
            "tp1": self._price(scalp.take_profit_1),
            "tp2": self._price(scalp.take_profit_2),
            "tp3": self._price(scalp.take_profit_3),
            "status": "closed",
            "r": round(scalp.r_multiple, 2),
            "exit": scalp.exit_reason,
        }

    @staticmethod
    def _risk_r(position: dict[str, Any], price: float | None) -> float:
        fill, first = float(position["fill_price"]), float(position.get("stop_loss") or 0.0)
        risk = abs(fill - first)
        if price is None or risk <= 0:
            return 0.0
        sign = 1.0 if position["direction"] == "LONG" else -1.0
        return round((price - fill) * sign / risk, 2)

    def _open_item(
        self, position: dict[str, Any], now: dt.datetime, price: float | None
    ) -> dict[str, Any]:
        targets = list(position.get("take_profits") or position.get("targets") or [])
        targets += [0.0] * (3 - len(targets))
        return {
            "k": "scalp",
            "id": position["id"],
            "mode": position.get("mode", "?"),
            "where": position.get("where", "paper"),
            "dir": "BUY" if position["direction"] == "LONG" else "SELL",
            "t": self._t(position.get("fill_time")) or self._t(now),
            "end": 0,
            "entry": self._price(position["fill_price"]),
            "sl": self._price(position.get("stop_loss") or position["stop"]),
            "stop": self._price(position["stop"]),
            "tp1": self._price(targets[0]),
            "tp2": self._price(targets[1]),
            "tp3": self._price(targets[2]),
            "status": "open",
            "r": self._risk_r(position, price),
            "exit": "",
        }

    def _active(
        self,
        open_scalps: Sequence[dict[str, Any]],
        mode: str,
        now: dt.datetime,
        price: float | None,
    ) -> dict[str, Any]:
        """The dashboard's trade column: the selected mode's open scalp, else any."""
        chosen = next((p for p in open_scalps if p.get("mode") == mode), None)
        chosen = chosen or (open_scalps[0] if open_scalps else None)
        if chosen is None:
            return {"active": ""}
        planned = list(chosen.get("take_profits") or [])
        live = list(chosen.get("targets") or [])
        hit = max(0, len(planned) - len(live))
        opened = chosen.get("fill_time")
        minutes = (
            int((ensure_utc(now) - dt.datetime.fromisoformat(opened)).total_seconds() // 60)
            if opened
            else 0
        )
        targets = planned + [0.0] * (3 - len(planned))
        return {
            "active": chosen["id"],
            "active_mode": chosen.get("mode", "?"),
            "active_dir": "BUY" if chosen["direction"] == "LONG" else "SELL",
            "active_entry": self._price(chosen["fill_price"]),
            "active_stop": self._price(chosen["stop"]),
            "active_tp1": self._price(targets[0]),
            "active_tp2": self._price(targets[1]),
            "active_tp3": self._price(targets[2]),
            "active_hit": hit,
            "active_children": f"{len(live)}/{len(planned) or len(live)}",
            "active_r": self._risk_r(chosen, price),
            "active_min": minutes,
        }

    def write(self, **kwargs: Any) -> bool:
        """Replace the file; ``False`` (and a log line) when it could not be written."""
        try:
            text = self.content(**kwargs)
            self.folder.mkdir(parents=True, exist_ok=True)
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
            log.warning("scalp panel file not written", path=str(self.path), error=str(exc))
            return False

    # -- the buttons ---------------------------------------------------------------------- #

    def read_commands(self) -> list[PanelCommand]:
        """The commands in ``scalp_commands.json`` (malformed lines are skipped)."""
        try:
            text = self.commands_path.read_text(encoding="ascii", errors="replace")
        except OSError:
            return []
        found: list[PanelCommand] = []
        for raw in text.splitlines():
            try:
                item = json.loads(raw)
                found.append(
                    PanelCommand(
                        int(item["id"]),
                        dt.datetime.fromtimestamp(int(item["t"]), tz=dt.UTC),
                        str(item["cmd"]).strip().lower(),
                    )
                )
            except (ValueError, KeyError, TypeError):
                continue
        return found

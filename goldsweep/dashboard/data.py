"""What the dashboard shows, read from the bot's SQLite database (Phase 7).

The dashboard never talks to MetaTrader 5 and never trades. The running bot
(``--paper`` or ``--live``) stores what it sees: the M5 candles, a snapshot of
the market (price, pools, H1 bias, pressure meter, news bias) and of its trading
(state, equity, positions, entry orders, risk counters), signals, trades,
headlines, scenario briefs and calendar events. Everything here reads those
tables. The one thing the dashboard writes is a command (pause, resume, stop)
into the ``commands`` table, which the bot carries out within a few seconds,
so only the bot ever sends orders.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from goldsweep.analysis.indicators import atr_values
from goldsweep.analysis.structure import Direction
from goldsweep.analysis.zones import Zone, find_fvgs
from goldsweep.core.clock import ensure_utc
from goldsweep.core.settings import Settings
from goldsweep.core.types import Timeframe
from goldsweep.data.calendar import CalendarService, EconomicEvent, is_blackout_event
from goldsweep.execution.book import third_target
from goldsweep.live import COMMAND_MAX_AGE, DASHBOARD_COMMANDS, HEARTBEAT_KEY, SNAPSHOT_KEY
from goldsweep.storage.db import Database

#: The bot writes a heartbeat every 10 seconds; older than this means it is not running.
ALIVE_WITHIN = dt.timedelta(seconds=60)
SHOWN_SIGNAL_ACTIONS = ("traded", "alert", "blocked")


def _utc(epoch: float | int | None) -> dt.datetime | None:
    return None if epoch is None else dt.datetime.fromtimestamp(int(epoch), tz=dt.UTC)


def _epoch(moment: dt.datetime) -> int:
    return int(ensure_utc(moment).timestamp())


# --------------------------------------------------------------------------- #
# Is the bot running, and what does it see?
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BotStatus:
    alive: bool
    seen: dt.datetime | None  # the last heartbeat
    mode: str | None  # PAPER / LIVE DEMO / LIVE
    state: str | None  # active / paused / stopped

    def describe(self, now: dt.datetime) -> str:
        if self.seen is None:
            return "The bot has not run with the dashboard yet: start --paper or --live."
        ago = max(0, int((ensure_utc(now) - self.seen).total_seconds()))
        if self.alive:
            return f"{self.mode} bot running, trading {self.state} (seen {ago} s ago)"
        return f"The bot is NOT running (last seen {ago // 60} min ago)."


def snapshot(db: Database) -> dict[str, Any] | None:
    """The bot's latest market and trading picture, or ``None`` before its first bar."""
    value = db.get_state(SNAPSHOT_KEY)
    return value if isinstance(value, dict) else None


def bot_status(db: Database, now: dt.datetime) -> BotStatus:
    beat = db.get_state(HEARTBEAT_KEY)
    picture = snapshot(db) or {}
    if not isinstance(beat, dict) or "time" not in beat:
        return BotStatus(False, None, picture.get("mode"), picture.get("state"))
    seen = dt.datetime.fromisoformat(beat["time"])
    alive = ensure_utc(now) - seen <= ALIVE_WITHIN
    return BotStatus(alive, seen, beat.get("mode") or picture.get("mode"), picture.get("state"))


# --------------------------------------------------------------------------- #
# The chart
# --------------------------------------------------------------------------- #


def candles(db: Database, symbol: str, bars: int = 144) -> pd.DataFrame:
    """The latest stored M5 bars (the bot keeps the last 300)."""
    return db.load_candles(symbol, Timeframe.M5, limit=bars)


def open_gaps(
    frame: pd.DataFrame, *, atr_period: int = 14, min_size_atr: float = 0.05, keep: int = 8
) -> list[Zone]:
    """Fair value gaps on these bars that price has not traded back through, newest last.

    A bullish gap is closed once a later low reaches its bottom; a bearish gap
    once a later high reaches its top.
    """
    if len(frame) < 3:
        return []
    atr = atr_values(
        frame["high"].to_numpy(), frame["low"].to_numpy(), frame["close"].to_numpy(), atr_period
    )
    highs, lows = frame["high"].to_numpy(), frame["low"].to_numpy()
    found: list[Zone] = []
    for direction in (Direction.LONG, Direction.SHORT):
        for zone in find_fvgs(frame, atr, direction, min_size_atr=min_size_atr):
            later = slice(zone.known_index + 1, None)
            if direction is Direction.LONG:
                closed = bool(np.any(lows[later] <= zone.bottom))
            else:
                closed = bool(np.any(highs[later] >= zone.top))
            if not closed:
                found.append(zone)
    found.sort(key=lambda z: z.time)
    return found[-keep:]


# --------------------------------------------------------------------------- #
# Signals, trades, equity
# --------------------------------------------------------------------------- #


def signals(db: Database, since: dt.datetime, tp3_r: float = 3.0) -> pd.DataFrame:
    """Alerts, trades and blocked setups since ``since``, newest first."""
    rows = db.query(
        "SELECT id, time, direction, session, score, entry, stop_loss, take_profit_1, "
        "take_profit_2, risk_reward, action, detail FROM signals "
        "WHERE time >= ? AND action IN (?, ?, ?) ORDER BY time DESC",
        (_epoch(since), *SHOWN_SIGNAL_ACTIONS),
    )
    records = []
    for sid, at, direction, session, score, entry, sl, tp1, tp2, rr, action, detail in rows:
        extra = json.loads(detail or "{}")
        tp3 = extra.get("take_profit_3")
        if tp3 is None:
            tp3 = third_target(Direction(direction), entry, sl, (tp1, tp2), tp3_r)
        records.append(
            {
                "time": _utc(at),
                "id": sid,
                "direction": direction,
                "session": session,
                "score": score,
                "entry": entry,
                "stop_loss": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "rr": rr,
                "action": action,
                "pool": extra.get("pool"),
                "pool_price": extra.get("pool_price"),
                "what_happened": extra.get("action"),
            }
        )
    columns = [
        "time", "id", "direction", "session", "score", "entry", "stop_loss", "tp1", "tp2",
        "tp3", "rr", "action", "pool", "pool_price", "what_happened",
    ]  # fmt: skip
    return pd.DataFrame.from_records(records, columns=columns)


def trades(db: Database, *, live: bool, since: dt.datetime | None = None) -> pd.DataFrame:
    """Closed trades (``live_trades`` or ``paper_trades``), oldest first."""
    table = "live_trades" if live else "paper_trades"
    rows = db.query(
        f"SELECT id, direction, session, fill_time, exit_time, fill_price, stop_loss, lots, "
        f"exit_reason, net_pnl, r_multiple, legs FROM {table} WHERE exit_time >= ? "
        "ORDER BY exit_time",
        (_epoch(since) if since else 0,),
    )
    records = [
        {
            "id": r[0],
            "direction": r[1],
            "session": r[2],
            "opened": _utc(r[3]),
            "closed": _utc(r[4]),
            "fill_price": r[5],
            "stop_loss": r[6],
            "lots": r[7],
            "exit": r[8],
            "net_pnl": r[9],
            "r": r[10],
            "parts": len(json.loads(r[11] or "[]")),
        }
        for r in rows
    ]
    columns = [
        "id", "direction", "session", "opened", "closed", "fill_price", "stop_loss", "lots",
        "exit", "net_pnl", "r", "parts",
    ]  # fmt: skip
    return pd.DataFrame.from_records(records, columns=columns)


def equity_curve(closed: pd.DataFrame, end_equity: float | None) -> pd.DataFrame:
    """Equity after each closed trade, ending at today's equity when it is known.

    The start is worked back from the end (equity minus every trade's result),
    so the curve needs no separate record of the opening balance.
    """
    if closed.empty:
        return pd.DataFrame(columns=["time", "equity"])
    pnl = closed["net_pnl"].astype(float)
    final = float(end_equity) if end_equity is not None else float(pnl.sum())
    start = final - float(pnl.sum())
    points = [{"time": closed["opened"].iloc[0], "equity": start}]
    running = start
    for when, result in zip(closed["closed"], pnl, strict=True):
        running += result
        points.append({"time": when, "equity": running})
    return pd.DataFrame(points)


@dataclass(frozen=True)
class Stats:
    trades: int
    wins: int
    losses: int
    net: float
    total_r: float
    win_rate: float | None
    profit_factor: float | None
    average_r: float | None


def trade_stats(closed: pd.DataFrame) -> Stats:
    """Plain figures from the closed trades. Past results do not predict future ones."""
    if closed.empty:
        return Stats(0, 0, 0, 0.0, 0.0, None, None, None)
    pnl = closed["net_pnl"].astype(float)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    gross_win, gross_loss = float(pnl[pnl > 0].sum()), float(-pnl[pnl < 0].sum())
    return Stats(
        trades=len(closed),
        wins=wins,
        losses=losses,
        net=float(pnl.sum()),
        total_r=float(closed["r"].astype(float).sum()),
        win_rate=wins / len(closed),
        profit_factor=gross_win / gross_loss if gross_loss > 0 else None,
        average_r=float(closed["r"].astype(float).mean()),
    )


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #


def headlines(db: Database, since: dt.datetime, limit: int = 60) -> pd.DataFrame:
    """Recent headlines with their AI (or keyword-rule) tags, newest first."""
    rows = db.query(
        "SELECT published, source, title, relevant, direction, impact, drivers, confidence, "
        "reason, method, url FROM headlines WHERE published >= ? ORDER BY published DESC LIMIT ?",
        (_epoch(since), int(limit)),
    )
    records = [
        {
            "time": _utc(r[0]),
            "source": r[1],
            "headline": r[2],
            "gold": None if r[3] is None else bool(r[3]),
            "direction": r[4],
            "impact": r[5],
            "drivers": ", ".join(json.loads(r[6])) if r[6] else "",
            "confidence": r[7],
            "why": r[8],
            "tagged_by": {"llm": "AI", "rules": "keywords"}.get(r[9] or "", r[9] or "pending"),
            "url": r[10],
        }
        for r in rows
    ]
    columns = [
        "time", "source", "headline", "gold", "direction", "impact", "drivers", "confidence",
        "why", "tagged_by", "url",
    ]  # fmt: skip
    return pd.DataFrame.from_records(records, columns=columns)


@dataclass(frozen=True)
class Brief:
    key: str
    time: dt.datetime
    trigger: str
    method: str
    cases: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def scenario_briefs(db: Database, since: dt.datetime) -> list[Brief]:
    rows = db.query(
        "SELECT key, time, trigger, method, body FROM scenarios WHERE time >= ? ORDER BY time DESC",
        (_epoch(since),),
    )
    briefs = []
    for key, at, trigger, method, body in rows:
        data = json.loads(body or "{}")
        cases = [(c.get("name", ""), c.get("text", "")) for c in data.get("cases", [])]
        briefs.append(Brief(key, _utc(at), trigger, method, cases, list(data.get("notes", []))))
    return briefs


def upcoming_releases(
    db: Database, settings: Settings, now: dt.datetime, hours: int = 48
) -> list[EconomicEvent]:
    """The stored calendar's releases that block new entries, from 1 h ago on."""
    news = settings.config.news
    service = CalendarService([], db, news)
    instant = ensure_utc(now)
    events = service.events(instant - dt.timedelta(hours=1), instant + dt.timedelta(hours=hours))
    return [event for event in events if is_blackout_event(event, news)]


def llm_today(db: Database, now: dt.datetime) -> dict[str, Any]:
    day = ensure_utc(now).date().isoformat()
    (calls, tokens_in, tokens_out, cost) = db.query(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens + cache_read_tokens), 0), "
        "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE day = ?",
        (day,),
    )[0]
    return {"day": day, "calls": calls, "input": tokens_in, "output": tokens_out, "cost": cost}


# --------------------------------------------------------------------------- #
# Backtest reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Report:
    name: str
    folder: Path
    summary: dict[str, Any]
    modified: dt.datetime

    @property
    def html(self) -> Path:
        return self.folder / "report.html"


def backtest_reports(reports_dir: Path) -> list[Report]:
    """Every backtest report folder (with a summary.json), newest first."""
    found = []
    if not reports_dir.is_dir():
        return found
    for summary in reports_dir.glob("*/summary.json"):
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        modified = dt.datetime.fromtimestamp(summary.stat().st_mtime, tz=dt.UTC)
        found.append(Report(summary.parent.name, summary.parent, data, modified))
    return sorted(found, key=lambda r: r.modified, reverse=True)


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested summary values as ``a.b.c`` keys (for a plain table)."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        elif not isinstance(value, list):
            flat[name] = value
    return flat


# --------------------------------------------------------------------------- #
# Commands for the running bot
# --------------------------------------------------------------------------- #


def send_command(db: Database, name: str, now: dt.datetime) -> int:
    """Queue a command for the running bot; returns its id."""
    if name not in DASHBOARD_COMMANDS:
        raise ValueError(f"unknown command {name!r}")
    db.execute(
        "INSERT INTO commands (created, source, name) VALUES (?, 'dashboard', ?)",
        (_epoch(now), name),
    )
    return int(db.query("SELECT MAX(id) FROM commands")[0][0])


@dataclass(frozen=True)
class CommandResult:
    status: str  # pending / done / expired
    reply: str | None


def command_result(db: Database, command_id: int, now: dt.datetime) -> CommandResult:
    rows = db.query("SELECT status, reply, created FROM commands WHERE id = ?", (command_id,))
    if not rows:
        return CommandResult("missing", None)
    status, reply, created = rows[0]
    if status == "pending" and _epoch(now) - created > COMMAND_MAX_AGE.total_seconds():
        return CommandResult("expired", "the bot did not pick it up: is it running?")
    return CommandResult(status, reply)


def recent_orders(db: Database, limit: int = 30) -> pd.DataFrame:
    """The latest order requests to MT5 and the broker's answers (live trading)."""
    rows = db.query(
        "SELECT time, signal_id, action, ok, retcode, ticket, message FROM order_log "
        "ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
    records = [
        {
            "time": _utc(r[0]),
            "signal": r[1],
            "action": r[2],
            "ok": bool(r[3]),
            "answer": r[6],
            "ticket": r[5],
        }
        for r in rows
    ]
    return pd.DataFrame.from_records(
        records, columns=["time", "signal", "action", "ok", "answer", "ticket"]
    )


def money(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{'+' if value >= 0 else '-'}{abs(value):,.2f}"


def to_local(moment: dt.datetime | None, zone: dt.tzinfo) -> str:
    if moment is None:
        return "-"
    return f"{ensure_utc(moment).astimezone(zone):%a %d %b %H:%M}"


def times_in(frame: pd.DataFrame, columns: Sequence[str], zone: dt.tzinfo) -> pd.DataFrame:
    """A copy with these UTC time columns shown in ``zone`` (for tables)."""
    shown = frame.copy()
    for column in columns:
        if column in shown:
            shown[column] = [to_local(value, zone) for value in shown[column]]
    return shown

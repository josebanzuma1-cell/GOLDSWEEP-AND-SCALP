"""Alert texts (spec section 8), plain text so nothing needs escaping.

A setup alert looks like::

    🟡 XAUUSD LONG setup (score 81.1) — London kill zone
    Swept: Asian low 2027.00 → MSS confirmed (M5)
    Entry: 2028.90 (FVG mid, limit) | SL: 2026.09 | TP1: 2036.00 | TP2: 2048.00
    R:R 2.5 / 6.8 | Risk 0.5% (0.17 lots)
    News bias: +0.26 (Gold jumps as Fed signals rate cuts, 50m ago) | Next high-impact: ...
    Pressure: Buyers 58% / Sellers 42% (buyers)
    Action: paper order placed, valid until 08:25 UTC
    Mode: PAPER  |  id: 2026-11-17-LDN-01

The texts describe what happened and what is planned. They never promise an
outcome: every trade can lose.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from goldsweep.ai.news_bias import NewsBias
from goldsweep.ai.scenarios import ScenarioBrief
from goldsweep.analysis.pressure import PressureReading
from goldsweep.analysis.signals import POST_NEWS, Signal
from goldsweep.analysis.zones import ZoneKind
from goldsweep.core.clock import SESSION_LABELS
from goldsweep.data.calendar import EconomicEvent
from goldsweep.execution.book import BookEvent, EventKind, ExitReason, TradePlan

_ZONE_NAMES = {ZoneKind.FVG: "FVG", ZoneKind.IFVG: "IFVG", ZoneKind.ORDER_BLOCK: "order block"}
_ENTRY_SPOT = {"fvg_mid": "mid", "fvg_top": "top", "fvg_bottom": "bottom"}


def session_name(session: str | None) -> str:
    if session == POST_NEWS:
        return "post-news window"
    return SESSION_LABELS.get(session or "", "outside the kill zones")


def duration(delta: dt.timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _money(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _price(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def entry_text(signal: Signal) -> str:
    if signal.entry_mode == "market":
        return "market"
    zone = _ZONE_NAMES.get(signal.zone.kind, "zone") if signal.zone is not None else "zone"
    return f"{zone} {_ENTRY_SPOT.get(signal.entry_mode, signal.entry_mode)}, limit"


def news_line(bias: NewsBias | None, next_event: EconomicEvent | None, now: dt.datetime) -> str:
    parts = []
    if bias is not None:
        text = f"News bias: {bias.score:+.2f}"
        if bias.top:
            item = bias.top[0][0]
            text += f" ({item.text[:70]}, {duration(now - item.time)} ago)"
        parts.append(text)
    if next_event is not None:
        parts.append(
            f"Next high-impact: {next_event.currency} {next_event.title} in "
            f"{duration(next_event.time - now)}"
        )
    return " | ".join(parts) if parts else "News bias: n/a"


def signal_alert(
    signal: Signal,
    *,
    action: str,
    traded: bool,
    blocked: bool,
    lots: float | None,
    risk_pct: float,
    pressure: PressureReading | None,
    bias: NewsBias | None,
    next_event: EconomicEvent | None,
    now: dt.datetime,
    mode: str,
    take_profit_3: float | None = None,
) -> str:
    icon = "🟢" if traded else ("⛔" if blocked else "🟡")
    rr2 = f" / {signal.risk_reward_2:.1f}" if signal.risk_reward_2 is not None else ""
    risk = f"Risk {risk_pct:g}%" + (f" ({lots:g} lots)" if lots else "")
    lines = [
        f"{icon} {signal.symbol} {signal.direction.value} setup (score {signal.score:.1f}) "
        f"— {session_name(signal.session)}",
        f"Swept: {signal.pool.label} {signal.pool.price:.2f} → MSS confirmed (M5)",
        f"Entry: {signal.entry:.2f} ({entry_text(signal)}) | SL: {signal.stop_loss:.2f} | "
        f"TP1: {signal.take_profit_1:.2f} | TP2: {_price(signal.take_profit_2)}"
        + (f" | TP3: {take_profit_3:.2f}" if take_profit_3 is not None else ""),
        f"R:R {signal.risk_reward_1:.1f}{rr2} | {risk}",
        news_line(bias, next_event, now),
    ]
    if pressure is not None:
        lines.append(f"Pressure: {pressure.describe()}")
    lines.append(f"Action: {action}")
    lines.append(f"Mode: {mode}  |  id: {signal.id}")
    return "\n".join(lines)


def trade_message(event: BookEvent, mode: str) -> str | None:
    """A message for each trade event worth telling the owner about."""
    plan: TradePlan = event.plan
    head = f"{mode} {plan.direction.value} {plan.id}"
    if event.kind is EventKind.FILLED:
        targets = " | ".join(
            f"TP{i} {target:.2f}" for i, target in enumerate(plan.targets, 1) if target is not None
        )
        parts = f" in {event.detail}" if event.detail else ""
        return (
            f"✅ {head} filled at {_price(event.price)}, {event.lots:g} lots{parts} | "
            f"SL {plan.stop_loss:.2f} | {targets}"
        )
    if event.kind is EventKind.LEG_CLOSED and event.leg is not None:
        leg = event.leg
        return (
            f"💰 {head}: {leg.reason.value.upper()} hit, {leg.lots:g} lots closed at "
            f"{leg.exit_price:.2f} ({_money(leg.pnl)}); the rest stays open"
        )
    if event.kind is EventKind.STOP_MOVED:
        what = "to breakeven" if event.detail == ExitReason.BREAKEVEN.value else "(trailed)"
        why = f" ({event.note})" if event.note else ""
        return f"🛡 {head}: stop moved {what} at {_price(event.price)}{why}"
    if event.kind is EventKind.CLOSED and event.trade is not None:
        trade = event.trade
        icon = "🏁" if trade.net_pnl >= 0 else "🔴"
        return (
            f"{icon} {head} closed ({trade.exit_reason.value}) at "
            f"{trade.legs[-1].exit_price:.2f}: {_money(trade.net_pnl)} "
            f"({trade.r_multiple:+.2f} R) | equity {event.equity or 0:,.2f}"
        )
    if event.kind in (EventKind.EXPIRED, EventKind.TP1_FIRST, EventKind.CANCELLED):
        return f"⌛ {head}: entry order withdrawn ({event.detail})"
    return None


def pre_news_warning(event: EconomicEvent, now: dt.datetime, before: int, after: int) -> str:
    figures = []
    if event.forecast is not None:
        figures.append(f"forecast {event.forecast}")
    if event.previous is not None:
        figures.append(f"previous {event.previous}")
    start = event.time - dt.timedelta(minutes=before)
    end = event.time + dt.timedelta(minutes=after)
    detail = f" ({', '.join(figures)})" if figures else ""
    return (
        f"⚠️ {event.currency} {event.title} in {duration(event.time - now)} "
        f"({event.time:%H:%M} UTC){detail}. No new entries {start:%H:%M}-{end:%H:%M} UTC."
    )


def scenario_message(brief: ScenarioBrief) -> str:
    return "🧭 " + "\n".join(brief.describe())


def daily_summary(
    *,
    day: str,
    mode: str,
    trades: Sequence[Any],
    equity: float,
    signals: dict[str, int],
    bias: NewsBias | None,
    llm_cost: float,
) -> str:
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = sum(1 for t in trades if t.net_pnl < 0)
    net = sum(t.net_pnl for t in trades)
    total_r = sum(t.r_multiple for t in trades)
    signal_text = ", ".join(f"{k} {v}" for k, v in sorted(signals.items())) or "none"
    lines = [
        f"📊 Daily summary {day} ({mode})",
        f"Trades: {len(trades)} ({wins} won, {losses} lost) | net {_money(net)} "
        f"({total_r:+.2f} R) | equity {equity:,.2f}",
        f"Signals: {signal_text}",
    ]
    for trade in trades:
        lines.append(
            f"  {trade.id} {trade.direction} {trade.exit_reason}: {_money(trade.net_pnl)} "
            f"({trade.r_multiple:+.2f} R)"
        )
    lines.append(f"News bias: {bias.score:+.2f}" if bias is not None else "News bias: n/a")
    lines.append(f"AI cost today: about US${llm_cost:.4f}")
    lines.append("Results vary from day to day; losses are part of the plan.")
    return "\n".join(lines)


def status_message(
    *,
    mode: str,
    state: str,
    equity: float,
    positions: Sequence[dict[str, Any]],
    orders: Sequence[TradePlan],
    today_trades: Sequence[Any],
    bias: NewsBias | None,
    next_window: str,
    llm: str,
) -> str:
    lines = [
        f"GoldSweep {mode} | trading {state}",
        f"Equity {equity:,.2f} | today {len(today_trades)} trade(s), "
        f"{_money(sum(t.net_pnl for t in today_trades))}",
    ]
    for p in positions:
        lines.append(
            f"Open: {p['direction']} {p['id']} {p['lots']:g} lots from {p['fill_price']:.2f}, "
            f"stop {p['stop']:.2f}"
        )
    for plan in orders:
        lines.append(
            f"Waiting: {plan.direction.value} {plan.id} limit {plan.entry:.2f} until "
            f"{plan.expires_at:%H:%M} UTC"
        )
    if not positions and not orders:
        lines.append("No open positions or entry orders.")
    lines.append(f"News bias: {bias.score:+.2f}" if bias is not None else "News bias: n/a")
    lines.append(f"Next window: {next_window}")
    lines.append(f"AI: {llm}")
    return "\n".join(lines)


HELP = (
    "Commands: /status (state, equity, positions), /pause (no new trades; open trades "
    "stay managed), /resume, /stop (kill switch: close everything and stop trading "
    "until /resume), /golive <account> (after an unattended start: allow live orders), "
    "/help"
)

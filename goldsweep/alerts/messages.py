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
from goldsweep.analysis.refine import RefineSetup
from goldsweep.analysis.signals import POST_NEWS, Signal
from goldsweep.analysis.zones import ZoneKind
from goldsweep.core.clock import SESSION_LABELS
from goldsweep.data.calendar import EconomicEvent
from goldsweep.execution.book import BookEvent, EventKind, ExitReason, Outcome, TradePlan

_ZONE_NAMES = {ZoneKind.FVG: "FVG", ZoneKind.IFVG: "IFVG", ZoneKind.ORDER_BLOCK: "order block"}
_ENTRY_SPOT = {
    "fvg_mid": "mid",
    "fvg_top": "top",
    "fvg_bottom": "bottom",
    "fvg_near": "near edge",
    "fvg_far": "far edge",
    "ote": "OTE",
}


_MODEL_NAMES = {
    "sweep_mss": "sweep + MSS",
    "cisd": "CISD",
    "continuation": "continuation",
    "breakout": "breakout",
}
_TAG_NAMES = {
    "smt": "SMT (silver held)",
    "unicorn": "Unicorn (gap on a breaker)",
    "bpr": "balanced price range",
    "judas": "Judas swing (beyond the NY midnight open)",
    "silver_bullet": "Silver Bullet hour",
    "ote": "OTE (62-79% pullback)",
    "d1_trend": "daily trend",
    "h4_trend": "4-hour trend",
    "ema_trend": "H1 200 EMA side",
    "weekly_value": "weekly discount/premium",
    "high_vol": "high volatility",
}


def model_name(model: str) -> str:
    return _MODEL_NAMES.get(model, model)


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
    plan: TradePlan | None = None,
    grade: str | None = None,
    monitoring: str | None = None,
    manual: bool = False,
) -> str:
    """``plan``, when given, supplies the targets actually used (fixed-R targets, D84).

    ``grade`` is the setup's grade (A+ / A / B, D86) and ``monitoring`` says whether
    it went on the shadow book for its session's A+ candidate (D87). ``manual`` (signals
    only, S11): the owner places the trade; ``lots`` is then a suggestion.
    """
    icon = "📲" if manual else ("🟢" if traded else ("⛔" if blocked else "🟡"))
    tp1, tp2, rr1 = signal.take_profit_1, signal.take_profit_2, signal.risk_reward_1
    rr2_value = signal.risk_reward_2
    if plan is not None:
        tp1, tp2, take_profit_3 = plan.take_profit_1, plan.take_profit_2, plan.take_profit_3
        rr1 = plan.risk_reward_1
        rr2_value = abs(tp2 - plan.entry) / plan.risk_distance if tp2 is not None else None
    rr2 = f" / {rr2_value:.1f}" if rr2_value is not None else ""
    if manual:
        risk = f"Risk {risk_pct:g}%" + (f": about {lots:g} lots on your account" if lots else "")
    else:
        risk = f"Risk {risk_pct:g}%" + (f" ({lots:g} lots)" if lots else "")
    model = "" if signal.model == "sweep_mss" else f" [{model_name(signal.model)} model]"
    head = f"MAIN SIGNAL {grade.split(' ')[0]} " if manual and grade else ""
    lines = [
        f"{icon} {head}{signal.symbol} {signal.direction.value} setup{model} "
        f"(score {signal.score:.1f}) — {session_name(signal.session)}",
        *([f"Type: {SIGNAL_TYPES['main']}"] if manual else []),
        f"Swept: {signal.pool.label} {signal.pool.price:.2f} → MSS confirmed (M5)",
        f"Entry: {signal.entry:.2f} ({entry_text(signal)}) | SL: {signal.stop_loss:.2f} | "
        f"TP1: {tp1:.2f} | TP2: {_price(tp2)}"
        + (f" | TP3: {take_profit_3:.2f}" if take_profit_3 is not None else ""),
        f"R:R {rr1:.1f}{rr2} | {risk}",
        news_line(bias, next_event, now),
    ]
    if pressure is not None:
        lines.append(f"Pressure: {pressure.describe()}")
    if signal.tags:
        lines.append("Confluence: " + ", ".join(_TAG_NAMES.get(t, t) for t in signal.tags))
    if grade:
        lines.append(f"Grade: {grade}")
    lines.append(f"Action: {action}")
    if monitoring:
        lines.append(f"👀 {monitoring}")
    lines.append(f"Mode: {mode}  |  id: {signal.id}")
    return "\n".join(lines)


def watch_message(watch: Any, event: str) -> str:
    """A manual main setup's next step (S11, D118): a
    :class:`~goldsweep.execution.signal_watch.SignalWatch` and what just happened."""
    side = "BUY" if watch.direction.value == "LONG" else "SELL"
    name = f"Main {side} {watch.id} ({watch.grade})"
    targets = " | ".join(
        f"TP{n} {t:.2f}"
        for n, t in enumerate((watch.take_profit_1, watch.take_profit_2, watch.take_profit_3), 1)
        if t is not None
    )
    if event == "entry":
        return (
            f"🎯 {name}: price is at the entry zone now ({watch.entry:.2f}), the retest.\n"
            f"Manual trade: enter if you have not | SL {watch.stop:.2f} | {targets}"
        )
    if event == "tp1":
        return (
            f"✅ {name}: TP1 {watch.take_profit_1:.2f} reached. Take part profit and move "
            f"your stop to breakeven ({watch.entry:.2f})."
        )
    if event == "stopped":
        return f"🔴 {name}: the stop {watch.stop:.2f} was hit."
    if event == "expired":
        return (
            f"⌛ {name}: no retest of {watch.entry:.2f} before {watch.expires_at:%H:%M} UTC. "
            "The setup expired: do not enter."
        )
    if event == "missed":
        return (
            f"❌ {name}: price reached TP1 without coming back to {watch.entry:.2f}. "
            "Missed: do not chase it."
        )
    return f"❌ {name}: price crossed the stop {watch.stop:.2f} before the retest: setup failed."


def tally_text(tally: Any) -> str:
    """``7 finished, 3 won, +0.42 R average`` (a :class:`~goldsweep.execution.shadow.Tally`)."""
    if not tally.finished:
        return "0 finished"
    return f"{tally.finished} finished, {tally.won} won, {tally.average_r:+.2f} R average"


def approval_request(playbook: str, playbook_name: str, rule: str, tally: Any) -> str:
    """The candidate has its monitored trades with a positive average: ask the owner."""
    return (
        f"✅ {playbook_name} A+ playbook confirmed in monitoring: {tally_text(tally)}.\n"
        f"Rule: {rule}.\n"
        f"Send /approve {playbook} to let it trade. Until then it is only monitored. "
        "Past results do not guarantee future ones; every trade can lose."
    )


def refined_entry(
    setup: RefineSetup,
    plan: TradePlan,
    *,
    symbol: str,
    action: str,
    traded: bool,
    mode: str,
    risk_pct: float,
) -> str:
    """The M1 turn came inside the gap (D83): the refined order and what was done."""
    icon = "🎯" if traded else "⛔"
    how = "market" if plan.entry_mode == "market" else "M1 FVG mid, limit"
    lines = [
        f"{icon} {symbol} {plan.direction.value} sniper entry (M1) — {setup.id}",
        f"Entry: {plan.entry:.2f} ({how}) | SL: {plan.stop_loss:.2f} (M5 stop was "
        f"{setup.stop_loss:.2f}) | TP1: {plan.take_profit_1:.2f} | TP2: "
        f"{_price(plan.take_profit_2)}"
        + (f" | TP3: {plan.take_profit_3:.2f}" if plan.take_profit_3 is not None else ""),
        f"R:R {plan.risk_reward_1:.1f} | Risk {risk_pct:g}%",
        f"Action: {action}",
        f"Mode: {mode}",
    ]
    return "\n".join(lines)


def refine_dropped(setup: RefineSetup, detail: str, mode: str) -> str:
    """An armed setup ends without an M1 entry (D83)."""
    return f"⌛ {mode} {setup.direction.value} {setup.id}: no sniper entry ({detail})"


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
        if event.outcome is Outcome.WITHDRAWN:
            return f"⌛ {head}: entry order {event.detail}"  # "withdrawn before ..."
        return f"⌛ {head}: entry order withdrawn ({event.detail})"
    return None


_SCALP_WHERE = {"paper": "paper", "demo": "demo account"}


def scalp_placed(plan: TradePlan, event: BookEvent, mode: str, where: str, reason: str) -> str:
    """A scalp sent (quiet messages, D97: this one and the close)."""
    targets = " / ".join(f"{t:.2f}" for t in plan.targets if t is not None)
    lots = f", {event.lots:g} lots" if event.lots else ""
    if plan.risk_pct is not None:
        lots += f", {plan.risk_pct:.3g}% risk"
    place = _SCALP_WHERE.get(where, where)
    return (
        f"⚡ Scalp {mode.upper()} {plan.direction.value} {plan.id} ({place}{lots}): "
        f"market | SL {plan.stop_loss:.2f} | TP {targets}\n{reason}"
    )


#: What each kind of signal is, said plainly in every manual signal (S11, D118).
SIGNAL_TYPES = {
    "main": "MAIN TRADE: M5 liquidity sweep, held for hours (TP1-TP3)",
    "hft": "HFT SCALP: 1-minute chart, held seconds to 15 minutes (TP1-TP5)",
    "intraday": "INTRADAY SCALP: 5-minute chart, closed by the New York close (TP1-TP3)",
    "swing": "SWING SCALP: 15-minute chart, held hours to two days (TP1-TP3)",
}
_MODE_NAMES = {"hft": "HFT", "intraday": "INTRADAY", "swing": "SWING"}


def scalp_signal(
    plan: TradePlan,
    mode: str,
    grade: Any,
    analysis: Any,
    *,
    reason: str,
    retest: float | None,
    lots: float | None,
    valid_min: int,
) -> str:
    """A scalp to take by hand (signals only, S11): its kind, grade, retest entry, SL and
    every TP, the suggested size and the market picture (a
    :class:`~goldsweep.scalping.grading.Analysis`)."""
    side = "BUY" if plan.direction.value == "LONG" else "SELL"
    sign = 1.0 if side == "BUY" else -1.0
    targets = " | ".join(f"TP{n} {t:.2f}" for n, t in enumerate(plan.targets, 1) if t is not None)
    late = plan.entry + sign * 0.5 * abs(plan.take_profit_1 - plan.entry)
    pct = plan.risk_pct
    risk = f"Risk {pct:.2g}%" if pct is not None else "Risk 0.25%"
    if lots:
        risk += f": about {lots:g} lots on your account"
    lines = [
        f"📲 SCALP SIGNAL {_MODE_NAMES.get(mode, mode.upper())} {grade.letter} {side} XAUUSD "
        "(manual trade)",
        f"Type: {SIGNAL_TYPES.get(mode, mode)}",
        (
            f"Enter now ~{plan.entry:.2f}: retest of {retest:.2f} confirmed"
            if retest is not None
            else f"Enter now ~{plan.entry:.2f}"
        ),
        f"SL {plan.stop_loss:.2f} | {targets}",
        risk,
        f"Why {grade.letter}: {'; '.join(grade.reasons)}",
        f"Setup: {reason}",
        f"Market: H1 {analysis.bias_text}"
        + (f" | news bias {analysis.news_bias:+.2f}" if analysis.news_bias is not None else "")
        + (f" | pressure {analysis.pressure} (estimate)" if analysis.pressure else "")
        + (f" | next: {analysis.next_event}" if analysis.next_event else ""),
    ]
    levels = []
    if analysis.above:
        levels.append("above " + ", ".join(f"{p:.2f} ({label})" for p, label in analysis.above[:2]))
    if analysis.below:
        levels.append("below " + ", ".join(f"{p:.2f} ({label})" for p, label in analysis.below[:2]))
    if levels:
        lines.append("Levels: " + " | ".join(levels))
    lines += [
        f"Valid {valid_min} min: skip it if price is already past {late:.2f}. "
        "After TP1, move your stop to breakeven.",
        f"id: {plan.id}",
    ]
    return "\n".join(lines)


def scalp_signal_result(trade: Any, mode: str) -> str:
    """How a scalp signal ended, as tracked on paper (signals only, S11)."""
    icon = "📊" if trade.net_pnl >= 0 else "🔴"
    side = "BUY" if trade.plan.direction.value == "LONG" else "SELL"
    return (
        f"{icon} Signal result (tracked on paper): {_MODE_NAMES.get(mode, mode)} {side} "
        f"{trade.plan.id} ended at {trade.exit_reason.value}, {trade.r_multiple:+.2f} R "
        f"after {duration(trade.exit_time - trade.fill_time)}"
    )


def breakeven_reminder(plan: TradePlan, mode: str, stop: float | None) -> str:
    """TP1 reached on a scalp signal: move the stop to breakeven (signals only, S11)."""
    side = "BUY" if plan.direction.value == "LONG" else "SELL"
    level = f" ({stop:.2f}, just past the entry to cover costs)" if stop is not None else ""
    return (
        f"✅ {_MODE_NAMES.get(mode, mode)} {side} {plan.id}: TP1 {plan.take_profit_1:.2f} "
        f"reached. Take part profit and move your stop to breakeven{level}."
    )


def scalp_closed(trade: Any, mode: str, where: str) -> str:
    """A scalp finished: every child closed."""
    icon = "🏁" if trade.net_pnl >= 0 else "🔴"
    held = duration(trade.exit_time - trade.fill_time)
    return (
        f"{icon} Scalp {mode.upper()} {trade.plan.id} closed ({trade.exit_reason.value}, "
        f"{_SCALP_WHERE.get(where, where)}) after {held}: {_money(trade.net_pnl)} "
        f"({trade.r_multiple:+.2f} R)"
    )


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
    "/approve <session> (a session's A+ playbook trades, after its monitoring), "
    "/revoke <session> (back to monitoring), /mode main|scalper|both (which strategies "
    "may trade), /scalper off|hft|intraday|swing (the scalper's mode, on paper), "
    "/approve scalper <mode> (its scalps go to the demo account), /revoke scalper <mode> "
    "(back to paper), /stats [days] (results per scalper mode and of the main strategy), /help"
)

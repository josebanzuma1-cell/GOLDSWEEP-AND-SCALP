"""The GoldSweep dashboard (spec 9.7): ``python main.py --dashboard``.

A Streamlit page on this PC (http://localhost:8501) that shows what the running
bot sees and does: the live M5 chart with liquidity pools, open fair value gaps
and signals; today's scenario briefs; the news feed with its AI tags and the
releases that block entries; open trades, risk status and the equity curve;
the signal log; backtest reports. The Control tab sends pause / resume / stop
(the kill switch) to the bot through the database: the dashboard itself never
trades and never talks to MetaTrader 5.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# ``streamlit run`` starts this file as a script: put the project folder first on the
# path, so ``goldsweep`` is this package wherever the command was started from.
_PROJECT = Path(__file__).resolve().parents[2]
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from goldsweep.core.settings import PROJECT_ROOT, Settings, load_settings  # noqa: E402
from goldsweep.dashboard import charts, data  # noqa: E402
from goldsweep.storage.db import Database  # noqa: E402

DB_ENV = "GOLDSWEEP_DASHBOARD_DB"
REPORTS_ENV = "GOLDSWEEP_REPORTS_DIR"
REFRESH_CHOICES = {"every 15 s": 15, "every 30 s": 30, "every minute": 60}


@st.cache_resource
def _settings() -> Settings:
    return load_settings()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _db_path(settings: Settings) -> str:
    """``--db PATH`` after ``--`` on the streamlit command line, else the environment,
    else the bot's database."""
    args = sys.argv[1:]
    if "--db" in args and args.index("--db") + 1 < len(args):
        return args[args.index("--db") + 1]
    return os.environ.get(DB_ENV) or str(settings.db_path)


def _reports_dir() -> Path:
    return Path(os.environ.get(REPORTS_ENV) or PROJECT_ROOT / "reports")


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def header(db: Database, now: dt.datetime, zone: dt.tzinfo) -> tuple[data.BotStatus, dict]:
    status = data.bot_status(db, now)
    picture = data.snapshot(db) or {}
    if status.alive:
        st.success(status.describe(now), icon="🟢")
    else:
        st.warning(
            status.describe(now)
            + " What you see below is the last saved picture. Open positions keep their "
            "stop-loss and take-profit at the broker.",
            icon="⚠️",
        )
    columns = st.columns(6)
    columns[0].metric("Mode", picture.get("mode") or status.mode or "-")
    columns[1].metric("Trading", picture.get("state") or "-")
    equity = picture.get("equity")
    columns[2].metric("Equity", f"{equity:,.2f}" if equity is not None else "-")
    price = picture.get("price")
    digits = int(picture.get("digits") or 2)
    columns[3].metric(f"{picture.get('symbol', 'Gold')}", f"{price:.{digits}f}" if price else "-")
    h1 = picture.get("h1_bias") or {}
    columns[4].metric(
        "H1 bias", f"{h1.get('value', '-')}", f"{h1.get('score', 0):+.2f}" if h1 else None
    )
    news = picture.get("news_bias")
    columns[5].metric("News bias", f"{news:+.2f}" if news is not None else "-")
    updated = picture.get("time")
    if updated:
        st.caption(
            f"Bot picture from {data.to_local(dt.datetime.fromisoformat(updated), zone)} "
            f"({zone}); refreshed automatically. Times are {zone} unless marked UTC."
        )
    return status, picture


def market_tab(
    db: Database, settings: Settings, picture: dict, zone: dt.tzinfo, hours: int
) -> None:
    symbol = picture.get("symbol")
    left, right = st.columns([3, 1])
    with left:
        if not symbol:
            st.info("The chart appears after the bot's first M5 bar with this version.")
        else:
            frame = data.candles(db, symbol, bars=hours * 12)
            strategy = settings.config.strategy
            gaps = data.open_gaps(
                frame, atr_period=strategy.atr_period, min_size_atr=strategy.fvg_min_atr
            )
            since = frame.index[0].to_pydatetime() if not frame.empty else _now()
            fig = charts.price_chart(
                frame,
                zone=zone,
                pools=picture.get("pools", []),
                gaps=gaps,
                signals=data.signals(db, since, settings.config.risk.tp3_r),
                positions=picture.get("positions", []),
                title=f"{symbol} M5: pools (dashed), open gaps (shaded), signals",
            )
            st.plotly_chart(fig, key="price_chart")
            st.caption(
                "Orange: buy-side liquidity above highs. Blue: sell-side liquidity below lows. "
                "Grey dotted: already swept. Shaded: fair value gaps price has not filled."
            )
    with right:
        pressure = picture.get("pressure")
        st.subheader("Buyers vs sellers")
        if pressure:
            st.plotly_chart(
                charts.pressure_bar(pressure["buyers"], pressure["sellers"]), key="pressure"
            )
            parts = pressure.get("components", {})
            st.caption(
                " | ".join(f"{name} {value:+.2f}" for name, value in parts.items())
                + ". An estimate from tick volume, candles, H1 structure and news: not real "
                "order flow."
            )
        else:
            st.caption("No reading yet.")
        st.subheader("Nearest pools")
        pools = pd.DataFrame(picture.get("pools", []))
        if pools.empty:
            st.caption("None yet.")
        else:
            st.dataframe(pools[["label", "price", "side", "strength", "swept"]], hide_index=True)
    st.subheader("Today's scenario briefs")
    briefs = data.scenario_briefs(db, _now() - dt.timedelta(hours=18))
    if not briefs:
        st.caption("None in the last 18 hours (they come 30 min before each kill zone).")
    for brief in briefs[:4]:
        with st.expander(f"{data.to_local(brief.time, zone)}: {brief.trigger} ({brief.method})"):
            for name, text in brief.cases:
                st.markdown(f"**{name}:** {text}")
            for note in brief.notes:
                st.caption(note)


def trades_tab(db: Database, picture: dict, zone: dt.tzinfo) -> None:
    live = bool(picture.get("live"))
    st.subheader("Open trades")
    positions = picture.get("positions", [])
    if positions:
        st.dataframe(pd.DataFrame(positions), hide_index=True)
    else:
        st.caption("No open positions.")
    orders = picture.get("orders", [])
    if orders:
        st.markdown("**Waiting entry orders**")
        table = pd.DataFrame(orders)[
            [
                "id",
                "direction",
                "entry",
                "stop_loss",
                "take_profit_1",
                "take_profit_2",
                "expires_at",
            ]
        ]
        st.dataframe(table, hide_index=True)

    st.subheader("Risk status")
    risk = picture.get("risk")
    if risk:
        cols = st.columns(5)
        cols[0].metric("Trades today", f"{risk['trades_today']} / {risk['max_trades_per_day']}")
        cols[1].metric("Open", f"{risk['open']} / {risk['max_open_positions']}")
        cols[2].metric(
            f"Today (limit {data.money(risk['day_limit'])})", data.money(risk["day_pnl"])
        )
        cols[3].metric(
            f"This week (limit {data.money(risk['week_limit'])})", data.money(risk["week_pnl"])
        )
        cols[4].metric(
            "Losses in a row", f"{risk['losses_in_a_row']} / {risk['max_consecutive_losses']}"
        )
        if risk.get("paused_until"):
            until = dt.datetime.fromisoformat(risk["paused_until"])
            st.warning(f"Paused by the risk rules until {data.to_local(until, zone)}.")
        st.caption(f"Risk per trade: {risk['risk_per_trade_pct']:g}% of the balance.")
    else:
        st.caption("The risk picture arrives with the bot's next bar.")

    closed = data.trades(db, live=live)
    st.subheader("Equity and results" + (" (live)" if live else " (paper)"))
    stats = data.trade_stats(closed)
    cols = st.columns(5)
    cols[0].metric("Trades", stats.trades)
    cols[1].metric("Won / lost", f"{stats.wins} / {stats.losses}")
    cols[2].metric("Net", data.money(stats.net))
    cols[3].metric("Total R", f"{stats.total_r:+.2f}")
    cols[4].metric("Profit factor", f"{stats.profit_factor:.2f}" if stats.profit_factor else "-")
    st.plotly_chart(
        charts.equity_chart(data.equity_curve(closed, picture.get("equity")), zone), key="equity"
    )
    if not closed.empty:
        shown = data.times_in(closed.iloc[::-1], ["opened", "closed"], zone)
        st.dataframe(shown, hide_index=True)
    st.caption("Past results do not predict future ones; every trade can lose.")
    if live:
        st.subheader("Orders sent to MT5")
        orders_log = data.recent_orders(db)
        if orders_log.empty:
            st.caption("No order requests yet.")
        else:
            st.dataframe(data.times_in(orders_log, ["time"], zone), hide_index=True)


def news_tab(db: Database, settings: Settings, zone: dt.tzinfo) -> None:
    now = _now()
    st.subheader("Releases that block new entries")
    releases = data.upcoming_releases(db, settings, now)
    news = settings.config.news
    if not releases:
        st.caption("None in the next 48 hours (or the calendar has not been read yet).")
    for event in releases:
        start = event.time - dt.timedelta(minutes=news.blackout_before_min)
        end = event.time + dt.timedelta(minutes=news.blackout_after_min)
        figures = ", ".join(
            f"{name} {value}"
            for name, value in (
                ("forecast", event.forecast),
                ("previous", event.previous),
                ("actual", event.actual),
            )
            if value
        )
        st.markdown(
            f"**{data.to_local(event.time, zone)}** {event.currency} {event.title} "
            f"({event.impact}) {figures}  \n"
            f"no entries {data.to_local(start, zone)[-5:]}-{data.to_local(end, zone)[-5:]}"
        )
    st.subheader("Headlines and their AI tags")
    feed = data.headlines(db, now - dt.timedelta(hours=24))
    only_gold = st.toggle("Only gold-related", value=True, key="only_gold")
    if only_gold and not feed.empty:
        feed = feed[feed["gold"].fillna(False).astype(bool)]
    if feed.empty:
        st.caption("No headlines in the last 24 hours.")
    else:
        st.dataframe(
            data.times_in(feed, ["time"], zone),
            hide_index=True,
            column_config={"url": st.column_config.LinkColumn("link", display_text="open")},
        )
    usage = data.llm_today(db, now)
    st.caption(
        f"AI today ({usage['day']} UTC): {usage['calls']} calls, "
        f"{usage['input']:,} input and {usage['output']:,} output tokens, "
        f"about US${usage['cost']:.4f}. The AI only tags text; it never places trades."
    )


def signals_tab(db: Database, settings: Settings, zone: dt.tzinfo) -> None:
    days = st.radio("Period", (1, 7, 30), horizontal=True, format_func=lambda d: f"{d} day(s)")
    found = data.signals(db, _now() - dt.timedelta(days=days), settings.config.risk.tp3_r)
    if found.empty:
        st.caption("No alerts, trades or blocked setups in this period.")
        return
    st.dataframe(data.times_in(found, ["time"], zone), hide_index=True)
    st.caption("traded: an order was placed; alert: alert only; blocked: a risk rule said no.")


def scalper_tab(db: Database, zone: dt.tzinfo) -> None:
    """The scalper (S7, D113): its mode, open scalps and results per mode and account."""
    picture = data.scalper_picture(db)
    today = picture.get("today") or {}
    cols = st.columns(4)
    cols[0].metric("Scalper mode", picture.get("mode", "-"))
    cols[1].metric("Trades on", picture.get("where", "-"))
    cols[2].metric("Scalps, last 24 h", today.get("scalps", 0))
    cols[3].metric("R, last 24 h", f"{today.get('r', 0.0):+.2f}")
    equity = picture.get("paper_equity")
    if isinstance(equity, dict) and equity:
        st.caption(
            "Paper books (notional): "
            + ", ".join(f"{mode} {value:,.2f}" for mode, value in equity.items())
        )
    st.subheader("Open scalps")
    open_scalps = picture.get("open") or []
    if open_scalps:
        shown = pd.DataFrame(open_scalps)
        keep = [
            c
            for c in ("id", "mode", "where", "direction", "lots", "fill_price", "stop", "targets")
            if c in shown
        ]
        st.dataframe(shown[keep], hide_index=True)
    else:
        st.caption("No scalp is open.")
    st.subheader("Results")
    days = st.radio(
        "Period",
        (1, 7, 30, 90),
        index=2,
        horizontal=True,
        format_func=lambda d: f"{d} day(s)",
        key="scalp_days",
    )
    since = _now() - dt.timedelta(days=days)
    table = data.scalp_stats(db, since)
    if table.empty:
        st.caption("No scalps closed in this period.")
        return
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            "won": st.column_config.NumberColumn(format="percent"),
            "break-even won": st.column_config.NumberColumn(format="percent"),
            "profit factor": st.column_config.NumberColumn(format="%.2f"),
            "R each": st.column_config.NumberColumn(format="%+.2f"),
            "backtest R each": st.column_config.NumberColumn(format="%+.2f"),
            "total R": st.column_config.NumberColumn(format="%+.2f"),
            "net": st.column_config.NumberColumn(format="%+.2f"),
        },
    )
    closed = data.scalps(db, since)
    st.line_chart(data.cumulative_r(closed), y_label="total R")
    st.dataframe(data.times_in(closed.iloc[::-1], ["opened", "closed"], zone), hide_index=True)
    st.caption(
        "After costs. A mode is judged on paper and on the demo account against its "
        "backtest (D111, D112); past results do not predict future ones, and every scalp "
        "can lose."
    )


def backtests_tab() -> None:
    reports = data.backtest_reports(_reports_dir())
    if not reports:
        st.caption(
            "No backtest reports yet. Run: python -m goldsweep.backtest --from 2024-01-01 "
            "--to 2026-10-31"
        )
        return
    chosen = st.selectbox("Report", reports, format_func=lambda r: r.name)
    if chosen is None:
        return
    flat = data.flatten(chosen.summary)
    table = pd.DataFrame({"value": [str(v) for v in flat.values()]}, index=list(flat.keys()))
    st.dataframe(table, height=420)
    if chosen.html.exists():
        st.download_button(
            "Download the full report (HTML)",
            chosen.html.read_bytes(),
            file_name=f"{chosen.name}.html",
            mime="text/html",
        )
    st.caption(
        f"Folder: {chosen.folder}. A backtest describes the past under stated assumptions; "
        "it cannot promise future results."
    )


def control_tab(db: Database, status: data.BotStatus, zone: dt.tzinfo) -> None:
    now = _now()
    st.write(
        "Commands go to the running bot through the database; it carries them out within a "
        "few seconds and also reports them on Telegram."
    )
    if not status.alive:
        st.error(
            "The bot is not running, so it cannot receive commands. To close positions now, "
            "use MetaTrader 5 (Trade tab: right-click a position, Close)."
        )
    sent = st.session_state.get("command_id")
    cols = st.columns(3)
    if cols[0].button("⏸ Pause new trades", disabled=not status.alive, key="pause"):
        sent = st.session_state["command_id"] = data.send_command(db, "pause", now)
    if cols[1].button("▶️ Resume", disabled=not status.alive, key="resume"):
        sent = st.session_state["command_id"] = data.send_command(db, "resume", now)
    if cols[2].button("ℹ️ Ask for /status", disabled=not status.alive, key="status"):
        sent = st.session_state["command_id"] = data.send_command(db, "status", now)
    st.divider()
    st.subheader("Strategies and scalper mode")
    switch = data.switch_state(db)
    st.write(
        f"Now: **{switch.get('strategies', 'main')}** may trade; scalper: "
        f"**{switch.get('scalper', 'off')}**. A selected scalper mode runs on paper; "
        "/approve scalper <mode> on Telegram sends it to the demo account."
    )
    cols = st.columns(2)
    strategies = cols[0].selectbox("Strategies that may trade", ["main", "scalper", "both"])
    if cols[0].button("Apply", disabled=not status.alive, key="apply_mode"):
        sent = st.session_state["command_id"] = data.send_command(db, f"mode:{strategies}", now)
    mode = cols[1].selectbox("Scalper mode", ["off", "hft", "intraday", "swing"])
    if cols[1].button("Apply", disabled=not status.alive, key="apply_scalper"):
        sent = st.session_state["command_id"] = data.send_command(db, f"scalper:{mode}", now)
    st.divider()
    st.subheader("🛑 Kill switch")
    st.write(
        "Cancels every GoldSweep entry order, closes every GoldSweep position at market and "
        "stops trading until Resume. Trades you opened yourself are not touched."
    )
    sure = st.checkbox("Yes, close all GoldSweep positions now", key="kill_sure")
    if st.button(
        "Close everything and stop", type="primary", disabled=not (sure and status.alive),
        key="kill",
    ):  # fmt: skip
        sent = st.session_state["command_id"] = data.send_command(db, "stop", now)
    if sent:
        result = data.command_result(db, int(sent), now)
        if result.status == "pending":
            st.info("Sent; waiting for the bot (this page refreshes by itself).")
        elif result.status == "done":
            st.success(result.reply or "done")
        else:
            st.error(f"Not carried out: {result.reply}")
    st.caption(f"Database: {db.path}")


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(page_title="GoldSweep", page_icon="🟡", layout="wide")
    settings = _settings()
    path = _db_path(settings)
    zone = settings.display_zone
    with st.sidebar:
        st.title("GoldSweep")
        st.caption("XAUUSD liquidity-sweep assistant: what the bot sees and does.")
        hours = st.slider("Chart hours", 3, 24, 12, key="hours")
        refresh = REFRESH_CHOICES[
            st.selectbox("Refresh", list(REFRESH_CHOICES), index=1, key="refresh")
        ]
        st.caption(
            "This page only reads the bot's database. Orders are placed by the bot "
            "(python main.py --live or --paper), never by this page."
        )

    @st.fragment(run_every=refresh)
    def live() -> None:
        # One short-lived connection per refresh: SQLite opens fast, and nothing is left
        # open across Streamlit's threads.
        with Database(path) as db:
            now = _now()
            status, picture = header(db, now, zone)
            market, trades, scalper, news, signals, backtests, control = st.tabs(
                ["Market", "Trades", "Scalper", "News", "Signals", "Backtests", "Control"]
            )
            with market:
                market_tab(db, settings, picture, zone, hours)
            with trades:
                trades_tab(db, picture, zone)
            with scalper:
                scalper_tab(db, zone)
            with news:
                news_tab(db, settings, zone)
            with signals:
                signals_tab(db, settings, zone)
            with backtests:
                backtests_tab()
            with control:
                control_tab(db, status, zone)

    live()


main()

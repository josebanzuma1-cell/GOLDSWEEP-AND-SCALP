"""Plotly figures for the dashboard: the price chart, the equity curve, the pressure bar."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from goldsweep.analysis.structure import Direction
from goldsweep.analysis.zones import Zone
from goldsweep.core.clock import ensure_utc

UP, DOWN = "#26a69a", "#ef5350"
BUY_SIDE, SELL_SIDE, SWEPT = "#ff9800", "#29b6f6", "#9e9e9e"
ENTRY, STOP, TARGET = "#1e88e5", "#e53935", "#43a047"
SIGNAL_BARS = 12  # how far a signal's lines reach to the right, in M5 bars


def _local(moment: dt.datetime | pd.Timestamp, zone: dt.tzinfo) -> dt.datetime:
    """Chart times are shown in the display time zone, without a zone suffix."""
    return ensure_utc(pd.Timestamp(moment).to_pydatetime()).astimezone(zone).replace(tzinfo=None)


def price_chart(
    frame: pd.DataFrame,
    *,
    zone: dt.tzinfo,
    pools: Sequence[dict[str, Any]] = (),
    gaps: Sequence[Zone] = (),
    signals: pd.DataFrame | None = None,
    positions: Sequence[dict[str, Any]] = (),
    title: str = "",
) -> go.Figure:
    """M5 candles with liquidity pools, open fair value gaps, signals and open trades."""
    fig = go.Figure()
    if frame.empty:
        fig.update_layout(title="No candles stored yet: they arrive with the bot's next bar.")
        return fig
    times = [_local(t, zone) for t in frame.index]
    fig.add_trace(
        go.Candlestick(
            x=times,
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="M5",
            increasing_line_color=UP,
            decreasing_line_color=DOWN,
            showlegend=False,
        )
    )
    first, last = times[0], times[-1]
    step = dt.timedelta(minutes=5)
    right = last + step * 6
    low, high = float(frame["low"].min()), float(frame["high"].max())
    margin = (high - low) * 0.15 or 1.0
    shown_low, shown_high = low - margin, high + margin

    for pool in pools:
        price = float(pool["price"])
        if not shown_low <= price <= shown_high:
            continue
        color = (
            SWEPT
            if pool.get("swept")
            else (BUY_SIDE if pool.get("side") == "buy_side" else SELL_SIDE)
        )
        fig.add_shape(
            type="line", x0=first, x1=right, y0=price, y1=price,
            line={"color": color, "width": 1, "dash": "dot" if pool.get("swept") else "dash"},
        )  # fmt: skip
        fig.add_annotation(
            x=right, y=price, text=f"{pool.get('label', '')} {price:.2f}", showarrow=False,
            xanchor="left", font={"size": 10, "color": color},
        )  # fmt: skip

    for gap in gaps:
        color = UP if gap.direction is Direction.LONG else DOWN
        fig.add_shape(
            type="rect", x0=_local(gap.time, zone), x1=right, y0=gap.bottom, y1=gap.top,
            fillcolor=color, opacity=0.15, line={"width": 0}, layer="below",
        )  # fmt: skip

    if signals is not None and not signals.empty:
        for row in signals.itertuples():
            at = _local(row.time, zone)
            if at < first:
                continue
            end = at + step * SIGNAL_BARS
            long = row.direction == Direction.LONG.value
            levels = [("Entry", row.entry, ENTRY), ("SL", row.stop_loss, STOP)]
            levels += [
                (name, value, TARGET)
                for name, value in (("TP1", row.tp1), ("TP2", row.tp2), ("TP3", row.tp3))
                if value is not None and not pd.isna(value)
            ]
            for name, value, color in levels:
                fig.add_shape(
                    type="line", x0=at, x1=end, y0=value, y1=value,
                    line={"color": color, "width": 2 if name == "Entry" else 1,
                          "dash": "solid" if name in ("Entry", "SL") else "dash"},
                )  # fmt: skip
            fig.add_trace(
                go.Scatter(
                    x=[at], y=[row.entry], mode="markers+text",
                    marker={"symbol": "triangle-up" if long else "triangle-down", "size": 13,
                            "color": ENTRY},
                    text=[f"{'BUY' if long else 'SELL'} {row.score:.0f} ({row.action})"],
                    textposition="bottom center" if long else "top center",
                    name=row.id, showlegend=False,
                    hovertext=f"{row.id}: entry {row.entry:.2f}, SL {row.stop_loss:.2f}, "
                    f"TP1 {row.tp1:.2f}",
                )
            )  # fmt: skip

    for position in positions:
        for name, value, color in (
            ("Filled", position.get("fill_price"), ENTRY),
            ("Stop", position.get("stop"), STOP),
        ):
            if value:
                fig.add_shape(
                    type="line", x0=first, x1=right, y0=value, y1=value,
                    line={"color": color, "width": 2},
                )  # fmt: skip
                fig.add_annotation(
                    x=first, y=value, text=f"{position.get('id')} {name.lower()} {value:.2f}",
                    showarrow=False, xanchor="left", yanchor="bottom",
                    font={"size": 10, "color": color},
                )  # fmt: skip

    fig.update_layout(
        title=title,
        height=560,
        margin={"l": 10, "r": 150, "t": 40, "b": 10},
        xaxis_rangeslider_visible=False,
        yaxis={"range": [shown_low, shown_high], "side": "right"},
        hovermode="x unified",
    )
    return fig


def equity_chart(curve: pd.DataFrame, zone: dt.tzinfo) -> go.Figure:
    fig = go.Figure()
    if not curve.empty:
        fig.add_trace(
            go.Scatter(
                x=[_local(t, zone) for t in curve["time"]],
                y=curve["equity"],
                mode="lines+markers",
                line={"shape": "hv", "color": ENTRY},
                name="equity",
            )
        )
    fig.update_layout(height=300, margin={"l": 10, "r": 10, "t": 30, "b": 10}, title="Equity")
    return fig


def pressure_bar(buyers: float, sellers: float) -> go.Figure:
    """Buyers against sellers, as one bar. An estimate from tick volume, candles and news."""
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            y=["pressure"], x=[buyers], orientation="h", name=f"Buyers {buyers:.0f}%",
            marker_color=UP, text=[f"Buyers {buyers:.0f}%"], textposition="inside",
        )
    )  # fmt: skip
    fig.add_trace(
        go.Bar(
            y=["pressure"], x=[sellers], orientation="h", name=f"Sellers {sellers:.0f}%",
            marker_color=DOWN, text=[f"Sellers {sellers:.0f}%"], textposition="inside",
        )
    )  # fmt: skip
    fig.update_layout(
        barmode="stack",
        height=90,
        showlegend=False,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        xaxis={"range": [0, 100], "visible": False},
        yaxis={"visible": False},
    )
    return fig

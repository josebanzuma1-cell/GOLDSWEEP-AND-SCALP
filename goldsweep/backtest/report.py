"""Backtest output: CSV files, a JSON summary and a self-contained HTML report.

Everything written depends only on the inputs (data, settings, code), never on
the time of the run, so the same command produces the same files.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from goldsweep.backtest.engine import BacktestResult
from goldsweep.backtest.metrics import Metrics, drawdown, equity_points
from goldsweep.backtest.simulator import SignalOutcome, Trade

DISCLAIMER = (
    "Past results on historical or synthetic data do not predict future results. "
    "Every trade can lose; the stop-loss is what keeps each loss small."
)

TRADE_COLUMNS = (
    "id",
    "direction",
    "session",
    "weekday",
    "score",
    "signal_time",
    "fill_time",
    "exit_time",
    "planned_entry",
    "fill_price",
    "stop_loss",
    "take_profit_1",
    "take_profit_2",
    "lots",
    "risk_amount",
    "exit_reason",
    "commission",
    "net_pnl",
    "r_multiple",
    "planned_rr",
    "swept",
    "pressure_buyers_pct",
    "pressure_label",
    "pressure_agreed",
)


def _time(moment: dt.datetime | None) -> str:
    return "" if moment is None else f"{moment:%Y-%m-%d %H:%M}"


def _num(value: float | None, digits: int = 2) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def trade_row(trade: Trade) -> dict[str, object]:
    plan = trade.plan
    signal = trade.signal  # the full signal, which backtests attach
    reading = getattr(trade.record, "pressure", None)
    agreed = reading.agrees_with(trade.direction) if reading is not None else None
    return {
        "id": plan.id,
        "direction": plan.direction.value,
        "session": plan.session or "",
        "weekday": f"{plan.time:%A}",
        "score": f"{plan.score:.1f}",
        "signal_time": _time(plan.time),
        "fill_time": _time(trade.fill_time),
        "exit_time": _time(trade.exit_time),
        "planned_entry": _num(plan.entry),
        "fill_price": _num(trade.fill_price),
        "stop_loss": _num(trade.stop_loss),
        "take_profit_1": _num(trade.take_profit_1),
        "take_profit_2": _num(trade.take_profit_2),
        "lots": f"{trade.lots:g}",
        "risk_amount": _num(trade.risk_amount),
        "exit_reason": trade.exit_reason.value,
        "commission": _num(trade.commission),
        "net_pnl": _num(trade.net_pnl),
        "r_multiple": _num(trade.r_multiple, 3),
        "planned_rr": _num(plan.risk_reward_1),
        "swept": signal.pool.label if signal is not None else "",
        "pressure_buyers_pct": "" if reading is None else f"{reading.buyers:.1f}",
        "pressure_label": "" if reading is None else reading.label,
        "pressure_agreed": "" if agreed is None else ("yes" if agreed else "no"),
    }


def write_trades_csv(path: Path, trades: Iterable[Trade]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS)
        writer.writeheader()
        for trade in trades:
            writer.writerow(trade_row(trade))


def write_signals_csv(path: Path, outcomes: Iterable[SignalOutcome]) -> None:
    columns = ("id", "time", "direction", "session", "score", "risk_reward", "outcome", "reason")
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for item in sorted(outcomes, key=lambda o: (o.record.time, o.record.signal.id)):
            signal = item.record.signal
            writer.writerow(
                [
                    signal.id,
                    _time(signal.time),
                    signal.direction.value,
                    signal.session or "",
                    f"{signal.score:.1f}",
                    f"{signal.risk_reward_1:.2f}",
                    item.outcome.value,
                    item.reason,
                ]
            )


def write_equity_csv(
    path: Path, trades: Sequence[Trade], balance: float, start: dt.datetime
) -> None:
    points = equity_points(trades, balance, start)
    peak = balance
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "equity", "drawdown_pct"))
        for moment, equity in points:
            peak = max(peak, equity)
            writer.writerow([_time(moment), f"{equity:.2f}", f"{(peak - equity) / peak * 100:.2f}"])


def _metrics_json(metrics: Metrics) -> dict[str, object]:
    return {
        key: (round(value, 6) if isinstance(value, float) else value)
        for key, value in metrics.as_dict().items()
    }


def summary_json(result: BacktestResult, command: str) -> dict[str, object]:
    wf = result.walk_forward
    outcome_counts = Counter(item.outcome.value for item in result.result.outcomes)
    return {
        "command": command,
        "symbol": result.data.symbol,
        "source": result.data.source,
        "synthetic": result.data.synthetic,
        "period": {"from": _time(result.start), "to": _time(result.end)},
        "fingerprints": result.fingerprints,
        "signals": len(result.records),
        "signal_outcomes": dict(sorted(outcome_counts.items())),
        "metrics": _metrics_json(result.metrics),
        "breakdowns": {
            name: {str(key): _metrics_json(value) for key, value in groups.items()}
            for name, groups in result.breakdowns.items()
        },
        "walk_forward": None
        if wf is None
        else {
            "folds": [
                {
                    "train": [_time(f.fold.train_start), _time(f.fold.train_end)],
                    "test": [_time(f.fold.test_start), _time(f.fold.test_end)],
                    "chosen": f.chosen.describe(),
                    "used_defaults": f.used_defaults,
                    "train_metrics": _metrics_json(f.train),
                    "test_metrics": _metrics_json(f.test),
                }
                for f in wf.folds
            ],
            "out_of_sample": _metrics_json(wf.oos_metrics),
            "in_sample_expectancy_r": wf.in_sample_expectancy_r,
            "warnings": wf.warnings,
        },
        "limitations": result.limitations,
        "settings": result.settings_snapshot,
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root { --bg:#ffffff; --fg:#1d2433; --muted:#5d6678; --line:#d9dee7; --card:#f5f7fa;
  --accent:#a8741a; --good:#1f7a4d; --bad:#b3261e; --warn-bg:#fdecea; --warn-fg:#8c1d18;
  --note-bg:#fff7e6; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --fg:#e6e9ef; --muted:#9aa3b2;
  --line:#2c323c; --card:#1c2027; --accent:#e0b04a; --good:#5cc08b; --bad:#f07167;
  --warn-bg:#3a1714; --warn-fg:#ffb4ab; --note-bg:#2b2416; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:24px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 8px; }
.sub { color:var(--muted); margin:0 0 16px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:10px 12px; }
.tile .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.tile .v { font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; }
.warn { background:var(--warn-bg); color:var(--warn-fg); border-radius:8px; padding:12px 16px;
  margin:16px 0; } .warn strong { display:block; margin-bottom:4px; }
.note { background:var(--note-bg); border-radius:8px; padding:12px 16px; margin:16px 0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
th, td { padding:6px 8px; border-bottom:1px solid var(--line); text-align:right;
  white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; }
.pos { color:var(--good); } .neg { color:var(--bad); }
svg { width:100%; height:auto; display:block; }
.chart { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:8px; }
ul { padding-left:20px; } code { font-size:13px; }
footer { color:var(--muted); font-size:13px; margin-top:32px; }
"""


def _e(text: object) -> str:
    return html.escape(str(text))


def _fmt(value: float | int | None, kind: str = "num") -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "r":
        return f"{value:+.2f} R"
    if kind == "money":
        return f"{value:,.2f}"
    if kind == "int":
        return f"{int(value):,}"
    return f"{value:.2f}"


def _signed_class(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _tiles(metrics: Metrics, currency: str) -> str:
    items = [
        ("Trades", _fmt(metrics.trades, "int"), ""),
        ("Win rate", _fmt(metrics.win_rate, "pct"), ""),
        ("Profit factor", _fmt(metrics.profit_factor), ""),
        ("Expectancy", _fmt(metrics.expectancy_r, "r"), _signed_class(metrics.expectancy_r)),
        (
            f"Net ({currency})",
            _fmt(metrics.net_profit, "money"),
            _signed_class(metrics.net_profit),
        ),
        ("Return", f"{metrics.return_pct:+.1f}%", _signed_class(metrics.return_pct)),
        ("Max drawdown", f"{metrics.max_drawdown_pct:.1f}%", ""),
        ("Avg planned R:R", _fmt(metrics.average_planned_rr), ""),
    ]
    return (
        '<div class="tiles">'
        + "".join(
            f'<div class="tile"><div class="k">{_e(k)}</div><div class="v {c}">{v}</div></div>'
            for k, v, c in items
        )
        + "</div>"
    )


def _metrics_table(groups: Mapping[object, Metrics], first: str) -> str:
    if not groups:
        return "<p>No trades.</p>"
    head = (
        f"<tr><th>{_e(first)}</th><th>Trades</th><th>Win rate</th><th>Profit factor</th>"
        "<th>Expectancy</th><th>Total R</th><th>Net</th></tr>"
    )
    rows = "".join(
        f"<tr><td>{_e(key)}</td><td>{m.trades}</td><td>{_fmt(m.win_rate, 'pct')}</td>"
        f"<td>{_fmt(m.profit_factor)}</td>"
        f'<td class="{_signed_class(m.expectancy_r)}">{_fmt(m.expectancy_r, "r")}</td>'
        f'<td class="{_signed_class(m.total_r)}">{m.total_r:+.2f}</td>'
        f'<td class="{_signed_class(m.net_profit)}">{_fmt(m.net_profit, "money")}</td></tr>'
        for key, m in groups.items()
    )
    return f'<div class="scroll"><table>{head}{rows}</table></div>'


def equity_svg(points: Sequence[tuple[dt.datetime, float]], baseline: float) -> str:
    """A small line chart of equity over time, as inline SVG."""
    if len(points) < 2:
        return "<p>Not enough trades to draw an equity curve.</p>"
    width, height, pad_l, pad_r, pad_t, pad_b = 900, 260, 70, 16, 14, 28
    times = [p[0].timestamp() for p in points]
    values = [p[1] for p in points]
    t0, t1 = min(times), max(times)
    v0, v1 = min(*values, baseline), max(*values, baseline)
    if v1 == v0:
        v1 = v0 + 1.0
    if t1 == t0:
        t1 = t0 + 1.0

    def x(t: float) -> float:
        return pad_l + (t - t0) / (t1 - t0) * (width - pad_l - pad_r)

    def y(v: float) -> float:
        return pad_t + (v1 - v) / (v1 - v0) * (height - pad_t - pad_b)

    path = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in zip(times, values, strict=True))
    base_y = y(baseline)
    amount, fraction = drawdown(values)
    labels = (
        f'<text x="{pad_l - 8}" y="{y(v1) + 4:.1f}" text-anchor="end">{v1:,.0f}</text>'
        f'<text x="{pad_l - 8}" y="{y(v0) + 4:.1f}" text-anchor="end">{v0:,.0f}</text>'
        f'<text x="{pad_l}" y="{height - 6}">{points[0][0]:%Y-%m-%d}</text>'
        f'<text x="{width - pad_r}" y="{height - 6}" text-anchor="end">'
        f"{points[-1][0]:%Y-%m-%d}</text>"
    )
    return (
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Equity curve; deepest drawdown {fraction * 100:.1f}% ({amount:,.0f})">'
        f'<g style="font-size:12px;fill:var(--muted)">{labels}</g>'
        f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{base_y:.1f}" y2="{base_y:.1f}" '
        'stroke="var(--line)" stroke-dasharray="4 4"/>'
        f'<polyline points="{path}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
        "</svg></div>"
    )


def _trades_table(trades: Sequence[Trade]) -> str:
    if not trades:
        return "<p>No trades.</p>"
    columns = (
        "id",
        "direction",
        "session",
        "score",
        "fill_time",
        "fill_price",
        "stop_loss",
        "take_profit_1",
        "lots",
        "exit_reason",
        "net_pnl",
        "r_multiple",
        "pressure_label",
    )
    head = "".join(f"<th>{_e(c.replace('_', ' '))}</th>" for c in columns)
    body = []
    for trade in trades:
        row = trade_row(trade)
        cells = "".join(
            f'<td class="{_signed_class(trade.r_multiple) if c == "r_multiple" else ""}">'
            f"{_e(row[c])}</td>"
            for c in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return f'<div class="scroll"><table><tr>{head}</tr>{"".join(body)}</table></div>'


def render_html(result: BacktestResult, command: str) -> str:
    metrics = result.metrics
    balance = float(result.settings_snapshot["backtest"]["initial_balance"])  # type: ignore[index]
    currency = result.data.spec.currency_profit or "account currency"
    title = (
        f"GoldSweep backtest: {result.data.symbol} {result.start:%Y-%m-%d} to {result.end:%Y-%m-%d}"
    )
    parts = [
        f"<h1>{_e(title)}</h1>",
        f'<p class="sub">Prices: {_e(result.data.source)} | data {result.fingerprints["data"]} '
        f"| settings {result.fingerprints['settings']} | code {result.fingerprints['code']}</p>",
    ]
    if result.data.synthetic:
        parts.append(
            '<div class="warn"><strong>Synthetic prices</strong>These results come from a random '
            "walk, not from real gold prices. They only show that the backtester works.</div>"
        )
    wf = result.walk_forward
    if wf is not None and wf.warnings:
        items = "".join(f"<li>{_e(w)}</li>" for w in wf.warnings)
        parts.append(
            '<div class="warn"><strong>Overfitting and reliability warnings</strong>'
            f"<ul>{items}</ul></div>"
        )
    parts.append("<h2>The configured strategy, whole period</h2>")
    parts.append(_tiles(metrics, currency))
    parts.append(equity_svg(equity_points(result.result.trades, balance, result.start), balance))
    outcome_counts = Counter(item.outcome.value for item in result.result.outcomes)
    parts.append(
        f"<p>{len(result.records)} signals; "
        + ", ".join(f"{_e(k.replace('_', ' '))}: {v}" for k, v in sorted(outcome_counts.items()))
        + ".</p>"
    )
    titles = {
        "session": "By session",
        "weekday": "By weekday",
        "direction": "By direction",
        "pressure": "By buyer/seller pressure meter (did it lean with the trade?)",
    }
    for name, groups in result.breakdowns.items():
        parts.append(f"<h2>{_e(titles.get(name, name))}</h2>")
        parts.append(_metrics_table(groups, name.capitalize()))
    if wf is not None:
        parts.append("<h2>Walk-forward test (out-of-sample)</h2>")
        if wf.folds:
            parts.append(
                "<p>Each window picks the best settings on its training months, then trades "
                "them on the following test months it has never seen.</p>"
            )
            parts.append(_tiles(wf.oos_metrics, currency))
            parts.append(
                equity_svg(
                    equity_points(wf.out_of_sample.trades, balance, wf.folds[0].fold.test_start),
                    balance,
                )
            )
            head = (
                "<tr><th>Train</th><th>Test</th><th>Chosen</th><th>Train trades</th>"
                "<th>Train exp.</th><th>Test trades</th><th>Test exp.</th><th>Test PF</th></tr>"
            )
            rows = "".join(
                f"<tr><td>{f.fold.train_start:%Y-%m-%d} .. {f.fold.train_end:%Y-%m-%d}</td>"
                f"<td>{f.fold.test_start:%Y-%m-%d} .. {f.fold.test_end:%Y-%m-%d}</td>"
                f"<td>{_e(f.chosen.describe())}{' (defaults)' if f.used_defaults else ''}</td>"
                f"<td>{f.train.trades}</td><td>{_fmt(f.train.expectancy_r, 'r')}</td>"
                f"<td>{f.test.trades}</td>"
                f'<td class="{_signed_class(f.test.expectancy_r)}">'
                f"{_fmt(f.test.expectancy_r, 'r')}</td>"
                f"<td>{_fmt(f.test.profit_factor)}</td></tr>"
                for f in wf.folds
            )
            parts.append(f'<div class="scroll"><table>{head}{rows}</table></div>')
        else:
            parts.append("<p>The period is too short for a walk-forward test.</p>")
    parts.append("<h2>Trades</h2>")
    parts.append(_trades_table(result.result.trades))
    parts.append("<h2>What this backtest cannot see</h2>")
    parts.append("<ul>" + "".join(f"<li>{_e(n)}</li>" for n in result.limitations) + "</ul>")
    parts.append(f"<p>Reproduce with: <code>{_e(command)}</code></p>")
    parts.append(f"<footer>{_e(DISCLAIMER)}</footer>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>GoldSweep backtest</title><style>{_CSS}</style></head>"
        f"<body><main>{''.join(parts)}</main></body></html>"
    )


def write_report(result: BacktestResult, out_dir: Path, command: str) -> dict[str, Path]:
    """Write every output file; returns their paths by name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    balance = float(result.settings_snapshot["backtest"]["initial_balance"])  # type: ignore[index]
    paths = {
        "report": out_dir / "report.html",
        "summary": out_dir / "summary.json",
        "trades": out_dir / "trades.csv",
        "signals": out_dir / "signals.csv",
        "equity": out_dir / "equity.csv",
    }
    write_trades_csv(paths["trades"], result.result.trades)
    write_signals_csv(paths["signals"], result.result.outcomes)
    write_equity_csv(paths["equity"], result.result.trades, balance, result.start)
    wf = result.walk_forward
    if wf is not None:
        paths["oos_trades"] = out_dir / "walk_forward_trades.csv"
        write_trades_csv(paths["oos_trades"], wf.out_of_sample.trades)
    paths["summary"].write_text(
        json.dumps(summary_json(result, command), indent=2, default=str), encoding="utf-8"
    )
    paths["report"].write_text(render_html(result, command), encoding="utf-8")
    return paths

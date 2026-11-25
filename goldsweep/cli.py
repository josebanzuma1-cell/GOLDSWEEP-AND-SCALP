"""Command-line interface.

Phase 1 provides the environment check::

    python main.py --check                     # MetaTrader 5
    python main.py --check --mock              # offline, synthetic prices
    python main.py --check --date 2026-03-09   # session table for another day

The report is plain ASCII so it prints on any Windows console.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import textwrap
from collections.abc import Callable, Sequence
from typing import TextIO
from zoneinfo import ZoneInfo

from goldsweep import __version__
from goldsweep.analysis.pressure import STRUCTURE_BARS, pressure_meter
from goldsweep.core.clock import ASIA, UTC, Clock, SessionCalculator, SessionSpan, SystemClock
from goldsweep.core.logging import get_logger, setup_logging, shutdown_logging
from goldsweep.core.server_time import ServerTimeState, format_offset
from goldsweep.core.settings import ConfigError, Settings, load_settings
from goldsweep.core.types import Mode, Timeframe
from goldsweep.data.broker import (
    AccountType,
    BrokerClient,
    BrokerConnectionError,
    BrokerError,
    BrokerUnavailableError,
)
from goldsweep.data.factory import create_broker
from goldsweep.storage.db import Database

EXIT_OK = 0
EXIT_BROKER = 1
EXIT_CONFIG = 2
EXIT_RUNNING = 4  # another bot is already running (D119)


def _alone(settings: Settings, run: Callable[[], int]) -> int:
    """Run the bot only if no other copy is running (D119): the scheduled tasks and a
    start by hand would otherwise send every signal twice."""
    from goldsweep.core.instance import AlreadyRunning, single_instance

    try:
        with single_instance(settings.data_dir):
            return run()
    except AlreadyRunning:
        print(
            "GoldSweep is already running (started by the scheduled task or another "
            "window): this copy stops. Nothing was changed."
        )
        return EXIT_RUNNING


#: An interactive check should answer within a couple of minutes, not retry for ten.
CHECK_CONNECT_ATTEMPTS = 2
CHECK_CONNECT_TIMEOUT_MS = 30_000

BrokerFactory = Callable[[], BrokerClient]
Emit = Callable[[str], None]

_SERVER_TIME_SOURCES = {
    "config": "set in config.yaml",
    "live_tick": "measured from a live tick",
    "cache": "remembered from the last live measurement",
    "assumed": "ASSUMED, not measured yet",
    "mock": "mock broker",
}


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


def _kv(label: str, value: str) -> str:
    """``Label       : value``; an empty label continues the previous line."""
    return f"{label:<12}: {value}" if label else f"{'':<14}{value}"


def _kv_wrapped(label: str, value: str, width: int = 84) -> list[str]:
    """Like :func:`_kv`, with a long value folded onto continuation lines."""
    parts = textwrap.wrap(value, width=width) or [""]
    return [_kv(label, parts[0]), *(_kv("", part) for part in parts[1:])]


def _heading(title: str) -> list[str]:
    return ["", title, "-" * len(title)]


def _clock_text(moment: dt.datetime, zone: dt.tzinfo, ref_date: dt.date) -> str:
    """``HH:MM`` in ``zone``, marked ``(+1d)`` / ``(-1d)`` when it falls on another date."""
    local = moment.astimezone(zone)
    text = f"{local:%H:%M}"
    days = (local.date() - ref_date).days
    return text if days == 0 else f"{text}({days:+d}d)"


def _range_text(span: SessionSpan, zone: dt.tzinfo, ref_date: dt.date) -> str:
    start = _clock_text(span.start, zone, ref_date)
    return f"{start}-{_clock_text(span.end, zone, ref_date)}"


def _both_zones(moment: dt.datetime, zone: ZoneInfo) -> str:
    utc, local = moment.astimezone(UTC), moment.astimezone(zone)
    return f"{utc:%a %Y-%m-%d %H:%M} UTC = {local:%a %Y-%m-%d %H:%M} {zone.key}"


def _format_table(rows: Sequence[Sequence[str]]) -> list[str]:
    """Left-aligned text table; the first row is the header."""
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines: list[str] = []
    for number, row in enumerate(rows):
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        lines.append("  ".join(cells).rstrip())
        if number == 0:
            lines.append("  ".join("-" * width for width in widths))
    return lines


# --------------------------------------------------------------------------- #
# Report sections
# --------------------------------------------------------------------------- #


def header_lines(settings: Settings, db: Database, now: dt.datetime, zone: ZoneInfo) -> list[str]:
    title = f"GoldSweep {__version__} - environment check"
    mode = settings.mode
    if mode is Mode.PAPER:
        mode_note = "no orders are ever sent"
    else:
        mode_note = "live requested; a typed confirmation is still required at startup"
    local = now.astimezone(zone)
    lines = [
        title,
        "=" * len(title),
        _kv("Mode", f"{mode.value}  ({mode_note})"),
        _kv("", settings.mode_summary),
    ]
    if settings.env.mode is not settings.config.mode:
        lines.append(_kv("", "the two switches disagree, so PAPER applies"))
    lines += [
        _kv("Config", str(settings.config_path)),
        _kv(".env file", str(settings.env_file) if settings.env_file else "none (not required)"),
        _kv("Database", f"{db.path}  (schema v{db.schema_version})"),
        _kv("Log folder", str(settings.log_dir)),
        _kv(
            "Time now",
            f"{now:%Y-%m-%d %H:%M:%S} UTC  =  {local:%Y-%m-%d %H:%M:%S} {zone.key}",
        ),
    ]
    return lines


def _trade_text(calc: SessionCalculator, span: SessionSpan) -> str:
    """What the "New trades" column says for one window."""
    if span.blocks_trading:
        return "blocked"
    last_instant = span.end - dt.timedelta(microseconds=1)
    if calc.is_weekend(span.start) and calc.is_weekend(last_instant):
        return "closed (weekend)"
    if calc.after_friday_cutoff(span.start):
        return "no (Friday cutoff)"
    if span.trade:
        return "on"
    return "off (range only)" if span.name == ASIA else "off"


def status_lines(calc: SessionCalculator, now: dt.datetime, zone: ZoneInfo) -> list[str]:
    """Where the market stands right now and when the next tradeable window opens."""
    status = calc.status(now)
    lines: list[str] = []
    reopen = calc.next_market_open(now)
    if reopen is not None:
        lines.append(_kv("Market", f"CLOSED (weekend); reopens {_both_zones(reopen, zone)}"))
    else:
        active = [span.label for span in calc.active_spans(now)]
        where = "inside " + ", ".join(active) if active else "between session windows"
        lines.append(_kv("Market", f"open; {where}"))
    if status.allowed:
        lines.append(
            _kv("New trades", "allowed by the clock (news, spread and risk checks come later)")
        )
    else:
        lines.append(_kv("New trades", "NOT allowed now: " + "; ".join(status.reasons)))
    upcoming = calc.next_tradeable_span(now)
    if upcoming is not None:
        opens = _both_zones(upcoming.start, zone)
        lines.append(_kv("Next window", f"{upcoming.label} opens {opens}"))
    return lines


def session_lines(
    calc: SessionCalculator, day: dt.date, zone: ZoneInfo, *, now: dt.datetime | None
) -> list[str]:
    """Session table for calendar date ``day``; ``now`` adds the live status lines."""
    rows: list[tuple[str, ...]] = [
        ("Session", "Defined as (local time)", "UTC", zone.key, "New trades")
    ]
    for span in calc.spans_for_date(day):
        rows.append(
            (
                span.label,
                span.definition,
                _range_text(span, UTC, day),
                _range_text(span, zone, day),
                _trade_text(calc, span),
            )
        )
    cutoff = calc.friday_cutoff_instant(day)
    rows.append(
        (
            "Friday cutoff",
            calc.friday_cutoff_definition,
            _clock_text(cutoff, UTC, day),
            _clock_text(cutoff, zone, day),
            "applies today" if day.weekday() == 4 else "Fridays only",
        )
    )
    lines = _heading(f"Sessions for {day:%A %Y-%m-%d}")
    lines.extend(_format_table(rows))
    if any("d)" in cell for row in rows[1:] for cell in row[2:4]):
        lines.append("(+1d) / (-1d): the time falls on the next / previous calendar day.")
    if now is not None:
        lines.append("")
        lines.extend(status_lines(calc, now, zone))
    return lines


def _server_time_text(state: ServerTimeState, now: dt.datetime) -> str:
    source = _SERVER_TIME_SOURCES.get(state.source, state.source)
    trust = "confirmed" if state.confirmed else "NOT confirmed"
    text = (
        f"UTC{format_offset(state.rule.utc_offset(now))} now; rule {state.rule.describe()} "
        f"({source}; {trust})"
    )
    skew = state.clock_skew_seconds
    if skew is not None and abs(skew) >= 2.0:
        text += f"; PC clock differs from the server by {skew:+.1f}s"
    return text


def _report_broker(
    emit: Emit, broker: BrokerClient, settings: Settings, db: Database, clock: Clock
) -> ServerTimeState:
    """Connect, then print account, symbol and price details.

    Returns:
        How the broker's timestamps are being converted to UTC, so the caller
        can fail the check when that conversion is in doubt.

    Raises:
        BrokerError: any step failed. Lines already printed stay printed.
    """
    broker.connect()
    account = broker.account_info()
    terminal = broker.terminal_info()
    currency = account.currency
    link = "up" if terminal.connected else "DOWN"
    algo = "enabled" if terminal.trade_allowed else "disabled"
    emit(
        _kv(
            "Terminal",
            f"{terminal.name} build {terminal.build} ({terminal.company}); "
            f"server link {link}; algo trading {algo}"
            + (f"; ping {terminal.ping_ms:g} ms" if terminal.ping_ms is not None else ""),
        )
    )
    emit(_kv("Account", f"{account.login} @ {account.server}  [{account.account_type.value}]"))
    emit(
        _kv(
            "Funds",
            f"balance {account.balance:,.2f} {currency} | equity {account.equity:,.2f} {currency}"
            f" | free margin {account.margin_free:,.2f} {currency} | leverage 1:{account.leverage}",
        )
    )
    if account.account_type is AccountType.REAL:
        emit(
            _kv("WARNING", "this is a REAL-money account. Build and test on a DEMO account first.")
        )

    spec = broker.resolve_symbol(settings.config.symbol_candidates)
    symbol = spec.name
    tick = broker.get_tick(symbol)
    spread = broker.spread_points(symbol)
    trigger = settings.config.timeframes.trigger
    candles = broker.get_rates(symbol, trigger, 200)
    meter = settings.config.pressure
    pressure_bars = broker.get_rates(symbol, meter.timeframe, meter.window)
    hourly = broker.get_rates(symbol, Timeframe.H1, STRUCTURE_BARS)
    now = clock.now()  # read after connecting, which can take a minute

    state = broker.server_time
    emit(_kv("Server time", _server_time_text(state, now)))
    if state.warning:
        for line in _kv_wrapped("WARNING", state.warning):
            emit(line)

    for line in _heading("Symbol"):
        emit(line)
    digits = spec.digits
    emit(_kv("Resolved", f"{symbol}  ({spec.description})"))
    emit(_kv("Candidates", ", ".join(settings.config.symbol_candidates)))
    emit(_kv("Trade mode", spec.trade_mode.value))
    emit(
        _kv(
            "Price steps",
            f"{digits} digits; point {spec.point:g}; tick size {spec.tick_size:g}",
        )
    )
    emit(
        _kv(
            "Contract",
            f"{spec.contract_size:g} units per lot; tick value {spec.tick_value:g} {currency}",
        )
    )
    emit(
        _kv(
            "",
            f"a 1.00 price move on 1.00 lot = {spec.value_per_price_unit:,.2f} {currency}",
        )
    )
    emit(
        _kv(
            "Volume",
            f"min {spec.volume_min:g}; step {spec.volume_step:g}; max {spec.volume_max:g} lots",
        )
    )
    emit(_kv("Stops level", f"{spec.stops_level} points"))
    emit(
        _kv(
            "Quote",
            f"bid {tick.bid:.{digits}f} / ask {tick.ask:.{digits}f} "
            f"at {tick.time:%Y-%m-%d %H:%M:%S} UTC",
        )
    )
    emit(_kv("Spread", f"{spread:g} points ({tick.spread:.{digits}f} in price)"))
    label = f"Last {trigger.value} bar"
    if candles.empty:
        emit(_kv(label, "none available yet"))
    else:
        bar = candles.iloc[-1]
        emit(
            _kv(
                label,
                f"opened {candles.index[-1]:%Y-%m-%d %H:%M} UTC | "
                f"O {bar['open']:.{digits}f} H {bar['high']:.{digits}f} "
                f"L {bar['low']:.{digits}f} C {bar['close']:.{digits}f} (closed bars only)",
            )
        )
    reading = pressure_meter(
        pressure_bars,
        meter,
        hourly=hourly,
        swing_lookback=settings.config.strategy.swing_lookback,
    )
    if reading is None:
        emit(_kv("Pressure", "no bars to read yet"))
    else:
        parts = reading.components
        emit(
            _kv(
                "Pressure",
                f"{reading.describe()} over the last {reading.bars} {meter.timeframe.value} bars",
            )
        )
        emit(
            _kv(
                "",
                f"volume {parts['volume']:+.2f} | candles {parts['candles']:+.2f} | "
                f"H1 structure {parts['structure']:+.2f} | news {parts['news']:+.2f} "
                "(the news part is filled in by --news and the trading loop)",
            )
        )
        emit(_kv("", "an estimate from tick volume and candles, not real order flow"))
    stored = db.upsert_candles(symbol, trigger, candles)
    db.record_spread(symbol, now, spread)
    emit(_kv("Stored", f"{stored} {trigger.value} candles and 1 spread sample in SQLite"))
    return state


def _broker_failure_lines(exc: BrokerError) -> list[str]:
    """The failure, followed by the advice that fits its cause."""
    lines = _kv_wrapped("FAILED", str(exc))
    if isinstance(exc, BrokerUnavailableError):
        lines += [
            "",
            "To run the check against MetaTrader 5:",
            "  1. Install MetaTrader 5, open it and log in to a DEMO account",
            "     (File > Open an Account). Wait until it shows a live connection.",
            "  2. In this project's Python environment run: pip install MetaTrader5",
            "  3. In .env either fill in MT5_LOGIN, MT5_PASSWORD and MT5_SERVER, or leave all",
            "     three blank to use the account the terminal is already logged in to.",
        ]
    elif isinstance(exc, BrokerConnectionError):
        lines += [
            "",
            "If .env sets MT5_LOGIN, MT5_PASSWORD and MT5_SERVER, check all three. Leave all",
            "three blank to use the account the terminal is already logged in to.",
        ]
    if isinstance(exc, BrokerUnavailableError | BrokerConnectionError):
        lines += ["", "To try everything else offline: python main.py --check --mock"]
    return lines


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def run_check(
    settings: Settings,
    *,
    use_mock: bool = False,
    day: dt.date | None = None,
    clock: Clock | None = None,
    broker_factory: BrokerFactory | None = None,
    out: TextIO | None = None,
) -> int:
    """Print the environment report and return a process exit code.

    Args:
        settings: loaded settings.
        use_mock: use the offline mock broker instead of MetaTrader 5.
        day: show the session table for this date instead of today.
        clock: time source (tests inject a fixed clock).
        broker_factory: builds the broker (tests inject a failing or fake one).
        out: where to print; defaults to standard output.

    Returns:
        ``EXIT_OK``, or ``EXIT_BROKER`` when the broker step failed. The session
        table is printed either way because it needs no broker.
    """
    stream = out if out is not None else sys.stdout

    def emit(line: str = "") -> None:
        print(line, file=stream, flush=True)  # show progress while MT5 connects

    active_clock: Clock = clock or SystemClock()
    now = active_clock.now()
    zone = settings.display_zone
    # The mock writes to its own file so synthetic prices never mix with real ones.
    db = Database(settings.mock_db_path if use_mock else settings.db_path)
    # The report itself says what went wrong; log records go to the file and SQLite only.
    setup_logging(settings.log_dir, db=db, console=False)
    log = get_logger("goldsweep.check")
    exit_code = EXIT_OK
    failure = "the broker step did not complete (the session table above is still valid)"
    note = ""
    try:
        for line in header_lines(settings, db, now, zone):
            emit(line)

        calc = SessionCalculator(settings.config.sessions)
        ref_day = day if day is not None else now.astimezone(zone).date()
        for line in session_lines(calc, ref_day, zone, now=None if day is not None else now):
            emit(line)

        title = (
            "Broker: MOCK (synthetic prices, not market data)"
            if use_mock
            else "Broker: MetaTrader 5"
        )
        for line in _heading(title):
            emit(line)
        broker: BrokerClient | None = None
        try:
            if broker_factory is not None:
                broker = broker_factory()
            else:
                if not use_mock:
                    emit("Connecting (this can take up to a minute) ...")
                broker = create_broker(
                    settings,
                    mock=use_mock,
                    clock=active_clock,
                    state=db,
                    connect_attempts=CHECK_CONNECT_ATTEMPTS,
                    connect_timeout_ms=CHECK_CONNECT_TIMEOUT_MS,
                )
            server_time = _report_broker(emit, broker, settings, db, active_clock)
            if server_time.warning:
                # Wrong timestamps would shift every session window: that is a failure.
                exit_code = EXIT_BROKER
                failure = "the broker server-time setting needs fixing (see WARNING above)"
            elif not server_time.confirmed:
                note = (
                    "Note: the broker server time could not be confirmed (no live ticks). "
                    "Run the check again while the market is open."
                )
        except BrokerError as exc:
            exit_code = EXIT_BROKER
            for line in _broker_failure_lines(exc):
                emit(line)
            log.error("environment check: broker step failed", error=str(exc))
        finally:
            if broker is not None:
                broker.shutdown()

        emit("")
        if exit_code == EXIT_OK:
            emit("Result: OK")
            if note:
                emit(note)
        else:
            emit(f"Result: FAILED: {failure}")
        log.info(
            "environment check finished",
            exit_code=exit_code,
            mode=settings.mode.value,
            mock=use_mock,
        )
        return exit_code
    finally:
        shutdown_logging()
        db.close()


def _parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date in YYYY-MM-DD form: {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="GoldSweep: XAUUSD news + liquidity-sweep alert and trading assistant.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print account info, the resolved gold symbol and today's session times, then exit",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use the offline mock broker (synthetic prices) instead of MT5",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="with --check: show the session table for this date instead of today",
    )
    parser.add_argument(
        "--news",
        action="store_true",
        help="refresh the calendar and headlines, classify them and print the news report",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="run paper trading live on the broker feed (PAPER mode only; Ctrl+C stops it)",
    )
    parser.add_argument(
        "--unattended",
        action="store_true",
        help="with --live: no console question (Task Scheduler); live orders wait for "
        "/golive <account> on Telegram",
    )
    parser.add_argument(
        "--watchdog",
        action="store_true",
        help="check once that the bot is running; alert on Telegram if not (Task Scheduler)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="open the dashboard in the browser (with --mock: the --mock database)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="trade on the MT5 account (needs MODE=LIVE in .env, mode: LIVE in config.yaml "
        "and the account number typed at startup; demo accounts only in Phase 6)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="with --news, --paper or --live: keyword rules only (no model calls)",
    )
    parser.add_argument("--config", default=None, metavar="PATH", help="path to config.yaml")
    parser.add_argument("--version", action="version", version=f"GoldSweep {__version__}")
    return parser


def _make_output_safe() -> None:
    """Never let a character the console cannot show crash the program."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns the process exit code."""
    _make_output_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.check or args.news or args.paper or args.live or args.dashboard or args.watchdog):
        parser.print_help()
        print("\nStart with: python main.py --check   (then --news, or --paper to paper-trade)")
        return EXIT_OK
    try:
        settings = load_settings(config_path=args.config)
    except ConfigError as exc:
        print(f"Configuration error\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.watchdog:
        from goldsweep.watchdog import run_watchdog

        return run_watchdog(settings)
    if args.dashboard:
        from goldsweep.dashboard.launch import run_dashboard

        return run_dashboard(settings, use_mock=args.mock)
    if args.live:
        if args.mock:
            print("--live trades on MetaTrader 5; --mock is for --check, --news and --paper.")
            return EXIT_CONFIG
        from goldsweep.live import run_live

        return _alone(
            settings,
            lambda: run_live(settings, use_llm=not args.no_llm, unattended=args.unattended),
        )
    if args.paper:
        from goldsweep.live import run_paper

        return _alone(
            settings, lambda: run_paper(settings, use_mock=args.mock, use_llm=not args.no_llm)
        )
    if args.news:
        from goldsweep.news_report import run_news

        return run_news(settings, use_mock=args.mock, use_llm=not args.no_llm)
    return run_check(settings, use_mock=args.mock, day=args.date)

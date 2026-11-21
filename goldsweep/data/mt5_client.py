"""MetaTrader 5 market-data client.

The ``MetaTrader5`` Python package only exists for Windows, so it is imported
lazily inside :meth:`MT5Client.connect`. Unit tests inject a fake module, which
keeps this file testable on any operating system.

Three things here are easy to get wrong and are handled deliberately:

* **Server time.** MT5 reports bar and tick times in the broker's server clock,
  not UTC. Every timestamp is converted with a
  :class:`~goldsweep.core.server_time.ServerTimeRule`; see that module.
* **Closed bars only.** Position 0 in MT5 is usually the bar still forming, but
  it is a finished bar whenever no tick has arrived since the boundary (quiet
  market, weekend). Only the newest bar is ever in doubt, and it is judged by
  the clock, not by its position.
* **Reconnects.** Calls that fail because the link to the terminal dropped are
  retried after reconnecting with exponential backoff.
"""

from __future__ import annotations

import datetime as dt
import time as _time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd

from goldsweep.core.clock import UTC, Clock, SystemClock, ensure_utc
from goldsweep.core.logging import get_logger
from goldsweep.core.server_time import (
    AUTO,
    DEFAULT_ASSUMED_RULE,
    ServerTimeRule,
    ServerTimeState,
    format_offset,
    offset_from_tick,
    rule_for_offset,
)
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import (
    AccountInfo,
    AccountType,
    BrokerClient,
    BrokerConnectionError,
    BrokerError,
    BrokerUnavailableError,
    SymbolSpec,
    TerminalInfo,
    Tick,
    TradeMode,
)
from goldsweep.data.candles import empty_candles

if TYPE_CHECKING:
    from goldsweep.core.settings import Settings

log = get_logger(__name__)

RES_E_AUTH_FAILED = -6
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
RES_E_IPC_TIMEOUT = -10005
#: ``last_error()`` codes that mean the link to the terminal is broken.
IPC_ERROR_CODES = frozenset({-10000, -10001, -10002, -10003, -10004, -10005})

_TRADE_MODES: dict[int, TradeMode] = {
    0: TradeMode.DISABLED,
    1: TradeMode.LONG_ONLY,
    2: TradeMode.SHORT_ONLY,
    3: TradeMode.CLOSE_ONLY,
    4: TradeMode.FULL,
}
_ACCOUNT_TYPES: dict[int, AccountType] = {
    0: AccountType.DEMO,
    1: AccountType.CONTEST,
    2: AccountType.REAL,
}

_VERIFY_INTERVAL = dt.timedelta(hours=6)  # re-check a confirmed server clock this often
_RETRY_CONFIRMED = dt.timedelta(minutes=30)  # ... or this soon if the market was shut
_RETRY_UNCONFIRMED = dt.timedelta(seconds=60)  # keep trying while the clock is a guess
_UNCONFIRMED_MARGIN = dt.timedelta(hours=14)  # largest plausible error of a guessed offset
_SKEW_WARNING_SECONDS = 3.0


class StateStore(Protocol):
    """Small persistent key/value store (implemented by ``storage.db.Database``)."""

    def get_state(self, key: str, default: Any = None) -> Any: ...

    def set_state(self, key: str, value: Any) -> None: ...


def _import_mt5() -> Any:
    try:
        import MetaTrader5  # type: ignore[import-not-found]
    except ImportError as exc:
        raise BrokerUnavailableError(
            "the MetaTrader5 Python package is not installed or does not support this "
            "platform (it is Windows-only). Install it with 'pip install MetaTrader5' on the "
            "PC that runs the MT5 terminal, or use the offline mock: "
            "python main.py --check --mock"
        ) from exc
    return MetaTrader5


def _connection_hint(error_code: int) -> str:
    """What the user can do about a failed connection, by MT5 error code."""
    if error_code == RES_E_IPC_TIMEOUT:
        return (
            "The terminal was found but did not answer. That usually means it is still "
            "starting, is showing a dialog, or has no trading account logged in. Open "
            "MetaTrader 5, log in to a demo account (File > Open an Account), wait until it "
            "shows a live connection, then try again."
        )
    return "Is MetaTrader 5 installed and able to start on this PC?"


def _tick_server_epoch(raw_tick: Any) -> float:
    """Tick time as MT5 reports it: server wall clock encoded as epoch seconds."""
    milliseconds = int(getattr(raw_tick, "time_msc", 0) or 0)
    return milliseconds / 1000.0 if milliseconds > 0 else float(raw_tick.time)


def _spec_from_info(info: Any) -> SymbolSpec:
    return SymbolSpec(
        name=str(info.name),
        description=str(getattr(info, "description", "")),
        digits=int(info.digits),
        point=float(info.point),
        contract_size=float(info.trade_contract_size),
        tick_size=float(info.trade_tick_size),
        tick_value=float(info.trade_tick_value),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        stops_level=int(info.trade_stops_level),
        freeze_level=int(getattr(info, "trade_freeze_level", 0)),
        # An unknown mode is treated as disabled: the safe reading.
        trade_mode=_TRADE_MODES.get(int(info.trade_mode), TradeMode.DISABLED),
        filling_mode=int(getattr(info, "filling_mode", 0)),
        currency_base=str(getattr(info, "currency_base", "")),
        currency_profit=str(getattr(info, "currency_profit", "")),
        currency_margin=str(getattr(info, "currency_margin", "")),
    )


class MT5Client(BrokerClient):
    """Read-only access to a MetaTrader 5 terminal: account, symbol, bars, ticks, spread."""

    name = "mt5"

    def __init__(
        self,
        *,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        path: Path | str | None = None,
        server_timezone: str = AUTO,
        clock: Clock | None = None,
        state: StateStore | None = None,
        mt5: Any | None = None,
        sleep: Callable[[float], None] = _time.sleep,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        init_timeout_ms: int = 60_000,
        closed_bar_margin: dt.timedelta = dt.timedelta(seconds=1),
        liveness_polls: int = 6,
        liveness_interval: float = 0.25,
    ) -> None:
        """Create the client. Nothing is contacted until :meth:`connect`.

        Args:
            login, password, server: account to log in to. All ``None`` means
                "use whatever account the terminal is already logged in to".
            path: ``terminal64.exe`` to start. ``None`` lets the package find it.
            server_timezone: ``"auto"`` or a rule understood by
                :meth:`ServerTimeRule.parse`.
            clock: time source (tests inject a manual clock).
            state: where the last confirmed server-time rule is remembered.
            mt5: stand-in for the ``MetaTrader5`` module (tests).
            sleep: stand-in for ``time.sleep`` (tests).
            max_attempts: connection attempts, and reconnect attempts per call.
            backoff_base, backoff_max: delay is ``base * 2**n`` seconds, capped.
            init_timeout_ms: how long the terminal may take to start.
            closed_bar_margin: grace period after a bar's end before it counts
                as closed, absorbing late ticks and small clock differences.
            liveness_polls, liveness_interval: how long to watch for a fresh
                tick when measuring the server clock.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._login = login
        self._password = password
        self._server = server
        self._path = Path(path) if path is not None else None
        self._clock: Clock = clock or SystemClock()
        self._state_store = state
        self._mt5 = mt5
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._init_timeout_ms = init_timeout_ms
        self._closed_bar_margin = closed_bar_margin
        self._liveness_polls = liveness_polls
        self._liveness_interval = liveness_interval

        self._auto_server_time = server_timezone.strip().lower() == AUTO
        if self._auto_server_time:
            self._state = ServerTimeState(DEFAULT_ASSUMED_RULE, source="assumed", confirmed=False)
        else:
            self._state = ServerTimeState(
                ServerTimeRule.parse(server_timezone), source="config", confirmed=True
            )
        self._connected = False
        self._server_name = ""
        self._next_time_check: dt.datetime | None = None
        self._warned_unconfirmed = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Clock | None = None,
        state: StateStore | None = None,
        mt5: Any | None = None,
        max_attempts: int | None = None,
        init_timeout_ms: int | None = None,
    ) -> MT5Client:
        """Build a client from ``.env`` (login, path) and config.yaml (server time zone).

        ``max_attempts`` and ``init_timeout_ms`` override how often and how long
        connecting is tried: an interactive check wants an answer sooner than an
        unattended bot does.
        """
        env = settings.env
        options: dict[str, Any] = {}
        if max_attempts is not None:
            options["max_attempts"] = max_attempts
        if init_timeout_ms is not None:
            options["init_timeout_ms"] = init_timeout_ms
        return cls(
            login=env.mt5_login,
            password=env.mt5_password.get_secret_value() if env.mt5_password else None,
            server=env.mt5_server,
            path=env.mt5_path,
            server_timezone=settings.config.broker.server_timezone,
            clock=clock,
            state=state,
            mt5=mt5,
            **options,
        )

    # -- connection -------------------------------------------------------- #

    def connect(self) -> None:
        """Start or attach to the terminal and log in.

        Raises:
            BrokerUnavailableError: package or terminal missing.
            BrokerConnectionError: login rejected, or still failing after all attempts.
        """
        if self._path is not None and not self._path.exists():
            raise BrokerUnavailableError(
                f"MT5_PATH points to '{self._path}', which does not exist. Install "
                "MetaTrader 5 or correct MT5_PATH in .env (leave it blank to let the "
                "package find the terminal)."
            )
        if self._mt5 is None:
            self._mt5 = _import_mt5()

        error: tuple[int, str] = (0, "")
        for attempt in range(1, self._max_attempts + 1):
            if self._try_connect():
                self._connected = True
                self._after_connect()
                return
            error = self._last_error()
            self._safe_shutdown()
            if error[0] == RES_E_AUTH_FAILED:
                raise BrokerConnectionError(
                    "MetaTrader 5 rejected the login. Check MT5_LOGIN, MT5_PASSWORD and "
                    f"MT5_SERVER in .env. ({error[1]})"
                )
            if attempt < self._max_attempts:
                delay = self._backoff_delay(attempt)
                log.warning(
                    "mt5 connect failed, retrying",
                    attempt=attempt,
                    delay_seconds=delay,
                    error_code=error[0],
                    error=error[1],
                )
                self._sleep(delay)
        raise BrokerConnectionError(
            f"could not connect to MetaTrader 5 after {self._max_attempts} attempts "
            f"(last error {error[0]}: {error[1]}). {_connection_hint(error[0])}"
        )

    def _backoff_delay(self, attempt: int) -> float:
        return float(min(self._backoff_base * 2 ** (attempt - 1), self._backoff_max))

    def _try_connect(self) -> bool:
        """One attempt: initialize, log in if credentials were given, read the account."""
        mt5 = self._mt5
        # The terminal path is a positional argument in the MetaTrader5 API.
        args: tuple[Any, ...] = (str(self._path),) if self._path is not None else ()
        if not mt5.initialize(*args, timeout=self._init_timeout_ms):
            return False
        current = mt5.account_info()
        already = (
            current is not None
            and self._login is not None
            and int(current.login) == int(self._login)
            and (not self._server or str(getattr(current, "server", "")) == self._server)
        )
        # Logging in again to the account the terminal already shows counts as an
        # account change in MT5, which can switch its Algo Trading button off.
        if self._login is not None and not already:
            credentials: dict[str, Any] = {}
            if self._password:
                credentials["password"] = self._password
            if self._server:
                credentials["server"] = self._server
            if not mt5.login(self._login, **credentials):
                return False
        account = mt5.account_info()
        if account is None:
            return False
        if self._login is not None and int(account.login) != int(self._login):
            mt5.shutdown()
            raise BrokerConnectionError(
                f"the terminal is logged in to account {account.login}, not the "
                f"configured MT5_LOGIN {self._login}"
            )
        self._server_name = str(getattr(account, "server", ""))
        return True

    def _after_connect(self) -> None:
        if self._auto_server_time and not self._state.confirmed:
            cached = self._load_cached_rule()
            if cached is not None:
                self._state = ServerTimeState(cached, source="cache", confirmed=False)
        self._next_time_check = None
        log.info(
            "mt5 connected",
            server=self._server_name,
            server_time_rule=self._state.rule.spec,
            server_time_source=self._state.source,
        )

    def _safe_shutdown(self) -> None:
        try:
            self._mt5.shutdown()
        except Exception as exc:  # shutting down a dead link must never raise
            log.debug("mt5 shutdown raised", error=str(exc))

    def shutdown(self) -> None:
        if self._mt5 is not None and self._connected:
            self._safe_shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        if not self._connected or self._mt5 is None:
            return False
        info = self._mt5.terminal_info()
        return info is not None and bool(info.connected)

    def _require_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise BrokerConnectionError("MT5 client is not connected: call connect() first")

    def _last_error(self) -> tuple[int, str]:
        try:
            code, message = self._mt5.last_error()
            return int(code), str(message)
        except Exception:
            return 0, "unknown error"

    def _call(self, function: str, *args: Any, **kwargs: Any) -> Any:
        """Call an MT5 API function, reconnecting with backoff when the link dropped.

        Returns the function's result, or ``None`` when it failed for a reason
        other than a broken link (the caller decides what that means).
        """
        self._require_connected()
        for attempt in range(1, self._max_attempts + 1):
            api = getattr(self._mt5, function)
            # Never pass an empty ``**kwargs``: the real package's order_check and
            # order_send then fail with "Unnamed arguments not allowed".
            result = api(*args, **kwargs) if kwargs else api(*args)
            if result is not None:
                return result
            code, message = self._last_error()
            if code not in IPC_ERROR_CODES:
                return None
            delay = self._backoff_delay(attempt)
            log.warning(
                "mt5 link lost, reconnecting",
                call=function,
                attempt=attempt,
                delay_seconds=delay,
                error_code=code,
                error=message,
            )
            self._sleep(delay)
            self._safe_shutdown()
            self._try_connect()
        raise BrokerConnectionError(
            f"MT5 call {function} still failing after {self._max_attempts} reconnect attempts"
        )

    # -- for the order gateway (execution/mt5_gateway.py) -------------------- #

    @property
    def module(self) -> Any:
        """The connected ``MetaTrader5`` module (its constants and trade functions)."""
        self._require_connected()
        return self._mt5

    def call(self, function: str, *args: Any, **kwargs: Any) -> Any:
        """An MT5 API call with the client's reconnect-and-backoff handling.

        Returns ``None`` when the call failed for a reason other than a broken link;
        :meth:`last_error` then says why.
        """
        return self._call(function, *args, **kwargs)

    def last_error(self) -> tuple[int, str]:
        return self._last_error()

    def server_epoch_to_utc(self, seconds: float) -> dt.datetime:
        """An MT5 timestamp (server wall clock as epoch seconds) as true UTC."""
        wall_clock = dt.datetime.fromtimestamp(float(seconds), tz=UTC).replace(tzinfo=None)
        return self._state.rule.to_utc(wall_clock)

    def utc_to_server_epoch(self, moment: dt.datetime) -> int:
        """True UTC as the server-clock epoch seconds MT5 charts use."""
        return int(self._to_mt5_time(ensure_utc(moment)).timestamp())

    # -- account and terminal ---------------------------------------------- #

    def account_info(self) -> AccountInfo:
        raw = self._call("account_info")
        if raw is None:
            raise BrokerError(f"MT5 account_info failed: {self._last_error()[1]}")
        margin = float(raw.margin)
        return AccountInfo(
            login=int(raw.login),
            server=str(raw.server),
            company=str(raw.company),
            currency=str(raw.currency),
            balance=float(raw.balance),
            equity=float(raw.equity),
            margin=margin,
            margin_free=float(raw.margin_free),
            margin_level=float(raw.margin_level) if margin > 0 else None,
            leverage=int(raw.leverage),
            # An unknown account type is treated as REAL: the cautious reading.
            account_type=_ACCOUNT_TYPES.get(int(raw.trade_mode), AccountType.REAL),
            trade_allowed=bool(raw.trade_allowed) and bool(getattr(raw, "trade_expert", True)),
            # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING is 2; anything else nets positions.
            hedging=int(getattr(raw, "margin_mode", 0)) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        )

    def terminal_info(self) -> TerminalInfo:
        raw = self._call("terminal_info")
        if raw is None:
            raise BrokerError(f"MT5 terminal_info failed: {self._last_error()[1]}")
        return TerminalInfo(
            name=str(raw.name),
            company=str(raw.company),
            build=int(raw.build),
            connected=bool(raw.connected),
            trade_allowed=bool(raw.trade_allowed),
            path=str(raw.path),
            data_path=str(raw.data_path),
            common_data_path=str(raw.commondata_path),
        )

    # -- symbols ----------------------------------------------------------- #

    def _lookup_symbol(self, name: str) -> SymbolSpec | None:
        info = self._call("symbol_info", name)
        return None if info is None else _spec_from_info(info)

    def _list_gold_symbol_names(self) -> list[str]:
        symbols = self._call("symbols_get", group="*XAU*,*GOLD*")
        return [str(symbol.name) for symbol in symbols] if symbols else []

    def _activate_symbol(self, spec: SymbolSpec) -> SymbolSpec:
        """Add the symbol to Market Watch so the terminal streams its ticks and bars."""
        if not self._mt5.symbol_select(spec.name, True):
            raise BrokerError(
                f"MT5 could not add {spec.name} to Market Watch: {self._last_error()[1]}"
            )
        return self._lookup_symbol(spec.name) or spec

    # -- server time ------------------------------------------------------- #

    @property
    def server_time(self) -> ServerTimeState:
        return self._state

    @property
    def _cache_key(self) -> str:
        return f"mt5.server_time.{self._server_name or 'unknown'}"

    def _load_cached_rule(self) -> ServerTimeRule | None:
        if self._state_store is None:
            return None
        try:
            cached = self._state_store.get_state(self._cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("spec"), str):
                return ServerTimeRule.parse(cached["spec"])
        except Exception as exc:
            log.warning("ignoring unreadable cached server-time rule", error=str(exc))
        return None

    def _save_cached_rule(self, rule: ServerTimeRule, offset: dt.timedelta) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.set_state(
                self._cache_key,
                {
                    "spec": rule.spec,
                    "offset_minutes": int(offset.total_seconds() // 60),
                    "detected_at": self._clock.now().isoformat(),
                },
            )
        except Exception as exc:
            log.warning("could not remember the server-time rule", error=str(exc))

    def _detect_live_offset(self, symbol: str) -> tuple[dt.timedelta, float] | None:
        """Measure the server offset, but only from a tick that is provably live.

        A stale tick (closed market) compared with the PC clock can look like a
        plausible offset, so the tick time must be seen to advance first.
        """
        first = self._call("symbol_info_tick", symbol)
        if first is None:
            return None
        first_stamp = _tick_server_epoch(first)
        for _ in range(self._liveness_polls):
            self._sleep(self._liveness_interval)
            tick = self._call("symbol_info_tick", symbol)
            if tick is None:
                return None
            stamp = _tick_server_epoch(tick)
            if stamp != first_stamp:
                return offset_from_tick(stamp, self._clock.now())
        return None

    def _ensure_server_time(self, symbol: str) -> None:
        """Confirm, or periodically re-confirm, how server time maps to UTC."""
        now = self._clock.now()
        if self._next_time_check is not None and now < self._next_time_check:
            return
        detected = self._detect_live_offset(symbol)
        if detected is None:
            retry = _RETRY_CONFIRMED if self._state.confirmed else _RETRY_UNCONFIRMED
            self._next_time_check = now + retry
            if not self._state.confirmed and not self._warned_unconfirmed:
                self._warned_unconfirmed = True
                log.warning(
                    "mt5 server time is not confirmed (no live ticks); the newest bar is "
                    "withheld until the market is live",
                    rule=self._state.rule.spec,
                    source=self._state.source,
                )
            return
        offset, skew = detected
        self._next_time_check = now + _VERIFY_INTERVAL
        self._apply_detection(offset, skew, now)

    def _apply_detection(self, offset: dt.timedelta, skew: float, now: dt.datetime) -> None:
        previous = self._state
        expected = previous.rule.utc_offset(now)
        if previous.source == "config":
            if expected == offset:
                self._state = replace(
                    previous,
                    confirmed=True,
                    observed_offset=offset,
                    clock_skew_seconds=skew,
                    warning=None,
                )
            else:
                warning = (
                    f"broker.server_timezone={previous.rule.spec!r} in config.yaml means "
                    f"UTC{format_offset(expected)}, but the live server clock is "
                    f"UTC{format_offset(offset)}. Fix the setting or use 'auto'."
                )
                # A wrong pin would shift every session window: stop trusting it.
                self._state = replace(
                    previous,
                    confirmed=False,
                    observed_offset=offset,
                    clock_skew_seconds=skew,
                    warning=warning,
                )
                log.error("mt5 server time mismatch", detail=warning)
        else:
            if expected == offset:
                rule = previous.rule
            else:
                rule = rule_for_offset(offset, now)
                if previous.confirmed:
                    log.critical(
                        "mt5 server clock offset changed",
                        old_rule=previous.rule.spec,
                        new_rule=rule.spec,
                    )
            self._state = ServerTimeState(
                rule,
                source="live_tick",
                confirmed=True,
                observed_offset=offset,
                clock_skew_seconds=skew,
            )
            self._save_cached_rule(rule, offset)
        if abs(skew) > _SKEW_WARNING_SECONDS:
            log.warning(
                "PC clock differs from the trade server; turn on Windows time synchronisation",
                skew_seconds=round(skew, 1),
            )

    def _to_mt5_time(self, moment: dt.datetime) -> dt.datetime:
        """True UTC -> the "server time labelled as UTC" datetime the MT5 API expects."""
        return self._state.rule.from_utc(moment).replace(tzinfo=UTC)

    # -- prices ------------------------------------------------------------ #

    def _timeframe_constant(self, timeframe: Timeframe) -> Any:
        return getattr(self._mt5, f"TIMEFRAME_{timeframe.value}")

    def _rates_to_frame(self, rates: Any) -> pd.DataFrame:
        """MT5 structured array (server time) -> candle frame (true UTC)."""
        if rates is None or len(rates) == 0:
            return empty_candles()
        raw = pd.DataFrame(rates)
        server_times = pd.DatetimeIndex(pd.to_datetime(raw["time"].to_numpy(), unit="s"))
        frame = pd.DataFrame(
            {
                "open": raw["open"].to_numpy(dtype="float64"),
                "high": raw["high"].to_numpy(dtype="float64"),
                "low": raw["low"].to_numpy(dtype="float64"),
                "close": raw["close"].to_numpy(dtype="float64"),
                "tick_volume": raw["tick_volume"].to_numpy(dtype="int64"),
                "spread": raw["spread"].to_numpy(dtype="int64"),
            },
            index=self._state.rule.index_to_utc(server_times).rename("time"),
        )
        return frame.loc[~frame.index.duplicated(keep="last")].sort_index()

    def _drop_forming(self, frame: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
        """Remove the newest bar unless the clock says it has closed.

        Older rows are closed by definition, because a newer bar exists. With an
        unconfirmed server clock the newest bar is withheld for as long as the
        guessed offset could be hiding that it is still forming.
        """
        if frame.empty:
            return frame
        margin = self._closed_bar_margin if self._state.confirmed else _UNCONFIRMED_MARGIN
        newest_close = ensure_utc(frame.index[-1].to_pydatetime()) + timeframe.duration
        if newest_close + margin > self._clock.now():
            return frame.iloc[:-1]
        return frame

    def get_rates(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        include_forming: bool = False,
    ) -> pd.DataFrame:
        if count <= 0:
            raise ValueError("count must be positive")
        self._ensure_server_time(symbol)
        wanted = count if include_forming else count + 1  # one spare in case bar 0 is forming
        rates = self._call(
            "copy_rates_from_pos", symbol, self._timeframe_constant(timeframe), 0, wanted
        )
        if rates is None:
            raise BrokerError(
                f"MT5 returned no {timeframe.value} bars for {symbol}: {self._last_error()[1]}"
            )
        frame = self._rates_to_frame(rates)
        if not include_forming:
            frame = self._drop_forming(frame, timeframe)
        return frame.tail(count)

    def get_rates_range(
        self, symbol: str, timeframe: Timeframe, start: dt.datetime, end: dt.datetime
    ) -> pd.DataFrame:
        first, last = ensure_utc(start), ensure_utc(end)
        if last <= first:
            raise ValueError("end must be after start")
        self._ensure_server_time(symbol)
        rates = self._call(
            "copy_rates_range",
            symbol,
            self._timeframe_constant(timeframe),
            self._to_mt5_time(first),
            self._to_mt5_time(last),
        )
        if rates is None:
            raise BrokerError(
                f"MT5 returned no {timeframe.value} bars for {symbol}: {self._last_error()[1]}"
            )
        frame = self._rates_to_frame(rates)
        frame = frame.loc[(frame.index >= first) & (frame.index < last)]
        return self._drop_forming(frame, timeframe)

    def get_tick(self, symbol: str) -> Tick:
        self._ensure_server_time(symbol)
        raw = self._call("symbol_info_tick", symbol)
        if raw is None:
            raise BrokerError(f"MT5 has no tick for {symbol}: {self._last_error()[1]}")
        server_wall_clock = dt.datetime.fromtimestamp(_tick_server_epoch(raw), tz=UTC).replace(
            tzinfo=None
        )
        return Tick(
            time=self._state.rule.to_utc(server_wall_clock),
            bid=float(raw.bid),
            ask=float(raw.ask),
            last=float(getattr(raw, "last", 0.0)),
        )

"""In-memory stand-in for the ``MetaTrader5`` package, which only exists for Windows.

It reproduces the behaviours the real package is known for. The important one:
every timestamp it returns is the trade server's wall-clock time encoded as if
it were UTC, so a client that forgets to convert ends up hours off.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import numpy as np

from goldsweep.core.clock import Clock

RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)

SUCCESS = (1, "Success")
Bar = tuple[dt.datetime, float, float, float, float]
Row = tuple[dt.datetime, float, float, float, float, int, int]


def gold_symbol(name: str = "XAUUSD", **overrides: Any) -> SimpleNamespace:
    """A symbol_info() result for a typical spot-gold contract."""
    values: dict[str, Any] = {
        "name": name,
        "description": "Gold vs US Dollar",
        "digits": 2,
        "point": 0.01,
        "trade_contract_size": 100.0,
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "trade_mode": 4,  # SYMBOL_TRADE_MODE_FULL
        "filling_mode": 3,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
        "visible": False,
        "select": False,
        "spread": 21,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeMT5:
    """Scriptable replacement for the MetaTrader5 module object."""

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(
        self,
        clock: Clock,
        *,
        server_offset: dt.timedelta | Callable[[dt.datetime], dt.timedelta] = dt.timedelta(hours=2),
        symbols: Iterable[SimpleNamespace] | None = None,
        login: int = 5012345,
        server: str = "Demo-Server",
        account_trade_mode: int = 0,  # ACCOUNT_TRADE_MODE_DEMO
        live: bool = True,
    ) -> None:
        self.clock = clock
        #: Server clock minus UTC. A callable makes it depend on the instant (DST).
        self.server_offset = server_offset
        chosen = list(symbols) if symbols is not None else [gold_symbol()]
        self.symbols: dict[str, SimpleNamespace] = {symbol.name: symbol for symbol in chosen}
        #: True: every tick request returns a newer tick. False: the market is shut.
        self.live = live
        #: True UTC time of the last tick while the market is shut.
        self.last_tick_time: dt.datetime | None = None
        #: Simulates a PC clock that runs this many seconds behind the true time.
        self.pc_clock_behind_seconds = 0.0
        self.login_switches_account = True
        self.bid = 2654.31
        self.ask = 2654.52
        self.account = SimpleNamespace(
            login=login,
            server=server,
            company="Demo Broker Ltd",
            currency="USD",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            margin_free=10_000.0,
            margin_level=0.0,
            leverage=100,
            trade_mode=account_trade_mode,
            margin_mode=2,  # ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
            trade_allowed=True,
            trade_expert=True,
            name="Test User",
        )
        self.terminal = SimpleNamespace(
            name="MetaTrader 5",
            company="MetaQuotes Ltd.",
            build=5200,
            connected=True,
            trade_allowed=True,
            path=r"C:\MT5",
            data_path=r"C:\MT5\data",
            commondata_path=r"C:\MT5\common",
        )
        self.bars: dict[int, list[Row]] = {}
        #: Every call made, as (function name, positional args, keyword args).
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        #: Scripted results for successive initialize() / login() calls (default: success).
        self.initialize_results: list[bool] = []
        self.login_results: list[bool] = []
        #: What last_error() reports after a failed initialize().
        self.initialize_error: tuple[int, str] = (-10003, "IPC initialize failed")
        #: Scripted failures: function name -> queue of last_error() tuples.
        self.errors: dict[str, list[tuple[int, str]]] = {}
        self._last_error: tuple[int, str] = SUCCESS
        self._initialized = False
        self._tick_sequence = 0

    # -- test helpers ------------------------------------------------------ #

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)

    def calls_named(self, name: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return [call for call in self.calls if call[0] == name]

    def add_bars(
        self, timeframe: int, bars: Iterable[Bar], *, volume: int = 100, spread: int = 21
    ) -> None:
        """Add bars given by their TRUE UTC open time; they are served in server time."""
        rows = self.bars.setdefault(timeframe, [])
        for open_time, open_, high, low, close in bars:
            rows.append((open_time, open_, high, low, close, volume, spread))
        rows.sort(key=lambda row: row[0])

    def _fail(self, name: str) -> bool:
        queue = self.errors.get(name)
        if queue:
            self._last_error = queue.pop(0)
            return True
        return False

    def _offset_seconds(self, at: dt.datetime) -> int:
        offset = self.server_offset(at) if callable(self.server_offset) else self.server_offset
        return int(offset.total_seconds())

    def _to_array(self, rows: list[Row]) -> np.ndarray:
        out = np.zeros(len(rows), dtype=RATES_DTYPE)
        for position, (open_time, open_, high, low, close, volume, spread) in enumerate(rows):
            server_epoch = int(open_time.timestamp()) + self._offset_seconds(open_time)
            out[position] = (server_epoch, open_, high, low, close, volume, spread, 0)
        return out

    # -- MetaTrader5 API --------------------------------------------------- #

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        self._record("initialize", *args, **kwargs)
        ok = self.initialize_results.pop(0) if self.initialize_results else True
        self._initialized = ok
        self._last_error = SUCCESS if ok else self.initialize_error
        return ok

    def login(self, login: int, **kwargs: Any) -> bool:
        self._record("login", login, **kwargs)
        ok = self.login_results.pop(0) if self.login_results else True
        if ok and self.login_switches_account:
            self.account.login = login
        self._last_error = SUCCESS if ok else (-6, "Authorization failed")
        return ok

    def shutdown(self) -> None:
        self._record("shutdown")
        self._initialized = False

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def account_info(self) -> SimpleNamespace | None:
        self._record("account_info")
        if not self._initialized:
            self._last_error = (-10004, "No IPC connection")
            return None
        if self._fail("account_info"):
            return None
        return self.account

    def terminal_info(self) -> SimpleNamespace | None:
        self._record("terminal_info")
        if not self._initialized:
            self._last_error = (-10004, "No IPC connection")
            return None
        if self._fail("terminal_info"):
            return None
        return self.terminal

    def symbol_info(self, name: str) -> SimpleNamespace | None:
        self._record("symbol_info", name)
        if self._fail("symbol_info"):
            return None
        info = self.symbols.get(name)
        if info is None:
            self._last_error = (-4, "Symbol not found")
        return info

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self._record("symbol_select", name, enable)
        info = self.symbols.get(name)
        if info is None:
            self._last_error = (-4, "Symbol not found")
            return False
        info.visible = bool(enable)
        info.select = bool(enable)
        return True

    def symbols_get(self, group: str | None = None) -> tuple[SimpleNamespace, ...]:
        self._record("symbols_get", group=group)
        if group is None:
            return tuple(self.symbols.values())
        patterns = [
            pattern.strip().upper()
            for pattern in group.split(",")
            if pattern.strip() and not pattern.strip().startswith("!")
        ]
        return tuple(
            symbol
            for symbol in self.symbols.values()
            if any(fnmatch.fnmatchcase(symbol.name.upper(), pattern) for pattern in patterns)
        )

    def symbol_info_tick(self, name: str) -> SimpleNamespace | None:
        self._record("symbol_info_tick", name)
        if self._fail("symbol_info_tick"):
            return None
        if name not in self.symbols:
            self._last_error = (-4, "Symbol not found")
            return None
        if self.live:
            self._tick_sequence += 1
            true_time = self.clock.now() + dt.timedelta(seconds=self.pc_clock_behind_seconds)
            epoch_ms = int(true_time.timestamp() * 1000) + self._tick_sequence
        else:
            true_time = self.last_tick_time or (self.clock.now() - dt.timedelta(hours=30))
            epoch_ms = int(true_time.timestamp() * 1000)
        server_ms = epoch_ms + self._offset_seconds(true_time) * 1000
        return SimpleNamespace(
            time=server_ms // 1000,
            time_msc=server_ms,
            bid=self.bid,
            ask=self.ask,
            last=0.0,
            volume=0,
        )

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> np.ndarray | None:
        self._record("copy_rates_from_pos", symbol, timeframe, start_pos, count)
        if self._fail("copy_rates_from_pos"):
            return None
        rows = self.bars.get(timeframe, [])
        end = len(rows) - start_pos  # position 0 is the newest bar
        if end <= 0:
            return self._to_array([])
        return self._to_array(rows[max(0, end - count) : end])

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: dt.datetime, date_to: dt.datetime
    ) -> np.ndarray | None:
        self._record("copy_rates_range", symbol, timeframe, date_from, date_to)
        if self._fail("copy_rates_range"):
            return None
        low, high = int(date_from.timestamp()), int(date_to.timestamp())
        rows = [
            row
            for row in self.bars.get(timeframe, [])
            if low <= int(row[0].timestamp()) + self._offset_seconds(row[0]) <= high
        ]
        return self._to_array(rows)

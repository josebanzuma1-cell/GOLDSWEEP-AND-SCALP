"""Broker interface shared by the live MT5 client and the offline mock.

Contract for every implementation:

* Every timestamp that leaves a broker object is true UTC (timezone-aware).
* Candle frames follow :mod:`goldsweep.data.candles`. Unless
  ``include_forming=True`` is requested, only fully closed bars are returned,
  so strategy code can never see a bar that is still forming (no look-ahead).
* Instrument properties (contract size, digits, point, tick value, volume
  limits) are read from the broker, never assumed.

Order execution is not part of this interface yet. It arrives with the
executors in Phases 5 and 6.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self

import pandas as pd

from goldsweep.core.logging import get_logger
from goldsweep.core.server_time import ServerTimeState
from goldsweep.core.types import Timeframe

log = get_logger(__name__)


class BrokerError(Exception):
    """A broker operation failed."""


class BrokerUnavailableError(BrokerError):
    """The broker software is not installed or cannot run on this machine."""


class BrokerConnectionError(BrokerError):
    """The broker terminal or the trade server cannot be reached."""


class SymbolNotFoundError(BrokerError):
    """No usable gold symbol exists on this account."""


class TradeMode(StrEnum):
    """What the broker allows on a symbol."""

    DISABLED = "DISABLED"
    LONG_ONLY = "LONG_ONLY"
    SHORT_ONLY = "SHORT_ONLY"
    CLOSE_ONLY = "CLOSE_ONLY"
    FULL = "FULL"


class AccountType(StrEnum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"


#: MT5 swap modes this project models (SYMBOL_SWAP_MODE_*).
SWAP_MODE_DISABLED, SWAP_MODE_POINTS, SWAP_MODE_CURRENCY_DEPOSIT = 0, 1, 4


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Instrument properties exactly as the broker reports them."""

    name: str
    description: str
    digits: int
    point: float
    contract_size: float
    tick_size: float
    tick_value: float  # account currency per tick, for 1.00 lot
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # minimum stop distance, in points
    freeze_level: int
    trade_mode: TradeMode
    filling_mode: int  # raw broker bit flags; decoded by the executor in Phase 6
    currency_base: str
    currency_profit: str
    currency_margin: str
    #: Overnight financing as the broker states it (S4): per lot per night in
    #: ``swap_mode`` units (MT5: 0 none, 1 points, 4 account currency); the triple
    #: swap falls on ``swap_rollover3days`` (MT5 numbering: 0 Sunday ... 3 Wednesday).
    swap_mode: int = 0
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_rollover3days: int = 3

    def swap_per_lot(self, long: bool) -> float | None:
        """Account currency per lot per night (negative: paid), or None for a swap mode
        this project does not model."""
        rate = self.swap_long if long else self.swap_short
        if self.swap_mode == SWAP_MODE_DISABLED:
            return 0.0
        if self.swap_mode == SWAP_MODE_POINTS:
            return rate * self.point * self.value_per_price_unit
        if self.swap_mode == SWAP_MODE_CURRENCY_DEPOSIT:
            return rate
        return None

    @property
    def value_per_price_unit(self) -> float:
        """Profit or loss, in account currency, of 1.00 lot when price moves by 1.00."""
        if self.tick_size <= 0:
            raise BrokerError(f"{self.name}: the broker reports a non-positive tick size")
        return self.tick_value / self.tick_size

    @property
    def tradable(self) -> bool:
        """True when the broker allows opening both long and short positions."""
        return self.trade_mode is TradeMode.FULL


@dataclass(frozen=True, slots=True)
class Tick:
    """Latest quote. ``time`` is true UTC."""

    time: dt.datetime
    bid: float
    ask: float
    last: float = 0.0

    @property
    def spread(self) -> float:
        """Ask minus bid, in price units."""
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: int
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float | None  # percent; None while no margin is in use
    leverage: int
    account_type: AccountType
    trade_allowed: bool
    #: True for a hedging account (several positions per symbol). A netting account
    #: merges every position on a symbol into one.
    hedging: bool = True

    @property
    def is_demo(self) -> bool:
        return self.account_type is AccountType.DEMO


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    name: str
    company: str
    build: int
    connected: bool  # terminal is connected to the trade server
    trade_allowed: bool  # the terminal's "Algo Trading" button is on
    path: str
    data_path: str
    common_data_path: str  # holds Common\Files, used for signals.json in Phase 6
    #: The terminal's last measured round trip to the trade server (S2), when known.
    ping_ms: float | None = None


def empty_ticks() -> pd.DataFrame:
    """A tick frame with no rows: columns ``bid`` and ``ask``, a UTC ``time`` index."""
    return pd.DataFrame(
        {"bid": pd.Series(dtype="float64"), "ask": pd.Series(dtype="float64")},
        index=pd.DatetimeIndex([], tz="UTC", name="time"),
    )


def rank_gold_symbols(names: Iterable[str], exclude: Iterable[str] = ()) -> list[str]:
    """Order broker symbol names by how likely they are to be spot gold in USD.

    ``XAUUSD`` with a suffix or prefix comes first, then names starting with
    ``GOLD``; shorter names win within each group. Other metals and crosses
    such as ``XAUEUR`` never match.
    """
    skip = set(exclude)
    scored: list[tuple[int, int, str]] = []
    for name in names:
        if name in skip:
            continue
        upper = name.upper()
        if upper.startswith("XAUUSD"):
            group = 0
        elif "XAUUSD" in upper:
            group = 1
        elif upper.startswith("GOLD"):
            group = 2
        else:
            continue
        scored.append((group, len(name), name))
    return [name for _group, _length, name in sorted(scored)]


class BrokerClient(ABC):
    """Market-data side of a broker connection."""

    name: str = "broker"

    # -- connection -------------------------------------------------------- #

    @abstractmethod
    def connect(self) -> None:
        """Open the connection. Raises :class:`BrokerError` on failure."""

    @abstractmethod
    def shutdown(self) -> None:
        """Close the connection. Safe to call more than once."""

    @abstractmethod
    def is_connected(self) -> bool: ...

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()

    # -- account and terminal ---------------------------------------------- #

    @abstractmethod
    def account_info(self) -> AccountInfo: ...

    @abstractmethod
    def terminal_info(self) -> TerminalInfo: ...

    @property
    @abstractmethod
    def server_time(self) -> ServerTimeState:
        """How this client converts broker timestamps to UTC, and whether that is confirmed."""

    # -- symbols ----------------------------------------------------------- #

    @abstractmethod
    def _lookup_symbol(self, name: str) -> SymbolSpec | None:
        """Properties of ``name``, or ``None`` when the account has no such symbol."""

    @abstractmethod
    def _list_gold_symbol_names(self) -> list[str]:
        """Names of every symbol on the account that might be gold."""

    def _activate_symbol(self, spec: SymbolSpec) -> SymbolSpec:
        """Hook run once on the resolved symbol (MT5 adds it to Market Watch)."""
        return spec

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Properties of ``symbol``.

        Raises:
            SymbolNotFoundError: the account has no such symbol.
        """
        spec = self._lookup_symbol(symbol)
        if spec is None:
            raise SymbolNotFoundError(f"symbol {symbol!r} is not available on this account")
        return spec

    def resolve_symbol(self, candidates: Sequence[str]) -> SymbolSpec:
        """Find the account's spot-gold symbol.

        ``candidates`` (from config.yaml) are tried in order; the first one that
        exists and is not disabled wins. If none exists, the account's symbol
        list is searched for ``XAUUSD`` / ``GOLD`` variants quoted in USD.

        Raises:
            SymbolNotFoundError: nothing usable was found.
        """
        tried: list[str] = []
        disabled: list[str] = []
        for name in candidates:
            tried.append(name)
            spec = self._lookup_symbol(name)
            if spec is None:
                continue
            if spec.trade_mode is TradeMode.DISABLED:
                disabled.append(name)
                continue
            if spec.currency_profit and spec.currency_profit.upper() != "USD":
                log.warning(
                    "configured gold symbol is not quoted in USD",
                    symbol=name,
                    currency_profit=spec.currency_profit,
                )
            return self._activate_symbol(spec)

        for name in rank_gold_symbols(self._list_gold_symbol_names(), exclude=tried):
            spec = self._lookup_symbol(name)
            if spec is None:
                continue
            if spec.trade_mode is TradeMode.DISABLED:
                disabled.append(name)
                continue
            if spec.currency_profit.upper() != "USD":
                continue  # a gold cross such as XAUEUR, or unknown: not what we trade
            log.warning(
                "gold symbol found by name search; add it to symbol_candidates in "
                "config.yaml to make the choice explicit",
                symbol=name,
                candidates=list(candidates),
            )
            return self._activate_symbol(spec)

        detail = f" Found but disabled for trading: {', '.join(disabled)}." if disabled else ""
        raise SymbolNotFoundError(
            f"no tradable gold symbol found (tried {', '.join(tried) or 'nothing'}, "
            f"then searched for XAUUSD / GOLD names).{detail}"
        )

    # -- prices ------------------------------------------------------------ #

    @abstractmethod
    def get_rates(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        include_forming: bool = False,
    ) -> pd.DataFrame:
        """The latest ``count`` bars, oldest first.

        Only closed bars are returned unless ``include_forming`` is true, in
        which case the last row may be the bar that is still forming.
        """

    @abstractmethod
    def get_rates_range(
        self, symbol: str, timeframe: Timeframe, start: dt.datetime, end: dt.datetime
    ) -> pd.DataFrame:
        """Closed bars whose open time lies in ``[start, end)`` (both true UTC)."""

    @abstractmethod
    def get_tick(self, symbol: str) -> Tick:
        """The latest quote."""

    def get_ticks(self, symbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        """Every bid/ask change in ``[start, end)`` (true UTC), oldest first (D106).

        Columns ``bid`` and ``ask``; the index (``time``) is the tick's UTC time to the
        millisecond. Brokers keep only a limited tick history.
        """
        raise BrokerError(f"the {self.name} broker does not serve tick history")

    def spread_points(self, symbol: str) -> float:
        """Current spread in broker points (ask minus bid, divided by the point size)."""
        spec = self.symbol_spec(symbol)
        if spec.point <= 0:
            raise BrokerError(f"{symbol}: the broker reports a non-positive point size")
        tick = self.get_tick(symbol)
        return round(tick.spread / spec.point, 1)

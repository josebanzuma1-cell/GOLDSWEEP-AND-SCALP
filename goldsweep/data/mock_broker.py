"""Offline mock broker with repeatable synthetic gold prices.

Used by unit tests and by ``python main.py --check --mock`` so the project can
be developed and tested on any operating system without MetaTrader 5.

The prices are a seeded random walk. They look like a gold chart (quiet Asia,
busier London and New York, a daily break, closed weekends) but they are NOT
market data and say nothing about how a strategy would perform. The mock is
only ever used when asked for explicitly; it is never a silent fallback.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from goldsweep.core.clock import Clock, SystemClock, ensure_utc
from goldsweep.core.server_time import ServerTimeRule, ServerTimeState
from goldsweep.core.types import Timeframe
from goldsweep.data.broker import (
    AccountInfo,
    AccountType,
    BrokerClient,
    BrokerConnectionError,
    BrokerError,
    SymbolSpec,
    TerminalInfo,
    Tick,
    TradeMode,
    empty_ticks,
)
from goldsweep.data.candles import aggregate_candles, empty_candles, validate_candles

_NEW_YORK = ZoneInfo("America/New_York")
_LONDON = ZoneInfo("Europe/London")
_ONE_MINUTE = pd.Timedelta(minutes=1)


def default_gold_spec(name: str = "XAUUSD", **overrides: object) -> SymbolSpec:
    """A typical spot-gold contract: 100 oz per lot, two decimals, USD-quoted."""
    values: dict[str, object] = {
        "name": name,
        "description": "Gold vs US Dollar (mock)",
        "digits": 2,
        "point": 0.01,
        "contract_size": 100.0,
        "tick_size": 0.01,
        "tick_value": 1.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "stops_level": 0,
        "freeze_level": 0,
        "trade_mode": TradeMode.FULL,
        "filling_mode": 3,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
    }
    values.update(overrides)
    return SymbolSpec(**values)  # type: ignore[arg-type]


def market_open_mask(index: pd.DatetimeIndex) -> np.ndarray:
    """True where spot gold normally trades.

    Open from Sunday 18:00 to Friday 17:00 New York time, with a daily break
    from 17:00 to 18:00. Computed in New York local time, so it follows US
    daylight saving on its own.
    """
    new_york = index.tz_convert(_NEW_YORK)
    weekday = np.asarray(new_york.day_of_week)  # Monday = 0
    hour = np.asarray(new_york.hour)
    closed = (
        (weekday == 5)
        | ((weekday == 6) & (hour < 18))
        | ((weekday == 4) & (hour >= 17))
        | (hour == 17)
    )
    return ~closed


def _volatility_multiplier(index: pd.DatetimeIndex) -> np.ndarray:
    """Quiet outside London hours, normal in London, busiest in the New York morning."""
    london_hour = np.asarray(index.tz_convert(_LONDON).hour)
    new_york_hour = np.asarray(index.tz_convert(_NEW_YORK).hour)
    multiplier = np.full(len(index), 0.6)
    multiplier[(london_hour >= 7) & (london_hour < 16)] = 1.0
    multiplier[(new_york_hour >= 8) & (new_york_hour < 12)] = 1.4
    return multiplier


def generate_m1_candles(
    start: dt.datetime,
    end: dt.datetime,
    *,
    seed: int = 7,
    start_price: float = 2650.0,
    base_sigma: float = 0.45,
    spread_points: int = 20,
    digits: int = 2,
) -> pd.DataFrame:
    """Synthetic one-minute bars for ``[start, end)``; identical for identical arguments."""
    first = pd.Timestamp(ensure_utc(start)).floor("1min")
    last = pd.Timestamp(ensure_utc(end)).floor("1min")
    if last <= first:
        raise ValueError("end must be after start")
    index = pd.date_range(first, last, freq="1min", inclusive="left", name="time")
    index = index[market_open_mask(index)]
    count = len(index)
    if count == 0:
        return empty_candles()

    rng = np.random.default_rng(seed)
    multiplier = _volatility_multiplier(index)
    sigma = base_sigma * multiplier
    close = start_price + np.cumsum(rng.normal(0.0, 1.0, count) * sigma)
    close = np.maximum(close, start_price * 0.2)  # a random walk must not go negative
    open_ = np.empty(count)
    open_[0] = start_price
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 1.0, count)) * sigma * 0.5
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 1.0, count)) * sigma * 0.5

    open_, high, low, close = (np.round(values, digits) for values in (open_, high, low, close))
    high = np.maximum.reduce([high, open_, close])  # rounding must not break the OHLC order
    low = np.minimum.reduce([low, open_, close])
    volume = rng.poisson(60.0 * multiplier) + 1

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": volume.astype("int64"),
            "spread": np.full(count, spread_points, dtype="int64"),
        },
        index=index,
    )


class MockBroker(BrokerClient):
    """In-memory broker that only ever reveals bars closed before ``clock.now()``."""

    name = "mock"

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        seed: int = 7,
        start_price: float = 2650.0,
        history_days: int = 30,
        future_days: int = 7,
        spread_points: int = 20,
        balance: float = 10_000.0,
        account_type: AccountType = AccountType.DEMO,
        symbols: Sequence[SymbolSpec] | None = None,
        candles_m1: pd.DataFrame | None = None,
    ) -> None:
        """Create the mock.

        Args:
            clock: time source; advance a ``ManualClock`` to reveal more bars.
            seed: random seed of the synthetic price path.
            start_price: first price of the synthetic path.
            history_days: days of history generated before ``clock.now()``.
            future_days: days generated after it, for a clock that moves forward.
            spread_points: constant spread, in points.
            balance: account balance and equity.
            account_type: reported account type (DEMO unless a test says otherwise).
            symbols: instruments on the account; all share the one price path.
            candles_m1: use these one-minute bars instead of generating any.
        """
        self._clock: Clock = clock or SystemClock()
        specs = list(symbols) if symbols is not None else [default_gold_spec()]
        self._symbols: dict[str, SymbolSpec] = {spec.name: spec for spec in specs}
        self._spread_points = spread_points
        self._balance = balance
        self._account_type = account_type
        self._connected = False
        if candles_m1 is not None:
            self._m1 = validate_candles(candles_m1)
        else:
            now = self._clock.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            self._m1 = generate_m1_candles(
                midnight - dt.timedelta(days=history_days),
                now + dt.timedelta(days=future_days),
                seed=seed,
                start_price=start_price,
                spread_points=spread_points,
            )
        self._frames: dict[Timeframe, pd.DataFrame] = {Timeframe.M1: self._m1}

    # -- connection -------------------------------------------------------- #

    def connect(self) -> None:
        self._connected = True

    def shutdown(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerConnectionError("mock broker is not connected: call connect() first")

    # -- account and terminal ---------------------------------------------- #

    def account_info(self) -> AccountInfo:
        self._require_connected()
        return AccountInfo(
            login=99000001,
            server="GoldSweep-Mock",
            company="GoldSweep mock broker",
            currency="USD",
            balance=self._balance,
            equity=self._balance,
            margin=0.0,
            margin_free=self._balance,
            margin_level=None,
            leverage=100,
            account_type=self._account_type,
            trade_allowed=True,
        )

    def terminal_info(self) -> TerminalInfo:
        self._require_connected()
        return TerminalInfo(
            name="Mock terminal",
            company="GoldSweep",
            build=0,
            connected=True,
            trade_allowed=False,
            path="",
            data_path="",
            common_data_path="",
        )

    @property
    def server_time(self) -> ServerTimeState:
        return ServerTimeState(rule=ServerTimeRule(), source="mock", confirmed=True)

    # -- symbols ----------------------------------------------------------- #

    def _lookup_symbol(self, name: str) -> SymbolSpec | None:
        self._require_connected()
        return self._symbols.get(name)

    def _list_gold_symbol_names(self) -> list[str]:
        self._require_connected()
        return list(self._symbols)

    # -- prices ------------------------------------------------------------ #

    def _frame(self, timeframe: Timeframe) -> pd.DataFrame:
        if timeframe not in self._frames:
            self._frames[timeframe] = aggregate_candles(self._m1, timeframe)
        return self._frames[timeframe]

    def _closed(self, timeframe: Timeframe, now: pd.Timestamp) -> pd.DataFrame:
        """Bars of ``timeframe`` that have fully closed by ``now``."""
        frame = self._frame(timeframe)
        return frame.loc[frame.index <= now - timeframe.duration]

    def get_rates(
        self,
        symbol: str,
        timeframe: Timeframe,
        count: int,
        *,
        include_forming: bool = False,
    ) -> pd.DataFrame:
        self.symbol_spec(symbol)
        if count <= 0:
            raise ValueError("count must be positive")
        now = pd.Timestamp(self._clock.now())
        bars = self._closed(timeframe, now).tail(count)
        if include_forming and timeframe is not Timeframe.M1:
            bucket_start = now.floor(timeframe.pandas_freq)
            minutes = self._m1.loc[
                (self._m1.index >= bucket_start) & (self._m1.index <= now - _ONE_MINUTE)
            ]
            if not minutes.empty:
                forming = aggregate_candles(minutes, timeframe)
                bars = pd.concat([bars, forming]).tail(count)
        return bars.copy()

    def get_rates_range(
        self, symbol: str, timeframe: Timeframe, start: dt.datetime, end: dt.datetime
    ) -> pd.DataFrame:
        self.symbol_spec(symbol)
        first = pd.Timestamp(ensure_utc(start))
        last = pd.Timestamp(ensure_utc(end))
        if last <= first:
            raise ValueError("end must be after start")
        closed = self._closed(timeframe, pd.Timestamp(self._clock.now()))
        return closed.loc[(closed.index >= first) & (closed.index < last)].copy()

    def get_tick(self, symbol: str) -> Tick:
        spec = self.symbol_spec(symbol)
        now = self._clock.now()
        position = int(self._m1.index.searchsorted(pd.Timestamp(now) - _ONE_MINUTE, side="right"))
        if position == 0:
            raise BrokerError("mock broker has no price data before the current time")
        last_open = self._m1.index[position - 1]
        price = float(self._m1["close"].iloc[position - 1])
        last_close = ensure_utc((last_open + _ONE_MINUTE).to_pydatetime())
        spread = self._spread_points * spec.point
        bid = round(price - spread / 2.0, spec.digits)
        ask = round(bid + spread, spec.digits)
        return Tick(time=min(now, last_close), bid=bid, ask=ask, last=price)

    def get_ticks(self, symbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
        """Four ticks per closed one-minute bar (open, high, low, close at 0/15/30/45 s)."""
        spec = self.symbol_spec(symbol)
        first, last = pd.Timestamp(ensure_utc(start)), pd.Timestamp(ensure_utc(end))
        if last <= first:
            raise ValueError("end must be after start")
        bars = self._closed(Timeframe.M1, pd.Timestamp(self._clock.now()))
        bars = bars.loc[(bars.index >= first - _ONE_MINUTE) & (bars.index < last)]
        if bars.empty:
            return empty_ticks()
        offsets = [pd.Timedelta(seconds=s) for s in (0, 15, 30, 45)]
        columns = ("open", "high", "low", "close")
        times = [t + offset for t in bars.index for offset in offsets]
        prices = [float(bars.at[t, c]) for t in bars.index for c in columns]
        spread = self._spread_points * spec.point
        frame = pd.DataFrame(
            {"bid": prices, "ask": [round(p + spread, spec.digits) for p in prices]},
            index=pd.DatetimeIndex(times, name="time"),
        )
        return frame.loc[(frame.index >= first) & (frame.index < last)]

"""Orders on MetaTrader 5 (spec section 7). The only module that sends anything to a broker.

Every request that opens exposure (a market order or a pending limit order)
carries its stop-loss and take-profit in the same request; :meth:`open` refuses
to send one without them (ground rule 2). Before sending, ``order_check`` is
asked first; it is also how the filling mode is chosen: the symbol's allowed
modes (FOK, IOC, then RETURN) are tried until the server accepts one.

Requotes, "price changed", "price off", timeouts, "too many requests" and lost
connections are retried with exponential backoff and a fresh price; every other
answer is final. Each request and its answer is handed to an ``OrderLog`` so it
lands in SQLite (``order_log`` table).

Prices are rounded to the symbol's digits. Volumes come from the risk manager
already rounded down to the volume step.
"""

from __future__ import annotations

import datetime as dt
import math
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from goldsweep.core.logging import get_logger
from goldsweep.data.broker import AccountInfo, BrokerError, SymbolSpec, Tick
from goldsweep.data.mt5_client import MT5Client

log = get_logger(__name__)

# Trade server return codes (MQL5 TRADE_RETCODE_*).
RETCODE_TEXT: dict[int, str] = {
    10004: "requote",
    10006: "request rejected",
    10007: "request cancelled by the trader",
    10008: "order placed",
    10009: "request completed",
    10010: "only part of the request was completed",
    10011: "request processing error",
    10012: "request cancelled by timeout",
    10013: "invalid request",
    10014: "invalid volume",
    10015: "invalid price",
    10016: "invalid stops",
    10017: "trading is disabled",
    10018: "the market is closed",
    10019: "not enough money",
    10020: "prices changed",
    10021: "no quotes to process the request",
    10022: "invalid order expiration",
    10023: "order state changed",
    10024: "too frequent requests",
    10025: "no changes in the request",
    10026: "autotrading disabled by the server",
    10027: "autotrading disabled in the terminal (press Algo Trading)",
    10028: "request locked for processing",
    10029: "order or position frozen",
    10030: "unsupported filling mode",
    10031: "no connection with the trade server",
    10032: "only allowed for live accounts",
    10033: "pending order limit reached",
    10034: "volume limit for the symbol reached",
    10035: "incorrect or prohibited order type",
    10036: "position already closed",
    10038: "close volume exceeds the position",
    10039: "a close order already exists",
    10040: "position limit reached",
    10041: "pending order activation rejected",
    10042: "only long positions are allowed",
    10043: "only short positions are allowed",
    10044: "only closing is allowed",
    10045: "FIFO rule: close the oldest position first",
}
DONE_CODES = frozenset({10008, 10009, 10010})
RETRY_CODES = frozenset({10004, 10012, 10020, 10021, 10024, 10031})
INVALID_FILL = 10030
CHECK_OK = 0  # order_check answers 0 when the request would be accepted

# MetaTrader5 constants, used when the module does not provide them (tests).
_DEFAULTS: dict[str, int] = {
    "TRADE_ACTION_DEAL": 1,
    "TRADE_ACTION_PENDING": 5,
    "TRADE_ACTION_SLTP": 6,
    "TRADE_ACTION_REMOVE": 8,
    "ORDER_TYPE_BUY": 0,
    "ORDER_TYPE_SELL": 1,
    "ORDER_TYPE_BUY_LIMIT": 2,
    "ORDER_TYPE_SELL_LIMIT": 3,
    "ORDER_FILLING_FOK": 0,
    "ORDER_FILLING_IOC": 1,
    "ORDER_FILLING_RETURN": 2,
    "ORDER_TIME_GTC": 0,
    "POSITION_TYPE_BUY": 0,
    "DEAL_ENTRY_IN": 0,
    "DEAL_ENTRY_OUT": 1,
    "DEAL_ENTRY_INOUT": 2,
    "DEAL_ENTRY_OUT_BY": 3,
}
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# Why a deal happened (MQL5 DEAL_REASON_*).
DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_SO = 6


@dataclass(frozen=True, slots=True)
class OrderResult:
    """The trade server's answer to one request."""

    ok: bool
    retcode: int
    message: str
    ticket: int = 0  # the order (pending) or the order that opened the position
    deal: int = 0
    price: float = 0.0
    volume: float = 0.0

    @classmethod
    def failed(cls, message: str, retcode: int = -1) -> OrderResult:
        return cls(False, retcode, message)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticket: int
    identifier: int  # the ticket of the order that opened it
    long: bool
    volume: float
    price_open: float
    sl: float  # 0.0: none
    tp: float  # 0.0: none
    profit: float
    comment: str
    magic: int
    time: dt.datetime  # UTC


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    ticket: int
    long: bool
    volume: float
    price_open: float
    sl: float
    tp: float
    comment: str
    magic: int


@dataclass(frozen=True, slots=True)
class BrokerDeal:
    ticket: int
    order: int
    position_id: int
    entry: int  # 0 in, 1 out, 2 in/out, 3 out by
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    reason: int
    time: dt.datetime  # UTC

    @property
    def is_exit(self) -> bool:
        return self.entry in (1, 2, 3)

    @property
    def net(self) -> float:
        return self.profit + self.commission + self.swap + self.fee


class OrderLog(Protocol):
    def __call__(
        self, action: str, signal_id: str | None, request: dict[str, Any], result: OrderResult
    ) -> None: ...


def _no_log(
    action: str, signal_id: str | None, request: dict[str, Any], result: OrderResult
) -> None:
    return None


class MT5Gateway:
    """Orders, modifications, closes and queries for one symbol and one magic number."""

    def __init__(
        self,
        client: MT5Client,
        spec: SymbolSpec,
        *,
        magic: int,
        deviation_points: int,
        order_log: OrderLog = _no_log,
        sleep: Callable[[float], None] = _time.sleep,
        attempts: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._client = client
        self._spec = spec
        self._magic = magic
        self._deviation = deviation_points
        self._log_order = order_log
        self._sleep = sleep
        self._attempts = max(1, attempts)
        self._backoff = backoff_seconds

    @property
    def spec(self) -> SymbolSpec:
        return self._spec

    @property
    def magic(self) -> int:
        return self._magic

    def _const(self, name: str) -> int:
        return int(getattr(self._client.module, name, _DEFAULTS[name]))

    def price(self, value: float) -> float:
        return round(float(value), self._spec.digits)

    # -- account and market ----------------------------------------------------- #

    def account(self) -> AccountInfo:
        return self._client.account_info()

    def tick(self) -> Tick:
        return self._client.get_tick(self._spec.name)

    def margin_level_after(self, long: bool, volume: float, price: float) -> float | None:
        """Margin level (%) once ``volume`` more lots are open; ``None`` when unknown."""
        kind = self._const("ORDER_TYPE_BUY" if long else "ORDER_TYPE_SELL")
        needed = self._client.call("order_calc_margin", kind, self._spec.name, volume, price)
        if needed is None:
            return None
        account = self.account()
        used = account.margin + float(needed)
        return math.inf if used <= 0 else account.equity / used * 100.0

    # -- what is open -------------------------------------------------------------- #

    def positions(self) -> list[BrokerPosition]:
        """Open positions on the symbol that carry this bot's magic number."""
        raw = self._client.call("positions_get", symbol=self._spec.name)
        if raw is None:
            raise BrokerError(f"MT5 positions_get failed: {self._client.last_error()[1]}")
        buy = self._const("POSITION_TYPE_BUY")
        return [
            BrokerPosition(
                ticket=int(p.ticket),
                identifier=int(getattr(p, "identifier", p.ticket)),
                long=int(p.type) == buy,
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp),
                profit=float(p.profit),
                comment=str(p.comment),
                magic=int(p.magic),
                time=self._client.server_epoch_to_utc(float(p.time)),
            )
            for p in raw
            if int(p.magic) == self._magic
        ]

    def orders(self) -> list[BrokerOrder]:
        """Pending orders on the symbol that carry this bot's magic number."""
        raw = self._client.call("orders_get", symbol=self._spec.name)
        if raw is None:
            raise BrokerError(f"MT5 orders_get failed: {self._client.last_error()[1]}")
        buys = {self._const("ORDER_TYPE_BUY"), self._const("ORDER_TYPE_BUY_LIMIT")}
        return [
            BrokerOrder(
                ticket=int(o.ticket),
                long=int(o.type) in buys,
                volume=float(getattr(o, "volume_current", o.volume_initial)),
                price_open=float(o.price_open),
                sl=float(o.sl),
                tp=float(o.tp),
                comment=str(o.comment),
                magic=int(o.magic),
            )
            for o in raw
            if int(o.magic) == self._magic
        ]

    def deals(self, position_id: int) -> list[BrokerDeal]:
        """Every deal of one position (entry and exits), oldest first."""
        raw = self._client.call("history_deals_get", position=position_id)
        if not raw:
            return []
        deals = [
            BrokerDeal(
                ticket=int(d.ticket),
                order=int(d.order),
                position_id=int(d.position_id),
                entry=int(d.entry),
                volume=float(d.volume),
                price=float(d.price),
                profit=float(d.profit),
                commission=float(d.commission),
                swap=float(d.swap),
                fee=float(getattr(d, "fee", 0.0)),
                reason=int(d.reason),
                time=self._client.server_epoch_to_utc(float(d.time)),
            )
            for d in raw
        ]
        return sorted(deals, key=lambda d: (d.time, d.ticket))

    # -- sending ----------------------------------------------------------------- #

    def _fillings(self, pending: bool) -> list[int]:
        """Filling modes to try, most likely first."""
        flags = self._spec.filling_mode
        fok, ioc, ret = (
            self._const("ORDER_FILLING_FOK"),
            self._const("ORDER_FILLING_IOC"),
            self._const("ORDER_FILLING_RETURN"),
        )
        modes = []
        if flags & SYMBOL_FILLING_FOK:
            modes.append(fok)
        if flags & SYMBOL_FILLING_IOC:
            modes.append(ioc)
        modes.append(ret)
        if pending:  # a resting order is usually filled "return"
            modes.remove(ret)
            modes.insert(0, ret)
        return modes

    @staticmethod
    def _result(raw: Any, fallback: str) -> OrderResult:
        if raw is None:
            return OrderResult.failed(fallback)
        code = int(raw.retcode)
        text = str(getattr(raw, "comment", "") or "") or RETCODE_TEXT.get(code, "unknown answer")
        return OrderResult(
            ok=code in DONE_CODES,
            retcode=code,
            message=f"{code} {RETCODE_TEXT.get(code, text)}",
            ticket=int(getattr(raw, "order", 0) or 0),
            deal=int(getattr(raw, "deal", 0) or 0),
            price=float(getattr(raw, "price", 0.0) or 0.0),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
        )

    def _check(self, request: dict[str, Any]) -> tuple[bool, int, str]:
        raw = self._client.call("order_check", request)
        if raw is None:
            code, text = self._client.last_error()
            return False, code, f"order_check failed: {text}"
        code = int(raw.retcode)
        ok = code == CHECK_OK or code in DONE_CODES
        return ok, code, f"{code} {RETCODE_TEXT.get(code, str(getattr(raw, 'comment', '')))}"

    def _send(
        self,
        action: str,
        signal_id: str | None,
        request: dict[str, Any],
        *,
        fresh_price: Callable[[], float] | None = None,
    ) -> OrderResult:
        """order_send with retries for transient answers; logs every attempt's outcome."""
        result = OrderResult.failed("not sent")
        for attempt in range(1, self._attempts + 1):
            if fresh_price is not None:
                request["price"] = self.price(fresh_price())
            raw = self._client.call("order_send", request)
            if raw is None:
                code, text = self._client.last_error()
                result = OrderResult.failed(f"order_send failed: {code} {text}", code)
            else:
                result = self._result(raw, "no answer")
            self._log_order(action, signal_id, dict(request), result)
            if result.ok or result.retcode not in RETRY_CODES or attempt == self._attempts:
                break
            delay = self._backoff * 2 ** (attempt - 1)
            log.warning("order retried", action=action, retcode=result.retcode, delay_seconds=delay)
            self._sleep(delay)
        level = log.info if result.ok else log.error
        level("order", action=action, signal=signal_id, ok=result.ok, answer=result.message)
        return result

    def open(
        self,
        *,
        long: bool,
        volume: float,
        stop_loss: float,
        take_profit: float,
        comment: str,
        signal_id: str,
        limit_price: float | None = None,
    ) -> OrderResult:
        """A market order (``limit_price`` None) or a pending limit order, with SL and TP."""
        if not (stop_loss > 0 and take_profit > 0):
            raise ValueError("refusing to send an order without a stop-loss and a take-profit")
        pending = limit_price is not None
        if pending:
            kind = self._const("ORDER_TYPE_BUY_LIMIT" if long else "ORDER_TYPE_SELL_LIMIT")
            action = self._const("TRADE_ACTION_PENDING")
        else:
            kind = self._const("ORDER_TYPE_BUY" if long else "ORDER_TYPE_SELL")
            action = self._const("TRADE_ACTION_DEAL")

        def market_price() -> float:
            tick = self.tick()
            return tick.ask if long else tick.bid

        request: dict[str, Any] = {
            "action": action,
            "symbol": self._spec.name,
            "volume": volume,
            "type": kind,
            "price": self.price(limit_price if pending else market_price()),  # type: ignore[arg-type]
            "sl": self.price(stop_loss),
            "tp": self.price(take_profit),
            "deviation": self._deviation,
            "magic": self._magic,
            "comment": comment[:31],
            "type_time": self._const("ORDER_TIME_GTC"),
        }
        name = "limit" if pending else "market"
        problem = "no filling mode accepted"
        for filling in self._fillings(pending):
            request["type_filling"] = filling
            ok, code, text = self._check(request)
            if ok:
                return self._send(
                    name, signal_id, request, fresh_price=None if pending else market_price
                )
            problem = text
            if code != INVALID_FILL:
                break
        result = OrderResult.failed(f"order_check refused it: {problem}")
        self._log_order(f"{name}_check", signal_id, dict(request), result)
        log.error("order refused by order_check", action=name, signal=signal_id, answer=problem)
        return result

    def modify(
        self, position: BrokerPosition, *, stop_loss: float, signal_id: str | None
    ) -> OrderResult:
        """Move a position's stop-loss; its take-profit stays as it is."""
        if stop_loss <= 0 or position.tp <= 0:
            raise ValueError("a position always keeps a stop-loss and a take-profit")
        request = {
            "action": self._const("TRADE_ACTION_SLTP"),
            "symbol": self._spec.name,
            "position": position.ticket,
            "sl": self.price(stop_loss),
            "tp": self.price(position.tp),
            "magic": self._magic,
        }
        return self._send("modify", signal_id, request)

    def close(
        self, position: BrokerPosition, *, comment: str, signal_id: str | None
    ) -> OrderResult:
        """Close a whole position at market."""
        long = position.long

        def exit_price() -> float:
            tick = self.tick()
            return tick.bid if long else tick.ask

        request: dict[str, Any] = {
            "action": self._const("TRADE_ACTION_DEAL"),
            "symbol": self._spec.name,
            "volume": position.volume,
            "type": self._const("ORDER_TYPE_SELL" if long else "ORDER_TYPE_BUY"),
            "position": position.ticket,
            "price": self.price(exit_price()),
            "deviation": self._deviation,
            "magic": self._magic,
            "comment": comment[:31],
            "type_time": self._const("ORDER_TIME_GTC"),
        }
        result = OrderResult.failed("no filling mode accepted")
        for filling in self._fillings(pending=False):
            request["type_filling"] = filling
            result = self._send("close", signal_id, request, fresh_price=exit_price)
            if result.retcode != INVALID_FILL:
                break
        return result

    def cancel(self, order_ticket: int, *, signal_id: str | None) -> OrderResult:
        """Delete a pending order."""
        request = {"action": self._const("TRADE_ACTION_REMOVE"), "order": order_ticket}
        return self._send("cancel", signal_id, request)

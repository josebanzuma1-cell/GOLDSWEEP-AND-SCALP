"""A FakeMT5 with a small trade server: positions, pending orders, SL/TP hits, deals.

Good enough to drive the order gateway and the live executor through the paths
that matter: order_check, filling modes, requotes, pending fills, stops and
targets hit by the server, closes by the bot or by hand, and the deal history
read afterwards. Prices move only when a test calls :meth:`quote`.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from goldsweep.core.clock import Clock
from tests.fake_mt5 import FakeMT5

DEAL, PENDING, SLTP, REMOVE = 1, 5, 6, 8
BUY, SELL, BUY_LIMIT, SELL_LIMIT = 0, 1, 2, 3
REASON_CLIENT, REASON_EXPERT, REASON_SL, REASON_TP = 0, 3, 4, 5
DONE, PLACED = 10009, 10008


class FakeTradingMT5(FakeMT5):
    TRADE_ACTION_DEAL = DEAL
    TRADE_ACTION_PENDING = PENDING
    TRADE_ACTION_SLTP = SLTP
    TRADE_ACTION_REMOVE = REMOVE
    ORDER_TYPE_BUY = BUY
    ORDER_TYPE_SELL = SELL
    ORDER_TYPE_BUY_LIMIT = BUY_LIMIT
    ORDER_TYPE_SELL_LIMIT = SELL_LIMIT
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TIME_GTC = 0
    POSITION_TYPE_BUY = 0

    def __init__(self, clock: Clock, **kwargs: Any) -> None:
        super().__init__(clock, **kwargs)
        self.open_positions: dict[int, SimpleNamespace] = {}
        self.pending_orders: dict[int, SimpleNamespace] = {}
        self.deal_history: list[SimpleNamespace] = []
        self.next_ticket = 5000
        #: ORDER_FILLING_* values the server accepts.
        self.allowed_fillings = {0, 1, 2}
        #: Scripted answers: order_send / order_check return these codes first.
        self.send_codes: list[int] = []
        self.check_codes: list[int] = []
        self.sent: list[dict[str, Any]] = []
        self.contract = 100.0
        self.commission_per_lot = 0.0  # per side

    # -- helpers ------------------------------------------------------------------ #

    def _new_ticket(self) -> int:
        self.next_ticket += 1
        return self.next_ticket

    def _server_now(self) -> int:
        now = self.clock.now()
        return int(now.timestamp()) + self._offset_seconds(now)

    def _answer(self, code: int, **values: Any) -> SimpleNamespace:
        base = {"retcode": code, "order": 0, "deal": 0, "price": 0.0, "volume": 0.0}
        base.update(values)
        base["comment"] = "done" if code in (DONE, PLACED) else "refused"
        return SimpleNamespace(**base)

    def _problem(self, request: dict[str, Any]) -> int:
        """The retcode the server would give an opening request (0 = fine)."""
        action = request["action"]
        if action not in (DEAL, PENDING) or "position" in request:
            return 0
        if request.get("type_filling") not in self.allowed_fillings:
            return 10030
        sl, tp, price = request.get("sl", 0.0), request.get("tp", 0.0), request["price"]
        if not sl or not tp:
            return 10016
        buy = request["type"] in (BUY, BUY_LIMIT)
        if buy and not sl < price < tp:
            return 10016
        if not buy and not tp < price < sl:
            return 10016
        if request["volume"] < 0.01:
            return 10014
        if action == PENDING:
            if buy and price >= self.ask:
                return 10015
            if not buy and price <= self.bid:
                return 10015
        return 0

    def _deal(
        self, position: SimpleNamespace, entry: int, price: float, reason: int, profit: float
    ) -> SimpleNamespace:
        deal = SimpleNamespace(
            ticket=self._new_ticket(),
            order=position.identifier,
            position_id=position.ticket,
            entry=entry,
            volume=position.volume,
            price=price,
            profit=profit,
            commission=-self.commission_per_lot * position.volume,
            swap=0.0,
            fee=0.0,
            reason=reason,
            time=self._server_now(),
            magic=position.magic,
            symbol=position.symbol,
            comment=position.comment,
        )
        self.deal_history.append(deal)
        return deal

    def _open(self, request: dict[str, Any], price: float, ticket: int) -> SimpleNamespace:
        buy = request["type"] in (BUY, BUY_LIMIT)
        position = SimpleNamespace(
            ticket=ticket,
            identifier=ticket,
            symbol=request["symbol"],
            type=BUY if buy else SELL,
            volume=request["volume"],
            price_open=price,
            sl=request["sl"],
            tp=request["tp"],
            profit=0.0,
            magic=request["magic"],
            comment=request["comment"],
            time=self._server_now(),
        )
        self.open_positions[ticket] = position
        self._deal(position, 0, price, REASON_EXPERT, 0.0)
        return position

    def _close(self, ticket: int, price: float, reason: int) -> None:
        position = self.open_positions.pop(ticket)
        sign = 1.0 if position.type == BUY else -1.0
        profit = round((price - position.price_open) * sign * position.volume * self.contract, 2)
        self._deal(position, 1, price, reason, profit)

    # -- MetaTrader5 trade API ---------------------------------------------------------- #

    def order_calc_margin(self, kind: int, symbol: str, volume: float, price: float) -> float:
        self._record("order_calc_margin", kind, symbol, volume, price)
        return volume * self.contract * price / self.account.leverage

    def order_check(self, request: dict[str, Any]) -> SimpleNamespace:
        self._record("order_check", dict(request))
        if self.check_codes:
            return self._answer(self.check_codes.pop(0))
        code = self._problem(request)
        return SimpleNamespace(retcode=code, comment="Done" if code == 0 else "refused")

    def order_send(self, request: dict[str, Any]) -> SimpleNamespace:
        self._record("order_send", dict(request))
        self.sent.append(dict(request))
        if self.send_codes:
            code = self.send_codes.pop(0)
            if code not in (DONE, PLACED):
                return self._answer(code)
        action = request["action"]
        problem = self._problem(request)
        if problem:
            return self._answer(problem)
        if action == DEAL and "position" not in request:
            buy = request["type"] == BUY
            price = self.ask if buy else self.bid
            ticket = self._new_ticket()
            self._open(request, price, ticket)
            return self._answer(DONE, order=ticket, price=price, volume=request["volume"])
        if action == DEAL:
            ticket = request["position"]
            if ticket not in self.open_positions:
                return self._answer(10036)
            position = self.open_positions[ticket]
            price = self.bid if position.type == BUY else self.ask
            self._close(ticket, price, REASON_EXPERT)
            return self._answer(DONE, price=price, volume=position.volume)
        if action == PENDING:
            ticket = self._new_ticket()
            self.pending_orders[ticket] = SimpleNamespace(
                ticket=ticket,
                symbol=request["symbol"],
                type=request["type"],
                volume_initial=request["volume"],
                volume_current=request["volume"],
                price_open=request["price"],
                sl=request["sl"],
                tp=request["tp"],
                magic=request["magic"],
                comment=request["comment"],
            )
            return self._answer(PLACED, order=ticket, volume=request["volume"])
        if action == SLTP:
            position = self.open_positions.get(request["position"])
            if position is None:
                return self._answer(10036)
            buy = position.type == BUY
            sl = request["sl"]
            if (buy and sl >= self.bid) or (not buy and sl <= self.ask):
                return self._answer(10016)
            position.sl, position.tp = sl, request["tp"]
            return self._answer(DONE)
        if action == REMOVE:
            if self.pending_orders.pop(request["order"], None) is None:
                return self._answer(10013)
            return self._answer(DONE)
        return self._answer(10013)

    def positions_get(self, symbol: str | None = None, **_kw: Any) -> tuple[SimpleNamespace, ...]:
        self._record("positions_get", symbol=symbol)
        return tuple(
            p for p in self.open_positions.values() if symbol is None or p.symbol == symbol
        )

    def orders_get(self, symbol: str | None = None, **_kw: Any) -> tuple[SimpleNamespace, ...]:
        self._record("orders_get", symbol=symbol)
        return tuple(
            o for o in self.pending_orders.values() if symbol is None or o.symbol == symbol
        )

    def history_deals_get(self, *args: Any, position: int | None = None, **_kw: Any) -> Any:
        self._record("history_deals_get", position=position)
        return tuple(d for d in self.deal_history if d.position_id == position)

    # -- the market moves ---------------------------------------------------------------- #

    def quote(self, bid: float, spread: float = 0.21) -> None:
        """New prices: pending orders fill, then stops and targets are hit."""
        self.bid, self.ask = round(bid, 2), round(bid + spread, 2)
        for ticket, order in list(self.pending_orders.items()):
            buy = order.type == BUY_LIMIT
            if (buy and self.ask <= order.price_open) or (not buy and self.bid >= order.price_open):
                del self.pending_orders[ticket]
                request = {
                    "symbol": order.symbol,
                    "type": order.type,
                    "volume": order.volume_current,
                    "sl": order.sl,
                    "tp": order.tp,
                    "magic": order.magic,
                    "comment": order.comment,
                }
                self._open(request, order.price_open, ticket)
        for ticket, position in list(self.open_positions.items()):
            buy = position.type == BUY
            price = self.bid if buy else self.ask
            if buy and position.sl and self.bid <= position.sl:
                self._close(ticket, position.sl, REASON_SL)
            elif buy and position.tp and self.bid >= position.tp:
                self._close(ticket, position.tp, REASON_TP)
            elif not buy and position.sl and self.ask >= position.sl:
                self._close(ticket, position.sl, REASON_SL)
            elif not buy and position.tp and self.ask <= position.tp:
                self._close(ticket, position.tp, REASON_TP)
            else:
                sign = 1.0 if buy else -1.0
                position.profit = round(
                    (price - position.price_open) * sign * position.volume * self.contract, 2
                )

    def close_by_hand(self, ticket: int) -> None:
        """The owner closes a position in the terminal or the mobile app."""
        position = self.open_positions[ticket]
        price = self.bid if position.type == BUY else self.ask
        self._close(ticket, price, REASON_CLIENT)

    def advance(self, minutes: float) -> None:
        self.clock.advance(dt.timedelta(minutes=minutes))  # type: ignore[attr-defined]

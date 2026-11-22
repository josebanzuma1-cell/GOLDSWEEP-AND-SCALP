"""Limits across the main strategy and the scalper on one account (D91, D111).

Each strategy keeps its own risk manager. Before a scalp is sent to the same account
as the main strategy (the scalper on the demo account beside the live-demo main
strategy, or both on paper), this guard also checks:

* the realised result today and this week over **both** strategies' trades against
  the account-wide limits (``risk.daily_loss_limit_pct`` and ``weekly_loss_limit_pct``);
* the money at risk in open positions of both, plus the new one, against
  ``scalper.risk.max_open_risk_pct`` of equity;
* **no opposite positions**: a scalp never opens against an open or waiting trade of
  the other strategy (that would be a hedge). The main strategy comes first: when it
  takes the other side, the scalp is closed before the main order (``live.py``).

Paper scalps beside a live main strategy are notional and are not guarded here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from goldsweep.analysis.periods import trading_day_of
from goldsweep.analysis.structure import Direction
from goldsweep.core.clock import ensure_utc
from goldsweep.core.settings import DayBoundaryConfig
from goldsweep.risk.risk_manager import RiskDecision
from goldsweep.storage.db import Database

_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Exposure:
    """An open position or waiting order of either strategy."""

    direction: Direction
    risk_amount: float  # account currency lost at its stop


class AccountGuard:
    """Account-wide checks for a new scalp."""

    def __init__(
        self,
        db: Database,
        boundary: DayBoundaryConfig,
        *,
        tables: Sequence[str],
        daily_loss_pct: float,
        weekly_loss_pct: float,
        max_open_risk_pct: float,
    ) -> None:
        self._db = db
        self._boundary = boundary
        self._tables = tuple(tables)
        self._daily = daily_loss_pct
        self._weekly = weekly_loss_pct
        self._open = max_open_risk_pct

    def realised(self, now: dt.datetime) -> tuple[float, float]:
        """Net result of both strategies' trades closed this trading day and this week
        (trading days of 17:00 New York, weeks from Monday, as the risk manager counts)."""
        instant = ensure_utc(now)
        day = trading_day_of(instant, self._boundary)
        monday = day - dt.timedelta(days=day.weekday())
        since = int((instant - dt.timedelta(days=9)).timestamp())
        today = week = 0.0
        for table in self._tables:
            rows = self._db.query(
                f"SELECT exit_time, net_pnl FROM {table} WHERE exit_time >= ?",  # noqa: S608
                (since,),
            )
            for exit_time, pnl in rows:
                closed = trading_day_of(
                    dt.datetime.fromtimestamp(exit_time, dt.UTC), self._boundary
                )
                if closed == day:
                    today += float(pnl)
                if monday <= closed <= day:
                    week += float(pnl)
        return today, week

    def check(
        self,
        now: dt.datetime,
        *,
        equity: float,
        direction: Direction,
        new_risk: float,
        exposures: Sequence[Exposure],
    ) -> RiskDecision:
        """May a new scalp in ``direction`` risking ``new_risk`` open now?"""
        instant = ensure_utc(now)
        reasons: list[str] = []
        if any(e.direction is not direction for e in exposures):
            reasons.append("the other strategy holds the opposite side (no hedging)")
        if equity > 0:
            open_risk = sum(e.risk_amount for e in exposures) + new_risk
            ceiling = self._open / 100.0 * equity
            if open_risk > ceiling + _TOLERANCE:
                reasons.append(
                    f"open risk {open_risk:.2f} would exceed {self._open:g}% of equity "
                    f"({ceiling:.2f}) across both strategies"
                )
            today, week = self.realised(instant)
            if today <= -self._daily / 100.0 * equity:
                reasons.append(f"account daily loss limit of {self._daily:g}% reached")
            if week <= -self._weekly / 100.0 * equity:
                reasons.append(f"account weekly loss limit of {self._weekly:g}% reached")
        return RiskDecision(not reasons, tuple(reasons))

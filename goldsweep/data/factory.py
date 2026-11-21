"""Chooses the broker implementation for a run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from goldsweep.core.clock import Clock
from goldsweep.data.broker import BrokerClient
from goldsweep.data.mock_broker import MockBroker
from goldsweep.data.mt5_client import MT5Client, StateStore

if TYPE_CHECKING:
    from goldsweep.core.settings import Settings


def create_broker(
    settings: Settings,
    *,
    mock: bool = False,
    clock: Clock | None = None,
    state: StateStore | None = None,
    connect_attempts: int | None = None,
    connect_timeout_ms: int | None = None,
) -> BrokerClient:
    """Return the offline mock when ``mock`` is true, otherwise the MetaTrader 5 client.

    The mock is only ever chosen explicitly. A missing MT5 installation is an
    error to report, never a reason to substitute synthetic prices.

    ``connect_attempts`` and ``connect_timeout_ms`` limit how often and how long
    the MT5 client tries to connect (default: the client's own, patient, settings).
    """
    if mock:
        return MockBroker(clock=clock)
    return MT5Client.from_settings(
        settings,
        clock=clock,
        state=state,
        max_attempts=connect_attempts,
        init_timeout_ms=connect_timeout_ms,
    )

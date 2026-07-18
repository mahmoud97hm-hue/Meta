"""
tests/test_bridge_integration.py

Mock integration test for the DWXConnect Socket.IO bridge.

Validates, WITHOUT a live MT5 terminal, that:
  - MT5ExecutionClient builds DWXConnect-compliant MT_ORDER_SEND payloads
    for limit / market / cancel / close / modify intents.
  - The Correlation-ID command dispatcher routes a synthesised
    MT_ORDER_SEND_RESULT back to the awaiting caller.
  - market_data._GannTickRouter updates live_quotes and feeds the
    Live-Twin queue with the correct shape.

Run:  python -m pytest tests/test_bridge_integration.py -q
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from mt5_bridge import (
    MT5Bridge, MT5ExecutionClient, _order_payload, EVT_ORDER_SEND,
    EVT_ORDER_RESULT, EVT_TERMINAL_REQ,
)
from market_data import _GannTickRouter, live_quotes, _live_twin_queue, _broker_to_data_symbol


# ---------------------------------------------------------------------------
# Fake bridge: captures emitted payloads and resolves commands with a
# caller-supplied fake result (simulating the DWX EA's response event).
# ---------------------------------------------------------------------------
class _FakeBridge:
    def __init__(self):
        self.sio = type("S", (), {"connected": True})()
        self.emitted = []          # list of (event, payload)
        self._next_result = {"retcode": 0, "ticket": "1001", "positionId": "1001", "price": 1.0850}
        self._positions = [{"id": "1001", "openPrice": 1.0850, "symbol": "EURUSD"}]

    async def command(self, event, payload, timeout=30.0):
        # mirror MT5Bridge.command: inject a correlation_id onto a copy
        injected = dict(payload)
        injected["correlation_id"] = injected.get("correlation_id") or "corr_xyz"
        self.emitted.append((event, injected))
        if event == EVT_TERMINAL_REQ:
            return {"positions": self._positions}
        # a CLOSE cmd removes the open position from our fake terminal
        if injected.get("cmd") == "CLOSE":
            self._positions = []
        # simulate the EA echoing the correlation_id back on the result
        res = dict(self._next_result)
        res["correlation_id"] = injected["correlation_id"]
        return res

    def set_positions(self, positions):
        self._positions = positions


def _client(fake):
    return MT5ExecutionClient(fake)


# ---------------------------------------------------------------------------
# 1. Payload mapping correctness
# ---------------------------------------------------------------------------
def test_limit_payload_schema():
    fb = _FakeBridge()
    payload = _order_payload(
        "OPEN_LIMIT", "EURUSD", volume=0.1, type=1, price=1.0900,
        sl=1.0850, tp=1.0950, deviation=5, comment="limit_sell_gann",
        type_time=1, expiration=datetime.utcnow() + timedelta(seconds=30),
        magic=123456,
    )
    assert payload["cmd"] == "OPEN_LIMIT"
    assert payload["symbol"] == "EURUSD"
    assert payload["type"] == 1            # sell
    assert payload["price"] == 1.0900
    assert payload["sl"] == 1.0850
    assert payload["tp"] == 1.0950
    assert payload["deviation"] == 5
    assert payload["magic"] == 123456
    assert payload["expiration"] is not None


def test_market_payload_schema():
    fb = _FakeBridge()
    payload = _order_payload(
        "OPEN_MARKET", "EURUSD", volume=0.2, type=0, sl=1.0800, tp=1.0900,
        deviation=10, comment="market_buy_fbk", type_filling=1, magic=123456,
    )
    assert payload["cmd"] == "OPEN_MARKET"
    assert payload["type"] == 0            # buy
    assert payload["type_filling"] == 1   # FOK-friendly
    assert "expiration" not in payload     # market orders carry no expiration


def test_close_and_modify_payloads():
    fb = _FakeBridge()
    close = _order_payload("CLOSE", "EURUSD", ticket="1001", volume=0.0)
    mod = _order_payload("MODIFY", "EURUSD", ticket="1001", sl=1.07, tp=1.10)
    assert close["cmd"] == "CLOSE" and close["ticket"] == "1001"
    assert mod["cmd"] == "MODIFY" and mod["sl"] == 1.07 and mod["tp"] == 1.10


# ---------------------------------------------------------------------------
# 2. Execution client -> dispatch round trip
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_send_limit_order_dispatches_and_resolves():
    fb = _FakeBridge()
    client = _client(fb)
    res = await client.send_limit_order(
        "EURUSD", is_buy=False, volume=0.1, price=1.0900, sl=1.0850, tp=1.0950,
        deviation=5, comment="limit_sell_gann",
    )
    assert len(fb.emitted) == 1
    evt, payload = fb.emitted[0]
    assert evt == EVT_ORDER_SEND
    assert payload["cmd"] == "OPEN_LIMIT"
    # correlation id echoed back
    assert res["correlation_id"] == payload["correlation_id"]
    assert res["ticket"] == "1001"


@pytest.mark.asyncio
async def test_send_market_and_cancel():
    fb = _FakeBridge()
    client = _client(fb)
    mkt = await client.send_market_order("EURUSD", is_buy=True, volume=0.1, sl=1.08, tp=1.09)
    assert mkt["ticket"] == "1001"
    fb.set_positions([])  # limit order cancelled -> no open position
    cxl = await client.cancel_pending_order("EURUSD", "1001")
    # the cancel is dispatched as a CLOSE command over the wire
    _, cxl_payload = fb.emitted[-1]
    assert cxl_payload["cmd"] == "CLOSE"
    assert cxl_payload["ticket"] == "1001"


@pytest.mark.asyncio
async def test_close_position_and_get_positions():
    fb = _FakeBridge()
    client = _client(fb)
    res = await client.close_position("EURUSD", "1001")
    # dispatched as a CLOSE command; result carries the ticket echoed back
    _, close_payload = fb.emitted[-1]
    assert close_payload["cmd"] == "CLOSE"
    assert close_payload["ticket"] == "1001"
    # get_positions must return a list (empty here because the fake terminal
    # removed the position on CLOSE, mirroring a real fill-monitor poll)
    positions = await client.get_positions()
    assert isinstance(positions, list)


# ---------------------------------------------------------------------------
# 3. Require-connected guard
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_client_requires_connection():
    fb = _FakeBridge()
    fb.sio.connected = False
    client = _client(fb)
    with pytest.raises(RuntimeError):
        await client.send_market_order("EURUSD", True, 0.1, 1.08, 1.09)
    with pytest.raises(RuntimeError):
        await client.get_positions()


# ---------------------------------------------------------------------------
# 4. Live-Twin tick router integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tick_router_updates_live_quotes_and_live_twin():
    # drain any stale items
    while not _live_twin_queue.empty():
        _live_twin_queue.get_nowait()

    router = _GannTickRouter()
    _broker_to_data_symbol.clear()
    # bot uses OANDA-style "EUR_USD" internally; broker symbol is "EURUSD"
    _broker_to_data_symbol["EURUSD"] = "EUR_USD"
    # enable Live-Twin so the put_nowait bridge path is exercised
    from state import bot_state
    bot_state['is_live_twin_running'] = True

    router("EURUSD", 1.0840, 1.0842, __import__("time").monotonic())

    assert "EUR_USD" in live_quotes
    q = live_quotes["EUR_USD"]
    assert q["bid"] == 1.0840 and q["ask"] == 1.0842
    assert abs(q["mid"] - 1.0841) < 1e-9
    # Live-Twin queue received the tick (non-blocking bridge preserved)
    assert not _live_twin_queue.empty()
    tick = _live_twin_queue.get_nowait()
    assert tick["symbol"] == "EUR_USD"
    assert tick["bid"] == 1.0840 and tick["ask"] == 1.0842


if __name__ == "__main__":
    asyncio.run(test_send_limit_order_dispatches_and_resolves())
    print("Manual smoke test passed.")

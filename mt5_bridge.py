"""
mt5_bridge.py — Self-hosted MT5 Socket.IO bridge client.

Replaces the MetaApi cloud SDK with a direct, ultra-low-latency connection to
the FortesenseLabs/metatrader-terminal DWXConnect server (Alpine + Wine + MT5).

Responsibilities:
  - Resilient async python-socketio client with auto-reconnect, ping timeouts,
    backoff, and a stale-tick watchdog.
  - Inbound event routing: ticks (MT_QUOTES), bars (MT_BARINFO),
    trade transactions (MT_TRADE_TRANSACTION).
  - Outbound command dispatcher with correlation-id futures so callers
    (execution.py) can `await` an order result without blocking the loop.

This module is broker-protocol agnostic: the DWX event schema is mapped via the
DWX_* constants so the rest of the bot never sees raw socket.io payloads.
"""

import asyncio
import time
import uuid
from typing import Any, Callable, Awaitable, Optional

import socketio

from state import (
    bot_state, METAAPI_TOKEN, ACCOUNT_ID, OANDA_TOKEN, OANDA_BASE_URL,
    SYMBOL_INFO, CONN_RUNNING, CONN_READ_ONLY, CONN_HALTED,
    _state_lock, get_http, log_exception, c_log, _safe_task,
    set_connection_state,
)

# ---------------------------------------------------------------------------
# DWXConnect event schema (FortesenseLabs/metatrader-terminal)
# ---------------------------------------------------------------------------
# Inbound (server -> us)
EVT_QUOTES = "MT_QUOTES"                 # live tick push: list of quote dicts
EVT_BAR = "MT_BARINFO"                   # new bar closed
EVT_TRADE_TX = "MT_TRADE_TRANSACTION"    # trade/order state change
EVT_ORDER_RESULT = "MT_ORDER_SEND_RESULT"  # response to an order request
EVT_TERMINAL_STATE = "MT_TERMINAL_DATA"  # account/terminal snapshot

# Outbound (us -> server)
EVT_SUBSCRIBE = "SUBSCRIBE_SYMBOLS"      # request tick streaming for symbols
EVT_BAR_SUBSCRIBE = "SUBSCRIBE_BARS"
EVT_ORDER_SEND = "MT_ORDER_SEND"         # place/modify/close order
EVT_TERMINAL_REQ = "GET_TERMINAL_DATA"

# Server default port (FortesenseLabs container: socket.io on :3000)
DEFAULT_BRIDGE_URL = "http://localhost:3000"
DEFAULT_API_KEY = ""  # set in bot_state['bridge_api_key'] if the server enforces one

# Tuning
_PING_TIMEOUT = 20.0
_PING_INTERVAL = 10.0
_RECONNECT_DELAY = 2.0
_RECONNECT_DELAY_MAX = 30.0
_CMD_TIMEOUT = 30.0
_WS_WATCHDOG_STALE_SECONDS = 60.0


class MT5Bridge:
    """
    Singleton-style async Socket.IO client for the DWXConnect server.

    Public surface used by the rest of the bot:
        await bridge.connect()
        bridge.on_tick  -> callback(symbol, bid, ask, ts)
        await bridge.command(event, payload, timeout=_CMD_TIMEOUT) -> result dict
        bridge.subscribe([symbols])
    """

    def __init__(self, url: str = DEFAULT_BRIDGE_URL, api_key: str = DEFAULT_API_KEY):
        self.url = url
        self.api_key = api_key
        self.sio = socketio.AsyncClient(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=0,            # infinite; we also run our own watchdog
            reconnection_delay=_RECONNECT_DELAY,
            reconnection_delay_max=_RECONNECT_DELAY_MAX,
            ping_timeout=_PING_TIMEOUT,
            ping_interval=_PING_INTERVAL,
        )
        self._connected = asyncio.Event()
        self._ready = asyncio.Event()           # synchronized / subscribed
        self._corr_futures: dict[str, asyncio.Future] = {}
        self._tick_callbacks: list[Callable[[str, float, float, float], None]] = []
        self._bar_callbacks: list[Callable[[dict], None]] = []
        self._trade_callbacks: list[Callable[[dict], None]] = []
        self._last_any_tick_ts = time.monotonic()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._register_handlers()

    # -- registration -------------------------------------------------------
    def _register_handlers(self):
        self.sio.on("connect", self._h_connect)
        self.sio.on("disconnect", self._h_disconnect)
        self.sio.on("reconnect", self._h_reconnect)
        self.sio.on(EVT_QUOTES, self._h_quotes)
        self.sio.on(EVT_BAR, self._h_bar)
        self.sio.on(EVT_TRADE_TX, self._h_trade)
        self.sio.on(EVT_ORDER_RESULT, self._h_order_result)
        self.sio.on(EVT_TERMINAL_STATE, self._h_terminal_state)
        self.sio.on("error", self._h_error)

    # -- lifecycle ---------------------------------------------------------
    async def connect(self):
        if self.sio.connected:
            return
        c_log(f"[MT5Bridge] connecting to DWXConnect server @ {self.url}")
        await self.sio.connect(
            self.url,
            transports=["websocket"],
            auth={"api_key": self.api_key} if self.api_key else None,
            wait=False,
        )
        # wait for the connect event to flip _connected
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=30)
        except asyncio.TimeoutError:
            raise RuntimeError("[MT5Bridge] connect handshake timed out (30s)")
        # resume subscriptions for active symbols
        await self._resubscribe()
        self._ready.set()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = _safe_task(self._watchdog_loop(), "mt5_bridge_watchdog")
        await set_connection_state(CONN_RUNNING, "DWXConnect bridge connected and subscribed.")

    async def disconnect(self):
        try:
            await self.sio.disconnect()
        except Exception as e:
            log_exception("[MT5Bridge] disconnect", e)

    async def wait_ready(self):
        await self._ready.wait()

    # -- subscriptions ------------------------------------------------------
    def on_tick(self, cb: Callable[[str, float, float, float], None]):
        self._tick_callbacks.append(cb)

    def on_bar(self, cb: Callable[[dict], None]):
        self._bar_callbacks.append(cb)

    def on_trade(self, cb: Callable[[dict], None]):
        self._trade_callbacks.append(cb)

    async def subscribe(self, symbols: list[str]):
        """Request tick streaming for a list of broker symbols."""
        if not self.sio.connected:
            return
        try:
            await self.sio.emit(EVT_SUBSCRIBE, {"symbols": symbols})
            c_log(f"[MT5Bridge] subscribed to ticks: {symbols}")
        except Exception as e:
            log_exception("[MT5Bridge] subscribe", e)

    async def _resubscribe(self):
        active = [s for s, on in bot_state.get('active_symbols', {}).items() if on]
        if active:
            await self.subscribe([_resolve_broker_symbol_local(s) for s in active])

    # -- command dispatcher ------------------------------------------------
    async def command(self, event: str, payload: dict, timeout: float = _CMD_TIMEOUT) -> dict:
        """
        Fire-and-await a request/response command over Socket.IO.

        DWX uses a correlation id echoed back on the result event so we can
        resolve exactly the right future without blocking the event loop.
        """
        if not self.sio.connected:
            raise RuntimeError("[MT5Bridge] not connected; cannot send command")
        corr = uuid.uuid4().hex
        payload = dict(payload)
        payload["correlation_id"] = corr
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._corr_futures[corr] = fut
        try:
            await self.sio.emit(event, payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._corr_futures.pop(corr, None)
            raise RuntimeError(f"[MT5Bridge] command {event} timed out after {timeout}s")
        finally:
            self._corr_futures.pop(corr, None)

    # -- event handlers ----------------------------------------------------
    async def _h_connect(self):
        self._connected.set()
        c_log("[MT5Bridge] socket connected to DWXConnect.")

    async def _h_disconnect(self):
        self._connected.clear()
        self._ready.clear()
        c_log("[MT5Bridge] socket disconnected — auto-reconnect active.")

    async def _h_reconnect(self):
        c_log("[MT5Bridge] socket reconnected — re-subscribing.")
        await self._resubscribe()
        self._ready.set()

    async def _h_quotes(self, data):
        """DWX pushes ticks. Payload shape is a list of quote dicts."""
        self._last_any_tick_ts = time.monotonic()
        quotes = data if isinstance(data, list) else [data]
        for q in quotes:
            try:
                sym = q.get("symbol") or q.get("Symbol")
                bid = _as_float(q.get("bid") or q.get("Bid"))
                ask = _as_float(q.get("ask") or q.get("Ask"))
                if not sym or bid is None or ask is None:
                    continue
                for cb in self._tick_callbacks:
                    try:
                        cb(sym, bid, ask, time.monotonic())
                    except Exception as e:
                        log_exception("[MT5Bridge] tick callback", e)
            except Exception as e:
                log_exception("[MT5Bridge] _h_quotes row", e)

    async def _h_bar(self, data):
        for cb in self._bar_callbacks:
            try:
                cb(data)
            except Exception as e:
                log_exception("[MT5Bridge] bar callback", e)

    async def _h_trade(self, data):
        for cb in self._trade_callbacks:
            try:
                cb(data)
            except Exception as e:
                log_exception("[MT5Bridge] trade callback", e)

    async def _h_order_result(self, data):
        corr = (data or {}).get("correlation_id") if isinstance(data, dict) else None
        fut = self._corr_futures.get(corr) if corr else None
        if fut is not None and not fut.done():
            fut.set_result(data if isinstance(data, dict) else {"raw": data})
        else:
            # unsolicited / broadcast result — surface to trade callbacks too
            await self._h_trade(data)

    async def _h_terminal_state(self, data):
        # account/equity snapshot — future use for risk module
        pass

    async def _h_error(self, data):
        c_log(f"[MT5Bridge] server error event: {data}")

    # -- watchdog ----------------------------------------------------------
    async def _watchdog_loop(self):
        while True:
            await asyncio.sleep(15)
            stale = (time.monotonic() - self._last_any_tick_ts) > _WS_WATCHDOG_STALE_SECONDS
            if stale and self.sio.connected:
                c_log("[MT5Bridge] WATCHDOG: tick stream stale — forcing reconnect.")
                await set_connection_state(CONN_READ_ONLY, "MT5Bridge watchdog: stale ticks")
                try:
                    await self.sio.disconnect()
                except Exception:
                    pass
                # socketio reconnection will re-fire _h_reconnect -> _resubscribe
                self._last_any_tick_ts = time.monotonic()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _as_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and abs(f) != float('inf') else None
    except (TypeError, ValueError):
        return None


def _resolve_broker_symbol_local(symbol: str) -> str:
    configured = bot_state.get('symbol', '').strip()
    if not configured or '_' in configured:
        return symbol.replace('_', '')
    return configured


# module-level singleton used by market_data.py / execution.py
bridge: Optional[MT5Bridge] = None


# ---------------------------------------------------------------------------
# DWXConnect Order-Send payload builders
# ---------------------------------------------------------------------------
# DWXConnect EA expects MT_ORDER_SEND payloads shaped like an MQL5
# MqlTradeRequest. We map our bot's high-level intents onto that schema.
#   action:  ORDER_TYPE_BUY(0) / SELL(1) / BUY_LIMIT(2) / SELL_LIMIT(3)
#            BUY_STOP(4) / SELL_STOP(5) / CLOSE_BY_RELATED(...) / MODIFY(6)
#   We use a custom "cmd" discriminator so the EA routes correctly:
#     OPEN_MARKET, OPEN_LIMIT, MODIFY, CLOSE
# ---------------------------------------------------------------------------
_DWX_ACTION = {
    "OPEN_MARKET": 0,   # market buy/sell chosen via type field
    "OPEN_LIMIT": 2,    # limit buy(2)/sell(3) chosen via type field
    "MODIFY": 6,        # SL/TP modification of an open position
    "CLOSE": 7,         # close by ticket
}


def _order_payload(cmd: str, broker_symbol: str, **kw) -> dict:
    """Build a DWXConnect-compliant MT_ORDER_SEND payload."""
    payload = {
        "cmd": cmd,
        "symbol": broker_symbol,
        "volume": float(kw.get("volume", 0.0)),
        "action": _DWX_ACTION.get(cmd, 0),
        "type": int(kw.get("type", 0)),          # 0 buy / 1 sell (for market+limit)
        "price": float(kw.get("price", 0.0)),
        "sl": float(kw.get("sl", 0.0)),
        "tp": float(kw.get("tp", 0.0)),
        "deviation": int(kw.get("deviation", 0)),  # slippage points
        "magic": int(kw.get("magic", 123456)),
        "comment": str(kw.get("comment", "gann")),
        "type_filling": int(kw.get("type_filling", 1)),   # 1 = FOK-friendly default
        "type_time": int(kw.get("type_time", 0)),         # 0 = GTC
        "expiration": kw.get("expiration"),               # datetime/ISO for timed orders
        "ticket": str(kw.get("ticket", "")),              # required for MODIFY/CLOSE
    }
    # strip null expiration for market/modify to keep EA parser happy
    if payload["expiration"] is None:
        del payload["expiration"]
    return payload


class MT5ExecutionClient:
    """
    High-level async execution adapter over the Socket.IO bridge.

    Replaces the old MetaApi `_metaapi_conn` surface used by execution.py:
        send_limit_order / send_market_order  (open)
        cancel_pending_order                  (IOC cancel)
        close_position / close_positions      (close)
        modify_position_sl_tp                 (SL/TP)
        get_positions                         (fill-monitor + closure confirm)

    All calls are `await bridge.command(EVT_ORDER_SEND, payload)` and therefore
    never block the event loop. The Correlation-ID dispatcher resolves the
    matching MT_ORDER_SEND_RESULT future.
    """

    def __init__(self, bridge_ref: "MT5Bridge"):
        self._b = bridge_ref

    def _require(self):
        if self._b is None or not self._b.sio.connected:
            raise RuntimeError("MT5ExecutionClient: bridge not connected")

    async def send_limit_order(self, broker_symbol: str, is_buy: bool, volume: float,
                               price: float, sl: float, tp: float,
                               deviation: int = 0, comment: str = "limit_gann",
                               expiration=None, magic: int = 123456) -> dict:
        self._require()
        payload = _order_payload(
            "OPEN_LIMIT", broker_symbol,
            volume=volume, type=(0 if is_buy else 1), price=price,
            sl=sl, tp=tp, deviation=deviation, comment=comment,
            type_time=(1 if expiration is not None else 0),
            expiration=expiration, magic=magic,
        )
        return await self._b.command(EVT_ORDER_SEND, payload)

    async def send_market_order(self, broker_symbol: str, is_buy: bool, volume: float,
                                sl: float, tp: float, deviation: int = 0,
                                comment: str = "market_fbk", magic: int = 123456,
                                type_filling: int = 1) -> dict:
        self._require()
        payload = _order_payload(
            "OPEN_MARKET", broker_symbol,
            volume=volume, type=(0 if is_buy else 1), price=0.0,
            sl=sl, tp=tp, deviation=deviation, comment=comment,
            type_filling=type_filling, magic=magic,
        )
        return await self._b.command(EVT_ORDER_SEND, payload)

    async def cancel_pending_order(self, broker_symbol: str, ticket: str,
                                   magic: int = 123456) -> dict:
        self._require()
        # DWX cancel == a CLOSE cmd on a pending order ticket
        payload = _order_payload(
            "CLOSE", broker_symbol, ticket=ticket, volume=0.0,
            price=0.0, sl=0.0, tp=0.0, magic=magic,
        )
        return await self._b.command(EVT_ORDER_SEND, payload)

    async def modify_position_sl_tp(self, broker_symbol: str, ticket: str,
                                    sl: float, tp: float, magic: int = 123456) -> dict:
        self._require()
        payload = _order_payload(
            "MODIFY", broker_symbol, ticket=ticket, sl=sl, tp=tp, magic=magic,
        )
        return await self._b.command(EVT_ORDER_SEND, payload)

    async def close_position(self, broker_symbol: str, ticket: str,
                             volume: float = 0.0, deviation: int = 0,
                             magic: int = 123456) -> dict:
        self._require()
        payload = _order_payload(
            "CLOSE", broker_symbol, ticket=ticket, volume=volume,
            price=0.0, sl=0.0, tp=0.0, deviation=deviation, magic=magic,
        )
        return await self._b.command(EVT_ORDER_SEND, payload)

    async def get_positions(self) -> list:
        """
        Returns the current open positions/orders list from the DWX terminal
        snapshot. Used by the fill-monitor and closure-confirmation polls.
        """
        self._require()
        data = await self._b.command(EVT_TERMINAL_REQ, {}, timeout=_CMD_TIMEOUT)
        # DWX returns either a positions key or a full terminal snapshot
        if isinstance(data, dict):
            return data.get("positions") or data.get("open_positions") or data.get("orders") or []
        return []


def get_execution_client() -> MT5ExecutionClient:
    """Returns an MT5ExecutionClient bound to the live bridge singleton."""
    global bridge
    if bridge is None:
        raise RuntimeError("get_execution_client: bridge not initialised")
    return MT5ExecutionClient(bridge)

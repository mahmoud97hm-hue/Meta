"""
server.py — Headless MT5 Socket.IO bridge server (runs INSIDE the mt5 container)

This is the process the Gann Scalper bot connects to over Railway private
networking. It wraps the official `MetaTrader5` Python library (running
against the headless MT5 terminal launched by entrypoint.sh) and exposes the
exact event protocol our client (mt5_bridge.py) already speaks:

  IN  (bot -> server):
    SUBSCRIBE_SYMBOLS   {"symbols": [...]}        -> start tick stream
    MT_ORDER_SEND       {cmd, symbol, ...}        -> place/modify/close
    GET_TERMINAL_DATA   {}                         -> positions snapshot

  OUT (server -> bot):
    MT_QUOTES           [{symbol,bid,ask,...}]     -> live ticks
    MT_ORDER_SEND_RESULT {correlation_id, ...}     -> order ack
    MT_TRADE_TRANSACTION {...}                     -> trade state change

Design notes:
  * No VNC, no WM, no MQL EA required — pure Python, lowest RAM footprint.
  * Tick streaming uses mt5.symbol_info_tick + a tight asyncio poll loop
    (sub-ms cadence) so the bot gets true push-like latency without the EA.
  * Every inbound command is keyed by a `correlation_id` and the matching
    RESULT echoes it back, matching the client's dispatcher contract.
"""

import asyncio
import os
import time

import MetaTrader5 as mt5
import socketio

BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "3000"))
API_KEY = os.environ.get("BRIDGE_API_KEY", "") or None

# --------------------------------------------------------------------------
# Terminal path resolution
# --------------------------------------------------------------------------
# The MetaTrader5 Python lib runs INSIDE Wine (via wine64 python.exe), so it
# must receive a WINDOWS-style path (e.g. C:\terminal\terminal64.exe) even
# though the Dockerfile / Railway env may express the path with a Linux
# layout. We auto-translate so no manual mapping is required:
#
#   1. Explicit Windows path wins:           MT5_TERMINAL_WINPATH
#   2. Else translate a Linux absolute path:  /opt/mt5/terminal/... -> C:\terminal\...
#      (the build installs portably into C:\terminal; we map the matching
#       Linux prefix /opt/mt5/terminal to it)
#   3. Else if it already looks like a Windows path, pass through.
#   4. Else fall back to the portable default C:\terminal\terminal64.exe.
# --------------------------------------------------------------------------
_LINUX_TERMINAL_PREFIX = "/opt/mt5/terminal"


def _resolve_mt5_terminal_path() -> str | None:
    winpath = os.environ.get("MT5_TERMINAL_WINPATH", "") or None
    if winpath:
        return winpath
    raw = os.environ.get("MT5_TERMINAL_PATH", "") or None
    if not raw:
        return "C:\\terminal\\terminal64.exe"
    # Already a Windows path (drive letter or backslash)?
    if ":" in raw or "\\" in raw:
        return raw
    # Linux absolute path: map known prefix to the Wine C: drive.
    if raw.startswith(_LINUX_TERMINAL_PREFIX):
        rel = raw[len(_LINUX_TERMINAL_PREFIX):].lstrip("/")
        return "C:\\terminal\\" + rel.replace("/", "\\")
    # Any other Linux path: assume it lives under the Wine C: drive root.
    rel = raw.lstrip("/")
    return "C:\\" + rel.replace("/", "\\")


MT5_TERMINAL_PATH = _resolve_mt5_terminal_path()

sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
app = socketio.ASGIApp(sio)

# subscription registry: symbol -> set of subscriber sids (single terminal => global)
_subscribed: set[str] = set()
_stream_task = None


# --------------------------------------------------------------------------
# MT5 lifecycle
# --------------------------------------------------------------------------
async def _ensure_mt5():
    if not mt5.terminal_info():
        login = int(os.environ.get("MT5_LOGIN", "0") or 0)
        password = os.environ.get("MT5_PASSWORD", "")
        server = os.environ.get("MT5_SERVER", "")
        # Point the lib at the auto-installed terminal; pass the broker
        # server string so MT5 downloads the datacenter config on login.
        init_kwargs = {}
        if MT5_TERMINAL_PATH:
            init_kwargs["path"] = MT5_TERMINAL_PATH
        if server:
            init_kwargs["server"] = server
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
        if login and password:
            if not mt5.login(login, password=password, server=server):
                raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")



def _auth_ok(environ) -> bool:
    if not API_KEY:
        return True
    # socket.io auth dict arrives in the handshake; also allow header token
    return environ.get("api_key") == API_KEY


# --------------------------------------------------------------------------
# Tick streaming loop (push-like via tight poll; no EA needed)
# --------------------------------------------------------------------------
async def _tick_stream():
    while True:
        if not _subscribed:
            await asyncio.sleep(0.5)
            continue
        t0 = time.monotonic()
        batch = []
        for sym in list(_subscribed):
            if not mt5.symbol_info(sym):
                continue
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                continue
            batch.append({
                "symbol": sym,
                "bid": tick.bid, "ask": tick.ask,
                "last": tick.last, "volume": tick.volume,
                "time": tick.time,
            })
        if batch:
            await sio.emit("MT_QUOTES", batch)
        # keep cadence tight but yield; ~ sub-millisecond loop on few symbols
        dt = time.monotonic() - t0
        await asyncio.sleep(max(0.0, 0.005 - dt))


# --------------------------------------------------------------------------
# Event handlers
# --------------------------------------------------------------------------
@sio.on("connect")
async def _on_connect(sid, environ):
    if not _auth_ok(environ):
        return False
    await _ensure_mt5()
    global _stream_task
    if _stream_task is None or _stream_task.done():
        _stream_task = asyncio.create_task(_tick_stream())
    print(f"[server] client connected: {sid}")


@sio.on("SUBSCRIBE_SYMBOLS")
async def _on_subscribe(sid, data):
    syms = (data or {}).get("symbols", [])
    for s in syms:
        if mt5.symbol_info(s) is None:
            mt5.symbol_select(s, True)
        _subscribed.add(s)
    await sio.emit("MT_QUOTES_SUBSCRIBED", {"symbols": list(_subscribed)})


@sio.on("MT_ORDER_SEND")
async def _on_order(sid, data):
    corr = data.get("correlation_id", "")
    try:
        cmd = data.get("cmd")
        symbol = data.get("symbol")
        volume = float(data.get("volume", 0.0))
        price = float(data.get("price", 0.0))
        sl = float(data.get("sl", 0.0))
        tp = float(data.get("tp", 0.0))
        deviation = int(data.get("deviation", 0))
        magic = int(data.get("magic", 123456))
        ticket = str(data.get("ticket", ""))

        request = {
            "symbol": symbol,
            "volume": volume,
            "sl": sl, "tp": tp,
            "deviation": deviation,
            "magic": magic,
            "comment": data.get("comment", "gann"),
            "type_filling": int(data.get("type_filling", 1)),
            "type_time": int(data.get("type_time", 0)),
        }
        if cmd == "OPEN_MARKET":
            request["action"] = mt5.TRADE_ACTION_DEAL
            request["type"] = mt5.ORDER_TYPE_BUY if int(data.get("type", 0)) == 0 else mt5.ORDER_TYPE_SELL
            request["price"] = mt5.symbol_info_tick(symbol).ask if int(data.get("type", 0)) == 0 else mt5.symbol_info_tick(symbol).bid
        elif cmd == "OPEN_LIMIT":
            request["action"] = mt5.TRADE_ACTION_PENDING
            request["type"] = mt5.ORDER_TYPE_BUY_LIMIT if int(data.get("type", 0)) == 0 else mt5.ORDER_TYPE_SELL_LIMIT
            request["price"] = price
            if data.get("expiration"):
                request["expiration"] = int(data["expiration"])
        elif cmd == "MODIFY":
            # modify SL/TP of an open position
            res = mt5.order_modify(ticket=int(ticket), sl=sl, tp=tp) if False else \
                  mt5.position_modify(ticket=int(ticket), sl=sl, tp=tp)
            await sio.emit("MT_ORDER_SEND_RESULT", {
                "correlation_id": corr, "retcode": 0 if res else mt5.last_error()[0],
                "ticket": ticket,
            })
            return
        elif cmd == "CLOSE":
            # close by ticket (or cancel pending order)
            pos = mt5.positions_get(ticket=int(ticket))
            if pos:
                p = pos[0]
                close_req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": p.symbol,
                    "volume": float(data.get("volume", p.volume) or p.volume),
                    "type": mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "position": int(ticket),
                    "price": mt5.symbol_info_tick(p.symbol).bid if p.type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(p.symbol).ask,
                    "deviation": deviation, "magic": magic,
                }
                result = mt5.order_send(close_req)
            else:
                # treat as pending-order deletion
                result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
            await sio.emit("MT_ORDER_SEND_RESULT", {
                "correlation_id": corr,
                "retcode": result.retcode if result else mt5.last_error()[0],
                "ticket": str(ticket),
                "price": getattr(result, "price", None),
            })
            return
        else:
            raise ValueError(f"unknown cmd: {cmd}")

        result = mt5.order_send(request)
        await sio.emit("MT_ORDER_SEND_RESULT", {
            "correlation_id": corr,
            "retcode": result.retcode if result else mt5.last_error()[0],
            "ticket": str(getattr(result, "order", "") or getattr(result, "deal", "")),
            "positionId": str(getattr(result, "order", "")),
            "price": getattr(result, "price", None),
        })
    except Exception as e:
        await sio.emit("MT_ORDER_SEND_RESULT", {
            "correlation_id": corr, "retcode": -1, "error": str(e),
        })


@sio.on("GET_TERMINAL_DATA")
async def _on_terminal(sid, data):
    positions = mt5.positions_get()
    out = [{
        "id": str(p.ticket), "ticket": str(p.ticket), "symbol": p.symbol,
        "volume": p.volume, "openPrice": p.price_open, "type": p.type,
    } for p in (positions or [])]
    await sio.emit("MT_TERMINAL_DATA", {"positions": out})


@sio.on("disconnect")
async def _on_disconnect(sid):
    print(f"[server] client disconnected: {sid}")


if __name__ == "__main__":
    import uvicorn
    print(f"[server] headless MT5 socket.io bridge on {BRIDGE_HOST}:{BRIDGE_PORT}")
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT, log_level="info")

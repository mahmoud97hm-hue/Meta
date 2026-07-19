"""
backend/api/main.py — Self-hosted MT5 bridge server (FastAPI + Socket.IO).

Runs as the bridge process inside the mt5-bridge container. It drives the
headless MetaTrader5 terminal (launched separately via Wine by the entrypoint)
through the official `MetaTrader5` Python library and exposes:

  * A Socket.IO server speaking the exact protocol the Gann Scalper bot's
    mt5_bridge.py client already expects:
        IN  (bot -> server): SUBSCRIBE_SYMBOLS, MT_ORDER_SEND, GET_TERMINAL_DATA
        OUT (server -> bot): MT_QUOTES, MT_ORDER_SEND_RESULT, MT_TRADE_TRANSACTION
  * A FastAPI HTTP layer for health / config / account + terminal management.

State (subscriptions, last quotes, connection status) is mirrored into Redis
so multiple workers / restarts stay consistent.

Runtime note: because the MetaTrader5 lib only works under Windows/Wine, this
process is launched with `wine64 C:\Python310\python.exe` by scripts/entrypoint.
Redis and the MT5 terminal run as native Linux / Wine processes respectively.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import MetaTrader5 as mt5
import socketio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

try:
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover - redis optional at import time
    aioredis = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "3000"))
API_KEY = os.environ.get("BRIDGE_API_KEY", "") or None
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH", "")
MT5_LOGIN = os.environ.get("MT5_LOGIN", "")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("MT5_SERVER", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

CONFIG_DIR = Path(os.environ.get("BRIDGE_CONFIG_DIR", "/opt/mt5bridge/backend/config"))
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
TERMINALS_FILE = CONFIG_DIR / "terminals.json"

# ---------------------------------------------------------------------------
# Socket.IO + FastAPI wiring
# ---------------------------------------------------------------------------
sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
app = FastAPI(title="MT5 Bridge", version="1.0.0")
http_app = socketio.ASGIApp(sio, app)

_redis = None


async def _get_redis():
    global _redis
    if aioredis is None:
        return None
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# subscription registry: symbol -> present (single terminal => global set)
_subscribed: set[str] = set()
_stream_task = None
_mt5_ready = False


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def _load_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _auth_ok(environ) -> bool:
    if not API_KEY:
        return True
    return (environ or {}).get("api_key") == API_KEY


# ---------------------------------------------------------------------------
# MT5 lifecycle
# ---------------------------------------------------------------------------
async def _ensure_mt5():
    global _mt5_ready
    if _mt5_ready and mt5.terminal_info():
        return
    init_kwargs = {}
    if MT5_TERMINAL_PATH:
        init_kwargs["path"] = MT5_TERMINAL_PATH
    if MT5_SERVER:
        init_kwargs["server"] = MT5_SERVER
    if not mt5.initialize(**init_kwargs):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    if MT5_LOGIN and MT5_PASSWORD:
        if not mt5.login(int(MT5_LOGIN), password=MT5_PASSWORD, server=MT5_SERVER):
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
    _mt5_ready = True


# ---------------------------------------------------------------------------
# Tick streaming loop (push-like via tight poll; no EA needed)
# ---------------------------------------------------------------------------
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
            try:
                r = await _get_redis()
                if r:
                    await r.set("mt5:last_quotes", json.dumps(batch))
            except Exception:
                pass
        dt = time.monotonic() - t0
        await asyncio.sleep(max(0.0, 0.005 - dt))


# ---------------------------------------------------------------------------
# Socket.IO event handlers
# ---------------------------------------------------------------------------
@sio.on("connect")
async def _on_connect(sid, environ):
    if not _auth_ok(environ):
        return False
    await _ensure_mt5()
    global _stream_task
    if _stream_task is None or _stream_task.done():
        _stream_task = asyncio.create_task(_tick_stream())
    print(f"[bridge] client connected: {sid}")


@sio.on("SUBSCRIBE_SYMBOLS")
async def _on_subscribe(sid, data):
    syms = (data or {}).get("symbols", [])
    for s in syms:
        if mt5.symbol_info(s) is None:
            mt5.symbol_select(s, True)
        _subscribed.add(s)
    await sio.emit("MT_QUOTES_SUBSCRIBED", {"symbols": list(_subscribed)})
    try:
        r = await _get_redis()
        if r:
            await r.sadd("mt5:subscribed", *syms)
    except Exception:
        pass


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
            side = int(data.get("type", 0))
            request["type"] = mt5.ORDER_TYPE_BUY if side == 0 else mt5.ORDER_TYPE_SELL
            tick = mt5.symbol_info_tick(symbol)
            request["price"] = tick.ask if side == 0 else tick.bid
        elif cmd == "OPEN_LIMIT":
            request["action"] = mt5.TRADE_ACTION_PENDING
            side = int(data.get("type", 0))
            request["type"] = mt5.ORDER_TYPE_BUY_LIMIT if side == 0 else mt5.ORDER_TYPE_SELL_LIMIT
            request["price"] = price
            if data.get("expiration"):
                request["expiration"] = int(data["expiration"])
        elif cmd == "MODIFY":
            res = mt5.position_modify(ticket=int(ticket), sl=sl, tp=tp)
            await sio.emit("MT_ORDER_SEND_RESULT", {
                "correlation_id": corr,
                "retcode": getattr(res, "retcode", -1),
                "description": mt5.last_error(),
            })
            return
        elif cmd == "CLOSE":
            res = mt5.position_close(ticket=int(ticket), deviation=deviation)
            await sio.emit("MT_ORDER_SEND_RESULT", {
                "correlation_id": corr,
                "retcode": getattr(res, "retcode", -1),
                "description": mt5.last_error(),
            })
            return

        res = mt5.order_send(request)
        result = {
            "correlation_id": corr,
            "retcode": getattr(res, "retcode", -1),
            "order": getattr(res, "order", 0),
            "deal": getattr(res, "deal", 0),
            "volume": getattr(res, "volume", 0.0),
            "price": getattr(res, "price", 0.0),
            "comment": getattr(res, "comment", ""),
            "description": mt5.last_error(),
        }
        await sio.emit("MT_ORDER_SEND_RESULT", result)
    except Exception as e:
        await sio.emit("MT_ORDER_SEND_RESULT", {
            "correlation_id": corr, "retcode": -1, "description": str(e),
        })


@sio.on("GET_TERMINAL_DATA")
async def _on_terminal_data(sid, data):
    try:
        info = mt5.terminal_info()
        positions = mt5.positions_get()
        result = {
            "terminal": info._asdict() if info else {},
            "positions": [p._asdict() for p in (positions or [])],
        }
        await sio.emit("MT_TERMINAL_DATA", result)
    except Exception as e:
        await sio.emit("MT_TERMINAL_DATA", {"error": str(e)})


# ---------------------------------------------------------------------------
# FastAPI HTTP endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mt5_connected": bool(mt5.terminal_info()),
        "subscribed": list(_subscribed),
    }


@app.get("/config/accounts")
async def get_accounts():
    return _load_json(ACCOUNTS_FILE)


@app.get("/config/terminals")
async def get_terminals():
    return _load_json(TERMINALS_FILE)


@app.post("/config/accounts")
async def set_accounts(payload: dict):
    ACCOUNTS_FILE.write_text(json.dumps(payload, indent=2))
    return {"status": "saved"}


@app.post("/config/terminals")
async def set_terminals(payload: dict):
    TERMINALS_FILE.write_text(json.dumps(payload, indent=2))
    return {"status": "saved"}


@app.get("/quotes")
async def get_quotes():
    r = await _get_redis()
    if not r:
        raise HTTPException(status_code=503, detail="redis unavailable")
    raw = await r.get("mt5:last_quotes")
    return JSONResponse(content=json.loads(raw) if raw else [])


# Use the socket.io ASGI app as the WSGI/ASGI entry so both protocols share a port.
def get_asgi_app():
    return http_app

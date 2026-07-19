"""
<<<<<<< HEAD
market_data.py — OANDA REST data fetching, mt5-bridge tick polling,
live-quote cache, and connection management.

Owns:
  - OANDA candle fetcher (fetch_candles, fetch_master_price)
  - mt5-bridge client singleton + tick polling loop
  - live_quotes cache, _gann_cache, tick semaphore
  - Bridge watchdog (_force_full_reconnect)
  - Broker symbol resolution
=======
market_data.py — Self-hosted MT5 Socket.IO bridge (DWXConnect), OANDA REST
candle fetcher, live-quote cache, and connection management.

Owns:
  - OANDA candle fetcher (fetch_candles, fetch_master_price)  [unchanged]
  - MT5Bridge lifecycle (DWXConnect socket.io connection)
  - live_quotes cache, _gann_cache, tick semaphore
  - Socket.IO tick router -> _GannTickRouter (zero-latency, no polling)
  - Live-Twin bridge (_live_twin_queue.put_nowait)
  - Stale-tick watchdog (_force_full_reconnect)
  - Broker symbol resolution

This module is a drop-in replacement for the old MetaApi-based market_data.py.
All public names used elsewhere in the bot are preserved.
>>>>>>> origin/main
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone, time as dtime

import aiohttp
import numpy as np
import pandas as pd

<<<<<<< HEAD
from mt5_bridge_client import (
    get_bridge_client, configure_bridge_client, BridgeHTTPError, BridgeConnectionError,
)
from state import (
    bot_state, MT5_BRIDGE_URL, MT5_ACCOUNT_ID, OANDA_TOKEN, OANDA_BASE_URL,
=======
from state import (
    bot_state, METAAPI_TOKEN, ACCOUNT_ID, OANDA_TOKEN, OANDA_BASE_URL,
>>>>>>> origin/main
    SYMBOL_INFO, CONN_RUNNING, CONN_READ_ONLY, CONN_HALTED,
    _state_lock, get_http, log_exception, c_log, _safe_task,
    set_connection_state,
)
<<<<<<< HEAD

# ---------------------------------------------------------------------------
# OANDA FETCHER
=======
from mt5_bridge import MT5Bridge, bridge as _shared_bridge

# ---------------------------------------------------------------------------
# OANDA FETCHER  (unchanged — kept for historical candle backfills)
>>>>>>> origin/main
# ---------------------------------------------------------------------------
_OANDA_GRAN = {'1m':'M1','2m':'M2','3m':'M3','4m':'M4','5m':'M5','6m':'M6',
               '10m':'M10','15m':'M15','20m':'M20','30m':'M30','1h':'H1','2h':'H2'}
_oanda_sem: asyncio.Semaphore | None = None


def _get_oanda_sem() -> asyncio.Semaphore:
    global _oanda_sem
    if _oanda_sem is None:
        _oanda_sem = asyncio.Semaphore(3)
    return _oanda_sem


from state import _safe_float


def _validated_candle(c: dict, symbol: str, granularity_str: str) -> dict | None:
    try:
        mid = c.get('mid')
        if not isinstance(mid, dict):
            raise ValueError(f"missing/invalid 'mid' field: {mid!r}")
        raw_time = c.get('time')
        if not raw_time:
            raise ValueError("missing 'time' field")
        o = float(mid['o']); h = float(mid['h']); l = float(mid['l']); c_ = float(mid['c'])
        vol = float(c.get('volume', 1.0) or 1.0)
        for v in (o, h, l, c_, vol):
            if v != v or v in (float('inf'), float('-inf')):
                raise ValueError(f"non-finite value in candle: {v!r}")
        return {
            'time': pd.Timestamp(raw_time).tz_convert('UTC'),
            'open': o, 'high': h, 'low': l, 'close': c_, 'volume': vol,
        }
    except (TypeError, ValueError, KeyError) as e:
        log_exception(f"_validated_candle [{symbol} {granularity_str}] -- skipping malformed candle", e)
        return None


async def fetch_candles(symbol: str, granularity_str: str, count: int = 5000,
                        end_time: datetime = None) -> list:
    gran_str = _OANDA_GRAN.get(granularity_str, 'M1')
    fetch_count = min(count, 120000)
    collected = []; remaining = fetch_count
    headers = {'Authorization': f'Bearer {OANDA_TOKEN}', 'Content-Type': 'application/json'}
    url = f'{OANDA_BASE_URL}/instruments/{symbol}/candles'
    current_end = end_time if end_time else datetime.now(timezone.utc)

    while remaining > 0:
        chunk = min(remaining, 5000)
        params = {'granularity': gran_str, 'count': chunk,
<<<<<<< HEAD
                   'to': current_end.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'), 'price': 'M'}
=======
                  'to': current_end.strftime('%Y-%m-%dT%H:%M:%S.000000000Z'), 'price': 'M'}
>>>>>>> origin/main
        candles = []
        async with _get_oanda_sem():
            for attempt in range(6):
                try:
                    async with get_http().get(url, headers=headers, params=params,
<<<<<<< HEAD
                                               timeout=aiohttp.ClientTimeout(total=20)) as resp:
=======
                                              timeout=aiohttp.ClientTimeout(total=20)) as resp:
>>>>>>> origin/main
                        if resp.status != 200:
                            if attempt == 5:
                                c_log(f"fetch_candles [{symbol} {granularity_str}]: giving up after 6 attempts "
                                      f"(last status {resp.status}) -- collected {len(collected)}/{fetch_count} candles so far.")
                                break
                            await asyncio.sleep(min(2 ** attempt, 30))
                            continue
                        data = await resp.json(); candles = data.get('candles', []); break
                except Exception as e:
                    log_exception(f"fetch_candles [{symbol} {granularity_str}] attempt {attempt+1}/6", e)
                    await asyncio.sleep(min(2 ** attempt, 30))

        if not candles: break
        complete = [c for c in candles if c.get('complete', True)]
        if not complete: break

        formatted = []
        for c in complete:
            vc = _validated_candle(c, symbol, granularity_str)
            if vc is not None:
                formatted.append(vc)

        if not formatted:
            c_log(f"fetch_candles [{symbol} {granularity_str}]: entire chunk failed validation, aborting fetch.")
            break

        collected = formatted + collected; remaining -= len(complete)
        earliest = pd.Timestamp(complete[0]['time']).tz_convert('UTC')
        current_end = earliest.to_pydatetime() - timedelta(seconds=1)
        if len(complete) < chunk: break
        await asyncio.sleep(0.2)
    return collected


async def fetch_master_price(symbol: str) -> float | None:
    mc = await fetch_candles(symbol, '1m', count=2)
    if not mc:
        c_log(f"fetch_master_price [{symbol}]: no 1m data from OANDA this cycle.")
        return None
    return float(mc[-1]['close'])


# ---------------------------------------------------------------------------
<<<<<<< HEAD
# LIVE QUOTES & TICK POLLING
=======
# LIVE QUOTES & SOCKET.IO TICK ROUTER
>>>>>>> origin/main
# ---------------------------------------------------------------------------
live_quotes: dict[str, dict] = {}
_broker_to_data_symbol: dict[str, str] = {}
_tick_semaphore = asyncio.Semaphore(5)
_gann_cache: dict[str, dict] = {}
_QUOTE_STALE_SECONDS = 5.0
_last_any_tick_ts = time.monotonic()
_WS_WATCHDOG_STALE_SECONDS = 60.0

<<<<<<< HEAD
# Live-Twin tick bridge
_live_twin_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

# Tick poller state
_tick_poller_task: asyncio.Task | None = None
_TICK_POLL_INTERVAL = 0.15  # 150ms per symbol

# Bridge connection state
_bridge_initialized = False
_bridge_reconnect_task: asyncio.Task | None = None
=======
# ── Live-Twin tick bridge (preserved) ──
_live_twin_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

# shared bridge singleton (imported from mt5_bridge)
bridge: MT5Bridge | None = None

# ── Strict Singleton: subscription set ──
_active_subscriptions: set[str] = set()


class _GannTickRouter:
    """
    Receives ticks from MT5Bridge.on_tick and routes them with zero latency:
      1. updates live_quotes cache
      2. fires the Gann level-touch detector
      3. feeds the Live-Twin engine via put_nowait (non-blocking)
    Registered once on the bridge. No polling, no threads.
    """

    def __init__(self):
        self._last_any_tick_ts = time.monotonic()

    def __call__(self, broker_sym: str, bid: float, ask: float, ts: float):
        global _last_any_tick_ts
        _last_any_tick_ts = ts
        data_sym = _broker_to_data_symbol.get(broker_sym)
        if not data_sym:
            # auto-register unknown symbols so we never drop a tick
            data_sym = broker_sym.replace('_', '')
            _broker_to_data_symbol[broker_sym] = data_sym
        mid = (bid + ask) / 2.0
        live_quotes[data_sym] = {'bid': bid, 'ask': ask, 'mid': mid, 'ts': ts}

        # tick-driven Gann level detection (fire-and-forget, non-blocking)
        from execution import _gann_tick_fire_check
        _safe_task(_gann_tick_fire_check(data_sym, mid, 0.0), 'tick_fire_check')

        # Live-Twin paper-trading bridge (write-discard, never blocks)
        if bot_state.get('is_live_twin_running', False):
            try:
                _live_twin_queue.put_nowait({
                    'symbol': data_sym, 'bid': bid, 'ask': ask, 'mid': mid, 'ts': ts
                })
            except asyncio.QueueFull:
                pass
>>>>>>> origin/main


def _lq_is_stale(symbol: str) -> bool:
    q = live_quotes.get(symbol)
    return q is None or (time.monotonic() - q['ts']) > _QUOTE_STALE_SECONDS


async def _lq_price_with_fallback(symbol: str) -> tuple[float | None, str, float | None]:
    q = live_quotes.get(symbol)
    if q is not None and (time.monotonic() - q['ts']) <= _QUOTE_STALE_SECONDS:
        return q['mid'], 'ws', round((time.monotonic() - q['ts']) * 1000)
    return None, 'ws_stale', None


def _resolve_broker_symbol(symbol: str) -> str:
    configured = bot_state.get('symbol', '').strip()
    if not configured or '_' in configured:
        return symbol.replace('_', '')
    return configured


async def _lq_subscribe_symbol(symbol: str) -> None:
<<<<<<< HEAD
    """No-op for bridge — all symbols are polled automatically."""
    broker_sym = _resolve_broker_symbol(symbol)
    _broker_to_data_symbol[broker_sym] = symbol


# ── Tick Poller ──
async def _tick_poller_loop() -> None:
    global _last_any_tick_ts
    c_log('Bridge tick poller started.')
    while True:
        try:
            active_symbols = [s for s, on in bot_state['active_symbols'].items() if on]
            if not active_symbols or not _bridge_initialized:
                await asyncio.sleep(0.5)
                continue

            client = get_bridge_client()
            for symbol in active_symbols:
                broker_sym = _resolve_broker_symbol(symbol)
                try:
                    tick = await client.get_tick(broker_sym)
                    if 'error' in tick:
                        continue
                    bid = tick.get('bid')
                    ask = tick.get('ask')
                    if bid is None or ask is None:
                        continue
                    _last_any_tick_ts = time.monotonic()
                    mid = (bid + ask) / 2
                    live_quotes[symbol] = {'bid': bid, 'ask': ask, 'mid': mid, 'ts': time.monotonic()}
                    from execution import _gann_tick_fire_check
                    _safe_task(_gann_tick_fire_check(symbol, mid, 0.0), 'tick_fire_check')
                    if bot_state.get('is_live_twin_running', False):
                        try:
                            _live_twin_queue.put_nowait({
                                'symbol': symbol, 'bid': bid, 'ask': ask, 'mid': mid,
                                'ts': time.monotonic()
                            })
                        except asyncio.QueueFull:
                            pass
                except (BridgeHTTPError, BridgeConnectionError) as e:
                    log_exception(f'_tick_poller_loop [{symbol}]', e)
                await asyncio.sleep(_TICK_POLL_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_exception('_tick_poller_loop', e)
            await asyncio.sleep(1)


# ── Bridge Reconnect Watchdog ──
async def _bridge_watchdog() -> None:
    while True:
        try:
            await asyncio.sleep(15)
            if not _bridge_initialized:
                continue
            active = [s for s, on in bot_state['active_symbols'].items() if on]
            market_open = _is_market_hours(check_bridge=False)
            if active and market_open and (time.monotonic() - _last_any_tick_ts) > _WS_WATCHDOG_STALE_SECONDS:
                await _force_full_reconnect(
                    f"No tick received for {time.monotonic() - _last_any_tick_ts:.0f}s "
                    f"(limit: {_WS_WATCHDOG_STALE_SECONDS:.0f}s)"
                )
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_exception('_bridge_watchdog', e)


async def _force_full_reconnect(reason: str) -> None:
    global _bridge_initialized, _last_any_tick_ts
    c_log(f"BRIDGE WATCHDOG: forcing re-initialization -- {reason}")
    await set_connection_state(CONN_READ_ONLY, f"Bridge watchdog: {reason}")
    try:
        client = get_bridge_client()
        await client.health()
        _bridge_initialized = True
        _last_any_tick_ts = time.monotonic()
        c_log("BRIDGE WATCHDOG: bridge health check passed, ticks should resume.")
        await set_connection_state(CONN_RUNNING, "Bridge watchdog: health check succeeded.")
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🔁 <b>Watchdog: bridge health restored</b>\nالسبب: {reason}")
    except (BridgeHTTPError, BridgeConnectionError) as e:
        c_log(f"BRIDGE WATCHDOG: bridge unreachable ({e}) -- will retry.")
        from telegram_ui import send_tg_msg
        await send_tg_msg(
            f"🛑 <b>Watchdog: bridge unreachable</b>\nالسبب الأصلي: {reason}\n"
            f"الخطأ: {e}\nسيُعاد المحاولة تلقائياً بعد 15 ثانية."
        )
    except Exception as e:
        log_exception('_force_full_reconnect', e)
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🛑 <b>Watchdog: فشلت محاولة إعادة الاتصال</b>\nالسبب: {reason}\nالخطأ: {e}")


# ── Market Hours ──
def _is_market_hours(check_bridge: bool = True) -> bool:
    offset = bot_state.get('broker_time_offset', 3)
    broker_now = datetime.now(timezone.utc) + timedelta(hours=offset)
    wd = broker_now.weekday()
    t = broker_now.time()
    if wd == 5:
        return False
    if wd == 4 and t >= dtime(23, 49):
        return False
    if wd == 6 and t < dtime(1, 1):
        return False
    return True


# ── Bridge Lifecycle ──
async def init_bridge() -> None:
    global _bridge_initialized, _tick_poller_task, _bridge_reconnect_task

    from state import load_bot_persistence
    await load_bot_persistence()

    configure_bridge_client(MT5_BRIDGE_URL, MT5_ACCOUNT_ID)
    c_log(f"Bridge client configured: {MT5_BRIDGE_URL} / account={MT5_ACCOUNT_ID}")

    client = get_bridge_client()
    try:
        info = await client.get_account_info()
        bal = info.get('balance', '?')
        eq = info.get('equity', '?')
        c_log(f"Bridge connected — balance={bal} equity={eq}")
        _bridge_initialized = True
        await set_connection_state(CONN_RUNNING, "Bridge connected.")
    except (BridgeHTTPError, BridgeConnectionError) as e:
        c_log(f"Bridge connection failed at startup: {e}")
        await set_connection_state(CONN_READ_ONLY, f"Bridge unreachable at startup: {e}")

    if _tick_poller_task is None or _tick_poller_task.done():
        _tick_poller_task = asyncio.create_task(_tick_poller_loop())
    if _bridge_reconnect_task is None or _bridge_reconnect_task.done():
        _bridge_reconnect_task = asyncio.create_task(_bridge_watchdog())
=======
    global _active_subscriptions, bridge
    if bridge is None or not bridge.sio.connected:
        return
    broker_sym = _resolve_broker_symbol(symbol)
    _broker_to_data_symbol[broker_sym] = symbol
    if broker_sym in _active_subscriptions:
        return  # guard against duplicate subscribe calls
    try:
        await bridge.subscribe([broker_sym])
        _active_subscriptions.add(broker_sym)
    except Exception as e:
        log_exception(f"_lq_subscribe_symbol [{symbol} -> {broker_sym}]", e)


async def _force_full_reconnect(reason: str) -> None:
    global bridge, _last_any_tick_ts, _active_subscriptions
    c_log(f"WS WATCHDOG: forcing full reconnect -- {reason}")
    await set_connection_state(CONN_READ_ONLY, f"WS watchdog: {reason}")
    if bridge is None:
        c_log("WS WATCHDOG: bridge is None — cannot reconnect")
        return
    try:
        _active_subscriptions.clear()  # re-subscribe on reconnect
        if bridge.sio.connected:
            try:
                await asyncio.wait_for(bridge.sio.disconnect(), timeout=15)
            except Exception as e:
                log_exception('_force_full_reconnect: disconnect', e)
        # socketio reconnection (reconnection=True) will re-fire connect ->
        # _resubscribe for all active symbols. Give it a bounded retry.
        connected = False
        for _ in range(5):
            try:
                await asyncio.wait_for(bridge.connect(), timeout=30)
                connected = True
                break
            except Exception as e:
                log_exception('_force_full_reconnect: reconnect attempt', e)
                await asyncio.sleep(5)
        if not connected:
            from telegram_ui import send_tg_msg
            await send_tg_msg(f"🛑 <b>Watchdog: فشلت محاولة إعادة الاتصال التلقائي</b>\nالسبب: {reason}")
            return
        _last_any_tick_ts = time.monotonic()
        c_log("WS WATCHDOG: reconnect successful, ticks should resume.")
        await set_connection_state(CONN_RUNNING, "WS watchdog: forced reconnect succeeded.")
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🔁 <b>Watchdog: أعيد الاتصال تلقائياً بـ DWXConnect</b>\nالسبب: {reason}")
    except Exception as e:
        log_exception('_force_full_reconnect', e)
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🛑 <b>Watchdog: خطأ في إعادة الاتصال</b>\nالسبب الأصلي: {reason}\nالخطأ: {e}")


# ---------------------------------------------------------------------------
# BRIDGE CONNECTION LIFECYCLE
# ---------------------------------------------------------------------------
async def _bootstrap_bridge_connection() -> bool:
    global bridge, _last_any_tick_ts
    try:
        url = bot_state.get('bridge_url') or MT5Bridge().url
        api_key = bot_state.get('bridge_api_key', '')
        bridge = MT5Bridge(url=url, api_key=api_key)
        router = _GannTickRouter()
        bridge.on_tick(router)
        await bridge.connect()
        await bridge.wait_ready()
        # subscribe all active symbols
        for sym, on in bot_state.get('active_symbols', {}).items():
            if on:
                await _lq_subscribe_symbol(sym)
        _last_any_tick_ts = time.monotonic()
        c_log("DWXConnect bridge established (live quotes subscribed).")
        await set_connection_state(CONN_RUNNING, "DWXConnect bridge connected and synchronized.")
        return True
    except Exception as e:
        log_exception("_bootstrap_bridge_connection", e)
        await set_connection_state(CONN_READ_ONLY, f"DWXConnect bridge bootstrap failed: {e}")
        return False


async def init_bridge():
    """Modern entrypoint: connect to the self-hosted MT5 bridge."""
    from state import load_bot_persistence
    await load_bot_persistence()
    if bot_state.get('_persistence_load_failed'):
        await set_connection_state(
            CONN_READ_ONLY,
            "Startup persistence file was present but unreadable. Starting READ_ONLY until a human "
            "confirms the true broker state and clears this manually."
        )
    await _bootstrap_bridge_connection()


# Backwards-compatible alias so main.py's `from market_data import init_metaapi`
# keeps working during the transition.
async def init_metaapi():
    c_log("[market_data] init_metaapi() is deprecated — routing to init_bridge() (DWXConnect).")
    await init_bridge()
>>>>>>> origin/main

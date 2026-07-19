"""
state.py — Global state, configuration, persistence, and connection state machine.

This module owns:
  - bot_state (the single source of truth for all runtime configuration)
  - The connection state machine (RUNNING / READ_ONLY / HALTED)
  - Persistence (save / load bot state to disk)
  - Diagnostic & trade-history logging
  - Utility functions (log_exception, c_log, _safe_float, _safe_task)
  - HTTP session management
"""

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone, time as dtime

import aiohttp
import pandas as pd

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('gold_scalper')


def log_exception(context: str, exc: Exception) -> None:
    logger.error("EXCEPTION in %s: %s\n%s", context, exc, traceback.format_exc())


def c_log(msg: str) -> None:
    """Console log with Damascus (UTC+3) timestamp."""
    dam = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%H:%M:%S')
    print(f"[{dam} DAM] {msg}", flush=True)


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        logger.critical("FATAL: required environment variable '%s' is not set. Refusing to start.", name)
        sys.exit(1)
    return val


MT5_BRIDGE_URL  = _require_env('MT5_BRIDGE_URL')
MT5_ACCOUNT_ID  = _require_env('MT5_ACCOUNT_ID')
TG_TOKEN       = _require_env('TG_TOKEN')
OANDA_ACCOUNT  = _require_env('OANDA_ACCOUNT')
OANDA_TOKEN    = _require_env('OANDA_TOKEN')
AVAILABLE_SYMBOLS = ['XAU_USD', 'XAU_EUR', 'XAG_USD', 'EUR_USD', 'GBP_JPY',
                     'GBP_AUD', 'GBP_NZD', 'AUD_JPY', 'NZD_JPY']
SYMBOL_INFO = {
    'XAU_USD': {'pip_value': 0.1,     'contract_size': 100,    'prec': 2, 'name': 'Gold (USD)'},
    'XAU_EUR': {'pip_value': 0.1,     'contract_size': 100,    'prec': 2, 'name': 'Gold (EUR)'},
    'XAG_USD': {'pip_value': 0.001,   'contract_size': 5000,   'prec': 3, 'name': 'Silver'},
    'EUR_USD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'EUR/USD'},
    'GBP_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'GBP/JPY'},
    'GBP_AUD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'GBP/AUD'},
    'GBP_NZD': {'pip_value': 0.00001, 'contract_size': 100000, 'prec': 5, 'name': 'GBP/NZD'},
    'AUD_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'AUD/JPY'},
    'NZD_JPY': {'pip_value': 0.01,    'contract_size': 100000, 'prec': 3, 'name': 'NZD/JPY'},
}
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com/v3'
_TFS = ['1m', '2m', '3m', '4m', '5m', '6m', '10m', '15m', '20m', '30m', '1h', '2h']
DAM_OFF = timedelta(hours=3)

# ---------------------------------------------------------------------------
# HTTP SESSION
# ---------------------------------------------------------------------------
_http: aiohttp.ClientSession | None = None


def get_http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        timeout   = aiohttp.ClientTimeout(total=30, connect=10)
        _http     = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _http


# ---------------------------------------------------------------------------
# CONNECTION STATE MACHINE
# ---------------------------------------------------------------------------
CONN_RUNNING   = 'RUNNING'
CONN_READ_ONLY = 'READ_ONLY'
CONN_HALTED    = 'HALTED'

_state_lock = asyncio.Lock()
_last_state_notify_ts = 0.0

# Guards mutations to daily PnL counters and gann_open_trades.
_daily_state_lock = asyncio.Lock()

# Guards global failure counters against lost += operations.
_fail_counter_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# PERSISTENCE
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get('PERSISTENT_DATA_PATH', '/app/data')
os.makedirs(DATA_DIR, exist_ok=True)
PERSISTENCE_FILE = os.path.join(DATA_DIR, 'bot_persistence.json')
TEMP_PERSISTENCE_FILE = os.path.join(DATA_DIR, 'bot_persistence.tmp')
PRESETS_FILE = os.path.join(DATA_DIR, 'presets.json')
TEMP_PRESETS_FILE = os.path.join(DATA_DIR, 'presets.tmp')

_PRESET_EXCLUDED_KEYS = {
    'gann_levels', 'gann_level_status', 'gann_cycle_active', 'gann_open_trades',
    'gann_last_h1_time', 'gann_cycle_started_at', 'auto_trade',
}

_persistence_write_lock = asyncio.Lock()


def _write_persistence_file_sync(data: dict) -> None:
    with open(TEMP_PERSISTENCE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(TEMP_PERSISTENCE_FILE, PERSISTENCE_FILE)


def _write_presets_file_sync(data: dict) -> None:
    with open(TEMP_PRESETS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(TEMP_PRESETS_FILE, PRESETS_FILE)


async def save_bot_persistence() -> None:
    """Atomic write of full operational state + settings to disk."""
    try:
        TOP_LEVEL_EXCLUDE = {'connection_obj', 'menu_button_map', 'timeframes',
                              'is_backtesting', 'live_connected', 'last_poll_ok',
                              'symbol_state', 'diag_log'}

        symbol_snapshot = {}
        for sym in sorted(bot_state['active_symbols'].keys()):
            ss = bot_state['symbol_state'].get(sym) or {}
            snap = {k: v for k, v in ss.items()
                    if k not in ('gann_last_h1_time', 'gann_cycle_started_at')}
            snap['gann_last_h1_time'] = ss.get('gann_last_h1_time').isoformat() if ss.get('gann_last_h1_time') else None
            snap['gann_cycle_started_at'] = ss.get('gann_cycle_started_at').isoformat() if ss.get('gann_cycle_started_at') else None
            symbol_snapshot[sym] = snap

        data = {'schema_version': 3, 'symbol_state': symbol_snapshot}
        for k, v in bot_state.items():
            if k not in TOP_LEVEL_EXCLUDE:
                data[k] = v
        raw = bot_state.get('live_daily_date')
        data['live_daily_date'] = raw.isoformat() if hasattr(raw, 'isoformat') else str(raw or '')
    except Exception as e:
        log_exception("save_bot_persistence (snapshot phase)", e)
        return

    try:
        async with _persistence_write_lock:
            await asyncio.to_thread(_write_persistence_file_sync, data)
    except Exception as e:
        log_exception("save_bot_persistence (write phase)", e)
        c_log(f"CRITICAL: Persistence Save Error -- open trade state may not survive a restart: {e}")


async def load_bot_persistence():
    if not os.path.exists(PERSISTENCE_FILE):
        c_log("No persistence file found -- starting fresh (expected on first boot).")
        return
    try:
        def _read():
            with open(PERSISTENCE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        data = await asyncio.to_thread(_read)

        TOP_LEVEL_EXCLUDE = {'connection_obj', 'menu_button_map', 'timeframes',
                              'is_backtesting', 'is_live_twin_running',
                              'live_connected', 'last_poll_ok', 'symbol_state', 'diag_log'}
        for k, v in data.items():
            if k in bot_state and k not in TOP_LEVEL_EXCLUDE and k != 'live_daily_date':
                bot_state[k] = v

        saved_date = data.get('live_daily_date')
        if saved_date and saved_date != 'None' and saved_date:
            try:
                bot_state['live_daily_date'] = datetime.fromisoformat(saved_date).date()
            except Exception:
                bot_state['live_daily_date'] = None

        symbol_state_data = data.get('symbol_state')
        if symbol_state_data is not None:
            for sym, snap in symbol_state_data.items():
                if sym not in bot_state['symbol_state']:
                    continue
                ss = bot_state['symbol_state'][sym]
                for k, v in snap.items():
                    if k in ('gann_last_h1_time', 'gann_cycle_started_at'):
                        continue
                    if k in ss:
                        ss[k] = v
                lh1 = snap.get('gann_last_h1_time')
                ss['gann_last_h1_time'] = pd.Timestamp(lh1).to_pydatetime() if lh1 else None
                csa = snap.get('gann_cycle_started_at')
                ss['gann_cycle_started_at'] = pd.Timestamp(csa).to_pydatetime() if csa else None
        else:
            for sym, trades in data.get('gann_open_trades', {}).items():
                if sym in bot_state['symbol_state']:
                    bot_state['symbol_state'][sym]['gann_open_trades'] = trades

        c_log("Bot state restored from persistence file.")
    except Exception as e:
        log_exception("load_bot_persistence", e)
        c_log(f"CRITICAL: Persistence file exists but failed to load ({e}). "
              f"Bot will start in READ_ONLY to avoid trading blind.")
        bot_state['_persistence_load_failed'] = True


_last_persist_save_ts = 0.0


async def _debounced_persist_save():
    global _last_persist_save_ts
    now = time.monotonic()
    if now - _last_persist_save_ts < 2.0:
        return
    _last_persist_save_ts = now
    await save_bot_persistence()


async def set_connection_state(new_state: str, reason: str) -> None:
    """Transition the connection state machine with Telegram notification."""
    global _last_state_notify_ts
    async with _state_lock:
        old_state = bot_state.get('connection_state', CONN_RUNNING)
        if old_state == new_state:
            return
        bot_state['connection_state'] = new_state
        bot_state['connection_state_reason'] = reason
    logger.warning("Connection state: %s -> %s (%s)", old_state, new_state, reason)
    now_mono = time.monotonic()
    if now_mono - _last_state_notify_ts < 10.0:
        return
    _last_state_notify_ts = now_mono
    icon = {'RUNNING': '\u2705', 'READ_ONLY': '\U0001F7E1', 'HALTED': '\U0001F6D1'}.get(new_state, '\u2139')
    # Circular import avoided via late import
    from telegram_ui import send_tg_msg
    await send_tg_msg(f"{icon} <b>connection state changed: {old_state} -> {new_state}</b>\n{reason}")


async def is_trading_allowed() -> bool:
    """New order placement is allowed only when fully healthy and outside restricted windows."""
    async with _state_lock:
        conn_state = bot_state.get('connection_state', CONN_RUNNING)
    if conn_state != CONN_RUNNING:
        return False
    from strategy import _is_within_dam_restricted_window
    if _is_within_dam_restricted_window():
        return False
    return True


# ---------------------------------------------------------------------------
# DIAGNOSTIC LOG
# ---------------------------------------------------------------------------
_DIAG_LOG_MAX_ENTRIES = 50000


def _diag_log_add(entry: dict) -> None:
    log = bot_state.setdefault('diag_log', [])
    log.append(entry)
    if len(log) > _DIAG_LOG_MAX_ENTRIES:
        del log[: len(log) - _DIAG_LOG_MAX_ENTRIES]


# ---------------------------------------------------------------------------
# TRADE HISTORY
# ---------------------------------------------------------------------------
_trade_history_lock = asyncio.Lock()


async def _record_closed_trade_history(symbol: str, tid: str, tr: dict, exit_px: float,
                                        pnl: float, outcome_label: str, close_reason: str,
                                        pnl_confirmed: bool) -> None:
    try:
        entry = tr.get('entry')
        is_buy = tr.get('is_buy')
        opened_at = tr.get('opened_at')
        closed_at_dt = datetime.now(timezone.utc)
        duration_min = None
        if opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at) if isinstance(opened_at, str) else opened_at
                duration_min = round((closed_at_dt - opened_dt).total_seconds() / 60.0, 1)
            except Exception:
                duration_min = None
        intended_entry = tr.get('level_price', entry)
        entry_slip = (entry - intended_entry) if (entry is not None and intended_entry is not None) else None
        async with _trade_history_lock:
            hist = bot_state.setdefault('live_trade_history', [])
            hist.append({
                'symbol': symbol, 'tid': tid, 'tf': tr.get('tf'),
                'is_real': bool(tr.get('is_real')),
                'is_buy': is_buy, 'opened_at': opened_at,
                'closed_at': closed_at_dt.isoformat(),
                'duration_min': duration_min, 'level_price': intended_entry,
                'entry': entry, 'entry_slippage': entry_slip,
                'tp': tr.get('tp'), 'sl': tr.get('sl'),
                'exit_price': exit_px, 'outcome': outcome_label,
                'pnl': pnl, 'pnl_confirmed_from_broker': pnl_confirmed,
                'close_reason': close_reason,
                'be_activated': bool(tr.get('be_activated')),
                'feed_source': tr.get('feed_source'),
                'feed_age_ms': tr.get('feed_age_ms'),
                'trigger_type': tr.get('trigger_type'),
                'exec_latency_ms': tr.get('exec_latency_ms'),
                'exec_method': tr.get('exec_method'),
                'exec_ioc_fail_reason': tr.get('exec_ioc_fail_reason'),
                'exec_slippage': tr.get('exec_slippage'),
            })
            if len(hist) > _DIAG_LOG_MAX_ENTRIES:
                del hist[: len(hist) - _DIAG_LOG_MAX_ENTRIES]
    except Exception as e:
        log_exception(f'_record_closed_trade_history [{symbol} {tid}]', e)


# ---------------------------------------------------------------------------
# UTILITY
# ---------------------------------------------------------------------------
def _safe_float(value, default: float = 0.0) -> float:
    """Coerce None/non-numeric/NaN/inf to a safe default."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float('inf'), float('-inf')):
        return default
    return f


def _safe_task(coro, name=''):
    """Create a background task with automatic exception logging."""
    t = asyncio.create_task(coro)
    t.add_done_callback(
        lambda fut: log_exception(f'background task [{name}]', fut.exception())
        if not fut.cancelled() and fut.exception() else None
    )
    return t


# ---------------------------------------------------------------------------
# BOT STATE (the single source of truth)
# ---------------------------------------------------------------------------
def _make_symbol_state() -> dict:
    return {
        'gann_levels': [],
        'gann_level_status': {},
        'gann_close_used': None,
        'gann_last_h1_time': None,
        'gann_cycle_active': False,
        'gann_cycle_started_at': None,
        'gann_open_trades': {},
        'auto_trade': False,
        'lot_size': 0.05,
        'gann_cycle_hours': 1,
        'gann_zone_filter': 'star',
        'gann_entry_mode': 'touch_trend',
        'trend_filter_type': 'ema',
        'trend_vwap_period': 100,
        'trend_ema_period': 60,
        'trend_timeframe': '1h',
        'break_even_enabled': False,
        'gann_be_trigger_points': 40,
        'gann_monitor_tfs': {tf: (tf in ['5m', '10m', '15m', '20m', '30m', '1h', '4m', '6m', '2h', '1m', '2m', '3m']) for tf in _TFS},
        'gann_touch_margin_pts': 5,
        'gann_tpsl_mode': 'fixed',
        'gann_tp_points': 70,
        'gann_sl_points': 110,
        'gann_tp_per_tf': {tf: 0 for tf in _TFS},
        'gann_sl_per_tf': {tf: 0 for tf in _TFS},
        'gann_atr_period': 14,
        'gann_atr_sl_mult': 1.5,
        'gann_atr_tp_mult': 2,
    }


bot_state: dict = {
    'connection_state': 'RUNNING',
    'connection_state_reason': '',
    'symbol':           'XAUUSD',
    'live_connected':   False,
    'connection_obj':   None,
    'chat_id':          None,
    'last_update_id':   0,
    'is_backtesting':   False,
    'is_live_twin_running': False,
    'timeframes':       _TFS,

    'gann_execution_mode': 'instant',
    'lt_latency_ms_min': 160,
    'lt_latency_ms_max': 200,
    'gann_spike_limit_pts': 20,

    'lt_mode': 'realistic',
    'lt_base_spread_usd': 0.34,
    'lt_friction': {
        'spread': True, 'slippage': True, 'latency': True,
        'commission': True, 'gaps': True, 'rejection': True,
    },
    'lt_commission_per_lot': 10.0,
    'lt_swap_long_per_lot_night': -93.17,
    'lt_swap_short_per_lot_night': 21.68,
    'lt_swap_wednesday_multiplier': 3.0,
    'lt_swap_per_lot_night': -6.5,
    'lt_rejection_prob': 0.015,

    'menu_button_map': {},
    'last_poll_ok':     0.0,
    'live_daily_realized': 0.0,
    'live_daily_date': None,
    'live_daily_hit': False,

    'active_symbols': {s: (s == 'XAU_USD') for s in AVAILABLE_SYMBOLS},
    'ui_selected_symbol': 'XAU_USD',
    'symbol_state': {s: _make_symbol_state() for s in AVAILABLE_SYMBOLS},

    'prot_daily_dd_usd':      200,
    'prot_daily_profit_usd':  150,
    'prot_true_sync': True,
    'prot_cost_be': True,
    'prot_max_slippage_points': 5,
    'prot_max_concurrent_trades': 4,
    'diag_log': [],
    'live_trade_history': [],
    'prot_stale_filter': True,
    'prot_cycle_inval': True,
    'prot_cycle_inval_pts': 200,
    'gann_anchor_tf': '1h',
    'prot_allow_multi_tf': True,
    'broker_time_offset': 3,
    'gann_calculation_mode': 'static_h1',
}

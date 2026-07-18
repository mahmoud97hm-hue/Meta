"""
strategy.py — Gann level calculation, trend filters, ATR, TP/SL, and time-of-day gates.

Pure strategy logic — no I/O, no execution, no UI.
"""

from datetime import datetime, timedelta, timezone, time as dtime

import numpy as np

from state import bot_state, SYMBOL_INFO

# ---------------------------------------------------------------------------
# GANN LEVELS & FAN ENGINE
# ---------------------------------------------------------------------------
GANN_TFC_H1 = 0.02

GANN_COEFS = [
    {'c': 0.0208, 'star': False, 'fan': False},
    {'c': 0.0417, 'star': False, 'fan': False},
    {'c': 0.0625, 'star': False, 'fan': False},
    {'c': 0.0833, 'star': True,  'fan': False},
    {'c': 0.125,  'star': False, 'fan': True},
    {'c': 0.25,   'star': False, 'fan': False},
    {'c': 0.333,  'star': False, 'fan': False},
    {'c': 0.5,    'star': True,  'fan': False},
    {'c': 1.0,    'star': True,  'fan': False},
    {'c': 2.0,    'star': False, 'fan': False},
    {'c': 4.0,    'star': False, 'fan': False},
]


def _anchor_hours() -> int:
    return 4 if bot_state.get('gann_anchor_tf', '1h') == '4h' else 1


def _anchor_label() -> str:
    return bot_state.get('gann_anchor_tf', '1h').upper()


def gann_calc_levels(symbol: str, close: float) -> list[dict]:
    levels = []
    anchor_tf = bot_state.get('gann_anchor_tf', '1h')
    multiplier = GANN_TFC_H1 * 2.0 if anchor_tf == '4h' else GANN_TFC_H1

    for i, item in enumerate(GANN_COEFS):
        offset = close * item['c'] * multiplier
        prec = SYMBOL_INFO[symbol]['prec']
        up = round(close + offset, prec)
        dn = round(close - offset, prec)

        up_lbl = "مقاومة"
        dn_lbl = "دعم"
        if item['star'] and not item['fan']:
            up_lbl = "مقاومة ⭐"; dn_lbl = "دعم ⭐"
        elif item['star'] and item['fan']:
            up_lbl = "مقاومة ⭐"; dn_lbl = "دعم ⭐"
        elif item['fan']:
            up_lbl = "مقاومة موازية للمروحة 🌀"; dn_lbl = "دعم موازي للمروحة 🌀"

        levels.append({'key': f'up_{i}', 'price': up, 'dir': 'up',
                        'star': item['star'], 'fan': item['fan'], 'label': up_lbl})
        if dn > 0:
            levels.append({'key': f'dn_{i}', 'price': dn, 'dir': 'dn',
                            'star': item['star'], 'fan': item['fan'], 'label': dn_lbl})

    levels.append({'key': 'ref', 'price': round(close, SYMBOL_INFO[symbol]['prec']),
                    'dir': 'ref', 'star': False, 'fan': False,
                    'label': f'إغلاق {_anchor_label()}'})
    levels.sort(key=lambda x: x['price'], reverse=True)
    return levels


def gann_active_levels(symbol: str) -> list[dict]:
    sym_state = bot_state['symbol_state'][symbol]
    lv = [l for l in sym_state['gann_levels'] if l['dir'] != 'ref']
    f = sym_state['gann_zone_filter']
    if f == 'star':
        return [l for l in lv if l['star']]
    elif f == 'star_fan':
        return [l for l in lv if l['star'] or l['fan']]
    return lv


def _gann_tf_tp(symbol: str, tf: str) -> int:
    sym_state = bot_state['symbol_state'][symbol]
    v = sym_state['gann_tp_per_tf'].get(tf, 0)
    return v if v > 0 else sym_state['gann_tp_points']


def _gann_tf_sl(symbol: str, tf: str) -> int:
    sym_state = bot_state['symbol_state'][symbol]
    v = sym_state['gann_sl_per_tf'].get(tf, 0)
    return v if v > 0 else sym_state['gann_sl_points']


# ---------------------------------------------------------------------------
# ATR (pure NumPy — no pandas in hot path)
# ---------------------------------------------------------------------------
def _gann_atr(candles: list, period: int) -> float | None:
    if candles is None or len(candles) < period + 1:
        return None
    recent = candles[-(period + 50):]
    n = len(recent)
    high = np.empty(n, dtype=np.float64)
    low = np.empty(n, dtype=np.float64)
    close = np.empty(n, dtype=np.float64)
    for i, c in enumerate(recent):
        high[i] = c['high']; low[i] = c['low']; close[i] = c['close']
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    return float(np.mean(tr[-period:]))


# ---------------------------------------------------------------------------
# TP/SL CALCULATION
# ---------------------------------------------------------------------------
def _gann_calc_tpsl(symbol: str, entry: float, is_buy: bool, candles: list,
                    tf: str = '') -> tuple[float, float]:
    sym_state = bot_state['symbol_state'][symbol]
    pv = SYMBOL_INFO[symbol]['pip_value']
    prec = SYMBOL_INFO[symbol]['prec']
    if sym_state['gann_tpsl_mode'] == 'atr':
        atr = sym_state.get('gann_atr_cache', {}).get(tf)
        if atr is None and candles:
            atr = _gann_atr(candles, sym_state['gann_atr_period'])
        if not atr:
            atr = _gann_tf_sl(symbol, tf) * pv
        sl_dist = atr * sym_state['gann_atr_sl_mult']
        tp_dist = atr * sym_state['gann_atr_tp_mult']
    else:
        sl_dist = _gann_tf_sl(symbol, tf) * pv
        tp_dist = _gann_tf_tp(symbol, tf) * pv
    if is_buy:
        return round(entry + tp_dist, prec), round(entry - sl_dist, prec)
    return round(entry - tp_dist, prec), round(entry + sl_dist, prec)


# ---------------------------------------------------------------------------
# CORE OUTCOME / BREAK-EVEN LOGIC
# ---------------------------------------------------------------------------
def core_eval_break_even(is_buy: bool, entry: float, current_px: float,
                          pip_value: float, be_pts: int, atr_period: int,
                          cost_be: bool) -> float | None:
    be_dist = be_pts * pip_value
    if (is_buy and current_px >= entry + be_dist) or (not is_buy and current_px <= entry - be_dist):
        be_margin = (atr_period * 0.1 * pip_value) if cost_be else 0.0
        return (entry + be_margin) if is_buy else (entry - be_margin)
    return None


def core_eval_outcome(is_buy: bool, current_px: float, tp: float, sl: float) -> str | None:
    if is_buy:
        if current_px >= tp: return 'WIN ✅'
        if current_px <= sl: return 'LOSS ❌'
    else:
        if current_px <= tp: return 'WIN ✅'
        if current_px >= sl: return 'LOSS ❌'
    return None


# ---------------------------------------------------------------------------
# TIME-OF-DAY FILTERS
# ---------------------------------------------------------------------------
_DAM_RESTRICTED_WINDOWS = [
    (dtime(7, 0),  dtime(9, 0)),
    (dtime(13, 0), dtime(14, 0)),
]


def _is_within_dam_restricted_window() -> bool:
    if not bot_state.get('prot_dam_time_filter', True):
        return False
    from state import DAM_OFF
    dam_now = datetime.now(timezone.utc) + DAM_OFF
    t = dam_now.time()
    return any(start <= t < end for start, end in _DAM_RESTRICTED_WINDOWS)


def _is_market_hours_now() -> bool:
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


# ---------------------------------------------------------------------------
# ANCHOR TIME HELPERS
# ---------------------------------------------------------------------------
def _last_closed_anchor_time_utc(anchor_hours: int, offset_hours: float,
                                  now_utc: datetime) -> datetime:
    broker_now = now_utc + timedelta(hours=offset_hours)
    floored = broker_now.replace(minute=0, second=0, microsecond=0)
    bucket_start_hour = (floored.hour // anchor_hours) * anchor_hours
    bucket_start_broker = floored.replace(hour=bucket_start_hour)
    return bucket_start_broker - timedelta(hours=offset_hours)


async def _gann_fetch_last_closed_anchor(symbol: str) -> dict | None:
    from market_data import fetch_candles
    anchor_tf = bot_state.get('gann_anchor_tf', '1h')
    anchor_hours = _anchor_hours()
    offset = bot_state.get('broker_time_offset', 3)
    target_close_utc = _last_closed_anchor_time_utc(anchor_hours, offset, datetime.now(timezone.utc))
    candles = await fetch_candles(symbol, anchor_tf, count=anchor_hours + 6)
    if not candles:
        return None
    candles = sorted(candles, key=lambda c: c['time'])
    eligible = [c for c in candles if c['time'].to_pydatetime() <= target_close_utc]
    return eligible[-1] if eligible else candles[-1]


# ---------------------------------------------------------------------------
# LEVELS FORMATTING
# ---------------------------------------------------------------------------
def _gann_fmt_levels_msg(symbol: str, close: float) -> str:
    sym_state = bot_state['symbol_state'][symbol]
    lines = []
    for l in sym_state['gann_levels']:
        if l['dir'] == 'ref':
            lines.append(f"➖ <b>{l['price']:.2f}</b>  (إغلاق {_anchor_label()})")
            continue
        icon = '🔴' if l['dir'] == 'up' else '🟢'
        lines.append(f"{icon} {l['price']:.2f}  {l['label']}")

    f_mode = sym_state['gann_zone_filter']
    if f_mode == 'star': filt = '⭐ المستويات الأصلية القوية فقط'
    elif f_mode == 'star_fan': filt = '⭐🌀 القوية + الموازية للمروحة'
    else: filt = '📋 كل المستويات (مخاطرة)'

    flt_trend = sym_state['trend_filter_type'].upper()
    if flt_trend == 'BOTH': flt_trend = 'VWAP + EMA'

    mode = f'لمس مباشر + فلتر ({flt_trend}_{sym_state["trend_timeframe"].upper()})' if sym_state['gann_entry_mode'] == 'touch_trend' else 'لمس أعمى (بدون فلتر)'
    return (f"📐 <b>سلّم جان (المروحة) — دورة جديدة</b>\n"
            f"إغلاق {_anchor_label()}: <b>{close:.2f}</b>\n\n"
            f"مدة المراقبة: {sym_state['gann_cycle_hours']}س  |  فلتر: {filt}\nالدخول: {mode}\n\n\n"
            + '\n'.join(lines))

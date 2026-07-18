"""
telegram_ui.py — Telegram Bot interface: keyboards, message sending, callback dispatch.

Owns:
  - Keyboard builders (main, protection, gann, backtest, live-twin, presets)
  - Message sending (send_tg_msg, edit_tg_msg, send_tg_document)
  - Callback handler (_handle_callback)
  - Telegram polling loop & watchdog
  - /set, /backtest, /backtestreal command parsing
"""

import asyncio
import html as html_mod
import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp
import openpyxl
import pandas as pd
import numpy as np
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from state import (
    bot_state, _TFS, AVAILABLE_SYMBOLS, SYMBOL_INFO, TG_TOKEN,
    CONN_RUNNING, CONN_READ_ONLY, CONN_HALTED, DAM_OFF,
    PRESETS_FILE, TEMP_PRESETS_FILE, _PRESET_EXCLUDED_KEYS,
    get_http, log_exception, c_log, _safe_task, _debounced_persist_save,
    save_bot_persistence, set_connection_state, _write_presets_file_sync,
)
from market_data import (
    _lq_subscribe_symbol, live_quotes,
    _QUOTE_STALE_SECONDS, _lq_price_with_fallback,
)
from strategy import (
    _anchor_label, _anchor_hours, gann_calc_levels, gann_active_levels,
    _gann_fmt_levels_msg, _gann_fetch_last_closed_anchor,
)
from execution import (
    _close_metaapi_trade, _close_metaapi_trades_batch,
)

TG_CAPTION_LIMIT = 1024
_last_state_notify_ts_ui = 0.0


# ── Telegram HTTP Helpers ──
async def _tg_post(url: str, **kwargs) -> bool:
    try:
        sess = get_http()
        async with sess.post(url, **kwargs) as resp:
            if resp.status != 200:
                body = await resp.text()
                c_log(f"Telegram API call failed ({resp.status}) for {url}: {body[:300]}")
            return resp.status == 200
    except Exception as e:
        log_exception(f"_tg_post [{url}]", e)
        return False


def _to_reply_kbd(inline_kbd: dict):
    rows = []; bmap = {}
    for row in inline_kbd.get('inline_keyboard', []):
        new_row = []
        for btn in row:
            text = btn['text']; cb = btn.get('callback_data', 'noop')
            if text in bmap and bmap[text] != cb and cb != 'noop' and bmap[text] != 'noop':
                c_log(f"BUTTON LABEL COLLISION: '{text}' maps to both '{bmap[text]}' and '{cb}'")
            new_row.append({'text': text}); bmap[text] = cb
        rows.append(new_row)
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True,
            'input_field_placeholder': 'اختر من القائمة...'}, bmap


async def send_tg_msg(text: str, reply_markup: dict = None) -> None:
    if not bot_state['chat_id']: return
    if reply_markup and 'inline_keyboard' in reply_markup:
        reply_markup, bmap = _to_reply_kbd(reply_markup)
        bot_state['menu_button_map'] = bmap
    payload = {'chat_id': bot_state['chat_id'], 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage', json=payload)


async def edit_tg_msg(chat_id, message_id, text, reply_markup=None) -> None:
    payload = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML'}
    if reply_markup: payload['reply_markup'] = reply_markup
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/editMessageText', json=payload)


async def _show(chat_id, msg_id, text: str, reply_markup: dict = None) -> None:
    if msg_id: await edit_tg_msg(chat_id, msg_id, text, reply_markup)
    else: await send_tg_msg(text, reply_markup)


async def answer_callback(cbq_id: str) -> None:
    await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery',
                   json={'callback_query_id': cbq_id})


async def send_tg_document(file_path: str, caption: str) -> None:
    if not bot_state['chat_id']: return
    try:
        doc_caption = caption; overflow_text = None
        if len(caption) > TG_CAPTION_LIMIT:
            doc_caption = caption[:TG_CAPTION_LIMIT - 20].rstrip() + "\n... (تابع أدناه)"
            overflow_text = caption
        with open(file_path, 'rb') as f:
            data = aiohttp.FormData()
            data.add_field('chat_id', str(bot_state['chat_id']))
            data.add_field('document', f, filename=os.path.basename(file_path))
            data.add_field('caption', doc_caption)
            await _tg_post(f'https://api.telegram.org/bot{TG_TOKEN}/sendDocument', data=data)
        if overflow_text:
            await send_tg_msg(overflow_text)
    except Exception as e:
        log_exception(f"send_tg_document [{file_path}]", e)


# ── Keyboards ──
def get_main_keyboard() -> dict:
    return {'inline_keyboard': [
        [{'text': '📊 لوحة التحكم المباشرة', 'callback_data': 'menu_dashboard'}],
        [{'text': '🔌 فحص حالة جسر MT5 (DWXConnect)', 'callback_data': 'check_bridge_status'}],
        [{'text': '🩺 تشخيص: ليه مفيش صفقات؟', 'callback_data': 'run_diag'}],
        [{'text': '📊 تصدير سجل تشخيص تفصيلي (Excel)', 'callback_data': 'export_diag_excel'}],
        [{'text': '📒 تصدير سجل الصفقات الحية (Excel)', 'callback_data': 'export_live_trades_excel'}],
        [{'text': '📋 تقرير تفاصيل التنفيذ (Latency/Method/Slippage)', 'callback_data': 'export_exec_report'}],
        [{'text': '📐 محرك جان (الاستراتيجية)', 'callback_data': 'menu_gann'}],
        [{'text': '🛡️ إعدادات الحماية', 'callback_data': 'menu_protection'}],
        [{'text': '💾 إدارة الإعدادات (Presets)', 'callback_data': 'menu_presets'}],
        [{'text': '📊 بدء الباكتيست', 'callback_data': 'menu_gann_bt'}],
        [{'text': '🧪 Live-Twin Simulator (تنفيذ واقعي)', 'callback_data': 'menu_lt'}],
        [{'text': '🔓 استئناف يدوي بعد HALT', 'callback_data': 'manual_resume_step1'}],
    ]}


def get_protection_keyboard() -> dict:
    dd = bot_state['prot_daily_dd_usd']; profit = bot_state['prot_daily_profit_usd']
    multi_tf = '✅ مسموح' if bot_state.get('prot_allow_multi_tf', True) else '❌ ممنوع'
    rows = [
        [{'text': '── الحدود اليومية ──', 'callback_data': 'noop'}],
        [{'text': f'📉 أقصى تراجع يومي: ${dd}', 'callback_data': 'noop'}],
        [{'text': '➖ خسارة $50', 'callback_data': 'prot_dec_dd'},
         {'text': '➕ خسارة $50', 'callback_data': 'prot_inc_dd'}],
        [{'text': f'💰 هدف الربح اليومي: ${profit}', 'callback_data': 'noop'}],
        [{'text': '➖ ربح $50', 'callback_data': 'prot_dec_profit'},
         {'text': '➕ ربح $50', 'callback_data': 'prot_inc_profit'}],
        [{'text': '── الحماية المتقدمة ──', 'callback_data': 'noop'}],
        [{'text': f"مزامنة MT4: {'✅' if bot_state.get('prot_true_sync', True) else '🔴'}", 'callback_data': 'tg_prot_sync'}],
        [{'text': f"إلغاء الدورة وقت الانفجار: {'✅' if bot_state.get('prot_cycle_inval', True) else '🔴'}", 'callback_data': 'tg_prot_inval'}],
        [{'text': f"BE شامل التكلفة: {'✅' if bot_state.get('prot_cost_be', True) else '🔴'}", 'callback_data': 'tg_prot_cost'}],
        [{'text': f"فلتر البيانات المتأخرة: {'✅' if bot_state.get('prot_stale_filter', True) else '🔴'}", 'callback_data': 'tg_prot_stale'}],
        [{'text': f"إطار مرجعي للجان: {bot_state.get('gann_anchor_tf', '1h').upper()}", 'callback_data': 'tg_prot_anchor'}],
        [{'text': f"فلتر أوقات دمشق: {'✅' if bot_state.get('prot_dam_time_filter', True) else '🔴'}", 'callback_data': 'tg_prot_dam_time'}],
        [{'text': f"حساب جان: {'⚡ حي' if bot_state.get('gann_calculation_mode', 'static_h1') == 'dynamic_live' else '📌 كلاسيكي'}", 'callback_data': 'tg_gann_calc_mode'}],
        [{'text': f'تكرار الصفقات (Multi-TF): {multi_tf}', 'callback_data': 'prot_toggle_multitf'}],
        [{'text': '🔄 تصفير كل الحمايات النشطة الآن', 'callback_data': 'prot_reset_all'}],
        [{'text': '🔙 رجوع للقائمة الرئيسية', 'callback_data': 'menu_main'}],
        [{'text': '🔙 رجوع لإعدادات جان', 'callback_data': 'menu_gann'}],
    ]
    return {'inline_keyboard': rows}


def get_gann_keyboard() -> dict:
    sym = bot_state['ui_selected_symbol']; sym_state = bot_state['symbol_state'][sym]
    zf = sym_state['gann_zone_filter']; em = sym_state['gann_entry_mode']
    mg = sym_state['gann_touch_margin_pts']; tpsm = sym_state['gann_tpsl_mode']
    hrs = sym_state['gann_cycle_hours']
    cyc = '🟢 نشطة' if sym_state['gann_cycle_active'] else '⚫ غير نشطة'
    open_n = len(sym_state['gann_open_trades'])
    flt_type = sym_state['trend_filter_type']
    if zf == 'star': zf_lbl = '⭐ المستويات الأصلية القوية فقط'
    elif zf == 'star_fan': zf_lbl = '⭐🌀 القوية + موازية للمروحة'
    else: zf_lbl = '📋 كل المستويات (للتجارب)'
    if flt_type == 'ema': filt_btn_lbl = "📉 الفلتر المعتمد: (EMA الشامل)"; flt_name = 'EMA'
    else: filt_btn_lbl = "🌊 الفلتر المعتمد: (VWAP الشامل)"; flt_name = 'VWAP'
    ttf_lbl = sym_state['trend_timeframe'].upper()
    em_lbl = f'⚡ لمس + فلتر ({flt_name}_{ttf_lbl})' if em == 'touch_trend' else '⚡ لمس أعمى (بدون فلتر)'
    tps_lbl = f'🎯 TP/SL: {"نقاط ثابتة" if tpsm == "fixed" else "حسب ATR"}'
    tp = sym_state['gann_tp_points']; sl = sym_state['gann_sl_points']
    atp = sym_state['gann_atr_tp_mult']; asp = sym_state['gann_atr_sl_mult']
    ap = sym_state['gann_atr_period']
    be_lbl = "🟢 مفعل" if sym_state['break_even_enabled'] else "⚫ معطل"
    auto_t = '🟢 مفعل' if sym_state.get('auto_trade', False) else '🔴 معطل'
    exec_mode = bot_state.get('gann_execution_mode', 'instant')
    exec_lbl = {'instant': '⚡ دخول لمس مباشر (Instant)', 'close': '⏳ انتظار إغلاق الشمعة (Close)',
                'hybrid': '🛡️ مباشر هجين (Hybrid Spike-Limit)',
                'all_concurrent': '🔀 الثلاثة معاً (All-Concurrent)'}.get(exec_mode, '⚡ دخول لمس مباشر (Instant)')
    rows = [
        [{'text': f'🤖 التداول الآلي (MetaAPI): {auto_t}', 'callback_data': 'gann_toggle_auto_trade'}],
        [{'text': '🛡️ إعدادات الحماية المتقدمة', 'callback_data': 'menu_protection'}],
        [{'text': f'📐 {sym} — دورة: {cyc}  |  صفقات: {open_n}', 'callback_data': 'noop'}],
        [{'text': '🔄 عرض الدعوم والمقاومات الحالية', 'callback_data': 'gann_show_levels'}],
        [{'text': '🕯️ تشخيص: آخر 10 شموع', 'callback_data': 'gann_show_last10'}],
        [{'text': '── أزواج التداول ──', 'callback_data': 'noop'}],
    ]
    pair_row = []
    for p in AVAILABLE_SYMBOLS:
        icon = '✅' if bot_state['active_symbols'][p] else '⬜'
        pair_row.append({'text': f'{icon} {p}', 'callback_data': f'gann_toggle_pair_{p}'})
        if len(pair_row) == 2: rows.append(pair_row); pair_row = []
    if pair_row: rows.append(pair_row)
    rows.append([{'text': '── تخصيص إعدادات الزوج ──', 'callback_data': 'noop'}])
    sel_row = []
    for p in AVAILABLE_SYMBOLS:
        sel = '📌 ' if p == sym else ''
        sel_row.append({'text': f'{sel}{p}', 'callback_data': f'gann_sel_pair_{p}'})
        if len(sel_row) == 2: rows.append(sel_row); sel_row = []
    if sel_row: rows.append(sel_row)
    rows += [
        [{'text': '── الاستراتيجية والفلتر ──', 'callback_data': 'noop'}],
        [{'text': f'الاستراتيجية: {em_lbl}', 'callback_data': 'gann_toggle_entry'}],
        [{'text': f'وضع التنفيذ: {exec_lbl}', 'callback_data': 'gann_toggle_exec_mode'}],
        [{'text': f'فلتر الدخول: {zf_lbl}', 'callback_data': 'gann_toggle_filter'}],
        [{'text': filt_btn_lbl, 'callback_data': 'gann_toggle_filter_type'}],
        [{'text': f'⏱️ فريم الترند: {ttf_lbl}', 'callback_data': 'gann_toggle_ttf'}],
        [{'text': f'🛡️ صمام الأمان (Break-Even): {be_lbl}', 'callback_data': 'gann_toggle_be'}],
    ]
    if sym_state.get('break_even_enabled', False):
        be_pts = sym_state.get('gann_be_trigger_points', 40)
        rows.append([{'text': 'BE −10p', 'callback_data': 'gann_dec_be_pts'},
                     {'text': f'تفعيل بعد: {be_pts}p', 'callback_data': 'noop'},
                     {'text': 'BE +10p', 'callback_data': 'gann_inc_be_pts'}])
    if flt_type == 'vwap':
        vwap_val = sym_state['trend_vwap_period']
        rows.append([{'text': 'VWAP −10', 'callback_data': 'gann_dec_vwap'},
                     {'text': f'قيمة {ttf_lbl} VWAP: {vwap_val}', 'callback_data': 'noop'},
                     {'text': 'VWAP +10', 'callback_data': 'gann_inc_vwap'}])
    if flt_type == 'ema':
        ema_val = sym_state['trend_ema_period']
        rows.append([{'text': 'EMA −10', 'callback_data': 'gann_dec_ema'},
                     {'text': f'قيمة {ttf_lbl} EMA: {ema_val}', 'callback_data': 'noop'},
                     {'text': 'EMA +10', 'callback_data': 'gann_inc_ema'}])
    rows += [
        [{'text': '📝 مساعدة: تغيير القيم', 'callback_data': 'gann_filter_help'}],
        [{'text': '── فريمات التنفيذ ──', 'callback_data': 'noop'}],
    ]
    tf_items = list(sym_state['gann_monitor_tfs'].items())
    for i in range(0, len(tf_items), 4):
        rows.append([{'text': ('✅' if on else '⬜') + f' {tfk}',
                      'callback_data': f'gann_tf_{tfk}'} for tfk, on in tf_items[i:i+4]])
    rows += [
        [{'text': '── إعدادات عامة ──', 'callback_data': 'noop'}],
        [{'text': '−ساعة', 'callback_data': 'gann_dec_hours'},
         {'text': f'مدة تجميد السلّم: {hrs} ساعة', 'callback_data': 'noop'},
         {'text': '+ساعة', 'callback_data': 'gann_inc_hours'}],
        [{'text': 'Lot −0.01', 'callback_data': 'gann_dec_lot'},
         {'text': f'حجم اللوت: {sym_state["lot_size"]}', 'callback_data': 'noop'},
         {'text': 'Lot +0.01', 'callback_data': 'gann_inc_lot'}],
        [{'text': 'Margin −1', 'callback_data': 'gann_dec_margin'},
         {'text': f'هامش اللمس {mg}p', 'callback_data': 'noop'},
         {'text': 'Margin +1', 'callback_data': 'gann_inc_margin'}],
        [{'text': '── TP / SL ──', 'callback_data': 'noop'}],
        [{'text': tps_lbl, 'callback_data': 'gann_toggle_tpsl'}],
    ]
    if tpsm == 'fixed':
        rows += [
            [{'text': 'TP −10', 'callback_data': 'gann_dec_tp10'},
             {'text': f'TP={tp}p', 'callback_data': 'noop'},
             {'text': 'TP +10', 'callback_data': 'gann_inc_tp10'}],
            [{'text': 'SL −10', 'callback_data': 'gann_dec_sl10'},
             {'text': f'SL={sl}p', 'callback_data': 'noop'},
             {'text': 'SL +10', 'callback_data': 'gann_inc_sl10'}],
        ]
    else:
        rows += [
            [{'text': 'ATR Period −', 'callback_data': 'gann_dec_atrp'},
             {'text': f'Period={ap}', 'callback_data': 'noop'},
             {'text': 'ATR Period +', 'callback_data': 'gann_inc_atrp'}],
            [{'text': 'SL mult −0.5', 'callback_data': 'gann_dec_atrsl'},
             {'text': f'SL×{asp}', 'callback_data': 'noop'},
             {'text': 'SL mult +0.5', 'callback_data': 'gann_inc_atrsl'}],
            [{'text': 'TP mult −0.5', 'callback_data': 'gann_dec_atrtp'},
             {'text': f'TP×{atp}', 'callback_data': 'noop'},
             {'text': 'TP mult +0.5', 'callback_data': 'gann_inc_atrtp'}],
        ]
    rows += [
        [{'text': '⚙️ TP/SL مخصص لكل فريم', 'callback_data': 'gann_tpsl_tf'}],
        [{'text': '📊 بدء الباكتيست', 'callback_data': 'menu_gann_bt'}],
        [{'text': '← رجوع', 'callback_data': 'menu_main'}],
    ]
    return {'inline_keyboard': rows}


def get_gann_tpsl_tf_keyboard(sel_tf: str = '') -> dict:
    sym_state = bot_state['symbol_state'][bot_state['ui_selected_symbol']]
    rows = [[{'text': '⚙️ TP/SL مخصص لكل فريم', 'callback_data': 'noop'}],
            [{'text': '(0 = يرجع للقيمة العامة)', 'callback_data': 'noop'}]]
    tfs_list = list(sym_state['gann_monitor_tfs'].keys())
    tf_row = []
    for tfk in tfs_list:
        icon = '👉' if tfk == sel_tf else ''
        tf_row.append({'text': f'{icon}{tfk}', 'callback_data': f'gann_tptf_sel_{tfk}'})
        if len(tf_row) == 4: rows.append(tf_row); tf_row = []
    if tf_row: rows.append(tf_row)
    if sel_tf:
        tp_v = sym_state['gann_tp_per_tf'].get(sel_tf, 0); sl_v = sym_state['gann_sl_per_tf'].get(sel_tf, 0)
        eff_tp = tp_v if tp_v > 0 else sym_state['gann_tp_points']
        eff_sl = sl_v if sl_v > 0 else sym_state['gann_sl_points']
        rows += [
            [{'text': f'── [{sel_tf}] ──', 'callback_data': 'noop'}],
            [{'text': f'TP فعلي: {eff_tp}p {"(مخصص)" if tp_v>0 else "(عام)"}', 'callback_data': 'noop'}],
            [{'text': 'TP −10', 'callback_data': f'gann_tptf_dtp_{sel_tf}'},
             {'text': f'TP={tp_v}', 'callback_data': 'noop'},
             {'text': 'TP +10', 'callback_data': f'gann_tptf_itp_{sel_tf}'}],
            [{'text': f'SL فعلي: {eff_sl}p {"(مخصص)" if sl_v>0 else "(عام)"}', 'callback_data': 'noop'}],
            [{'text': 'SL −10', 'callback_data': f'gann_tptf_dsl_{sel_tf}'},
             {'text': f'SL={sl_v}', 'callback_data': 'noop'},
             {'text': 'SL +10', 'callback_data': f'gann_tptf_isl_{sel_tf}'}],
            [{'text': '↺ إعادة ضبط', 'callback_data': f'gann_tptf_rst_{sel_tf}'}],
        ]
    rows.append([{'text': '← رجوع', 'callback_data': 'menu_gann'}])
    return {'inline_keyboard': rows}


def get_gann_bt_keyboard() -> dict:
    if bot_state['is_backtesting']:
        return {'inline_keyboard': [[{'text': '⏳ الباكتيست يعمل...', 'callback_data': 'noop'}],
                                     [{'text': '⏹ إلغاء', 'callback_data': 'cancel_bt'}]]}
    return {'inline_keyboard': [
        [{'text': 'يوم واحد', 'callback_data': 'gbt_1'}, {'text': 'يومين', 'callback_data': 'gbt_2'}],
        [{'text': 'ثلاثة أيام', 'callback_data': 'gbt_3'}, {'text': 'أسبوع', 'callback_data': 'gbt_7'}],
        [{'text': 'شهر كامل', 'callback_data': 'gbt_30'}],
        [{'text': 'أو أرسل: /backtest YYYY-MM-DD', 'callback_data': 'noop'}],
        [{'text': '← رجوع', 'callback_data': 'menu_gann'}],
    ]}


def get_live_twin_keyboard() -> dict:
    if bot_state['is_live_twin_running']:
        return {'inline_keyboard': [[{'text': '⏳ Live-Twin يعمل...', 'callback_data': 'noop'}],
                                     [{'text': '⏹ إلغاء', 'callback_data': 'cancel_lt'}]]}
    mode = bot_state.get('lt_mode', 'realistic')
    mode_label = '🧪 واقعي (Live-Twin)' if mode == 'realistic' else '🧊 مثالي (Idealized A/B)'
    return {'inline_keyboard': [
        [{'text': f'الوضع: {mode_label}', 'callback_data': 'lt_toggle_mode'}],
        [{'text': '⚙️ إعدادات الاحتكاك (Friction)', 'callback_data': 'menu_lt_friction'}],
        [{'text': 'يوم واحد', 'callback_data': 'lt_1'}, {'text': 'يومين', 'callback_data': 'lt_2'}],
        [{'text': 'ثلاثة أيام', 'callback_data': 'lt_3'}, {'text': 'أسبوع', 'callback_data': 'lt_7'}],
        [{'text': 'شهر كامل', 'callback_data': 'lt_30'}],
        [{'text': 'أو أرسل: /backtestreal YYYY-MM-DD', 'callback_data': 'noop'}],
        [{'text': '← رجوع', 'callback_data': 'menu_main'}],
    ]}


def get_live_twin_friction_keyboard() -> dict:
    fric = bot_state['lt_friction']
    def tag(key, label):
        return {'text': f"{label}: {'✅' if fric.get(key) else '🔴'}", 'callback_data': f'lt_fric_{key}'}
    return {'inline_keyboard': [
        [{'text': f"Spread أساسي: ${bot_state['lt_base_spread_usd']} (34pt)", 'callback_data': 'noop'}],
        [tag('spread', '📶 سبريد ديناميكي')],
        [tag('slippage', '⚡ انزلاق (Slippage)')],
        [tag('latency', '⏱ تأخير التنفيذ (200-800ms)')],
        [tag('commission', '💵 عمولة')],
        [tag('gaps', '📉 فجوات نهاية الأسبوع/Rollover')],
        [tag('rejection', '🚫 رفض/Requote')],
        [{'text': '← رجوع', 'callback_data': 'menu_lt'}],
    ]}


# ── Bridge (DWXConnect) Status Command ──
async def check_bridge_status_command(chat_id: int):
    from mt5_bridge import bridge as _bridge
    from market_data import live_quotes, _last_any_tick_ts
    await send_tg_msg("⏳ جاري فحص حالة جسر MT5 (DWXConnect)...")
    try:
        if _bridge is None or not _bridge.sio.connected:
            await send_tg_msg("⚠️ <b>جسر MT5 غير متصل حالياً.</b> تحقق من حاوية mt5-bridge على Railway.")
            return
        client = None
        try:
            from mt5_bridge import get_execution_client
            client = get_execution_client()
        except RuntimeError:
            client = None
        # pull a live snapshot of open positions via the bridge
        positions = []
        if client is not None:
            try:
                positions = await client.get_positions()
            except Exception:
                positions = []
        tick_age = time.monotonic() - _last_any_tick_ts
        msg = (
            f"<b>حالة جسر MT5 (DWXConnect)</b>\n"
            f"الاتصال: ✅ متصل\n"
            f"عمر آخر تيك: {tick_age:.1f}s\n"
            f"الأدوات الحية: {len(live_quotes)}\n"
            f"الصفقات المفتوحة: {len(positions)}\n"
        )
        for p in positions[:10]:
            sym = p.get('symbol') or p.get('id') or '?'
            msg += f"🔸 {sym}\n"
        await send_tg_msg(msg)
    except Exception as e:
        await send_tg_msg(f"❌ خطأ في فحص جسر MT5:\n{html_mod.escape(str(e))}")


# ── Callback Dispatcher ──

# ════════════════════════════════════════════════════════════
# DISPATCH TABLES — exact-match + prefix-match callbacks.
# To add a new button: just add an entry to _EXACT_HANDLERS
# or a prefix+handler to _PREFIX_MAP.  No dispatcher changes.
# ════════════════════════════════════════════════════════════

# ── Exact-match early handlers (async operations that spawn tasks) ──
async def _handle_callback(d: str, chat_id: int, msg_id: int) -> None:
    from execution import _consecutive_real_order_failures as _exec_failures
    from gann_monitor import _recon_consecutive_mismatches

    if d == 'check_bridge_status':
        _safe_task(check_bridge_status_command(chat_id), 'check_bridge_status'); return
    if d == 'run_diag':
        async def _run_diag_task():
            try:
                from gann_monitor import gann_run_diagnostics
                report = await gann_run_diagnostics()
                sections = report.split('\n\n'); chunk = ""
                for sec in sections:
                    if len(chunk) + len(sec) + 2 > 3500:
                        if chunk.strip(): await send_tg_msg(chunk)
                        chunk = ""
                        if len(sec) > 3500:
                            for line in sec.split('\n'):
                                if len(chunk) + len(line) + 1 > 3500:
                                    await send_tg_msg(chunk); chunk = ""
                                chunk += line + "\n"
                            continue
                    chunk += sec + "\n\n"
                if chunk.strip(): await send_tg_msg(chunk)
            except Exception as e:
                log_exception('gann_run_diagnostics', e); await send_tg_msg(f"❌ فشل التشخيص: {e}")
        _safe_task(_run_diag_task(), 'run_diag'); return
    if d == 'export_diag_excel':
        async def _export_diag_task():
            try:
                from gann_monitor import export_diag_log_excel
                await export_diag_log_excel()
            except Exception as e:
                log_exception('export_diag_log_excel', e); await send_tg_msg(f"❌ فشل تصدير سجل التشخيص: {e}")
        asyncio.create_task(_export_diag_task()); return
    if d == 'export_live_trades_excel':
        async def _export_live_trades_task():
            try:
                from gann_monitor import export_live_trades_excel
                await export_live_trades_excel()
            except Exception as e:
                log_exception('export_live_trades_excel', e); await send_tg_msg(f"❌ فشل تصدير سجل الصفقات الحية: {e}")
        _safe_task(_export_live_trades_task(), 'export_live_trades_excel'); return
    if d == 'export_exec_report':
        async def _export_exec_report_task():
            fname = None
            try:
                from gann_monitor import export_execution_details_report
                fname = await export_execution_details_report()
                if fname is None: await send_tg_msg("📭 <b>لا يوجد سجل صفقات حية مغلقة بعد.</b>"); return
                hist = bot_state.get('live_trade_history', [])
                wins = sum(1 for t in hist if t.get('outcome') == 'WIN')
                losses = sum(1 for t in hist if t.get('outcome') == 'LOSS')
                total_pnl = sum(t.get('pnl', 0.0) for t in hist)
                wr = round(100 * wins / max(wins + losses, 1), 1)
                caption = f"📋 <b>تقرير تفاصيل التنفيذ</b>\n{len(hist)} صفقة | WR: {wr}% | صافي: {total_pnl:+.2f}$"
                await send_tg_document(fname, caption)
            except Exception as e:
                log_exception('export_execution_details_report', e); await send_tg_msg(f"❌ فشل إنشاء تقرير التنفيذ: {e}")
            finally:
                if fname and os.path.exists(fname):
                    try: os.remove(fname)
                    except Exception: pass
        _safe_task(_export_exec_report_task(), 'export_exec_report'); return
    if d == 'menu_dashboard':
        # Live dashboard
        conn_state = bot_state.get('connection_state', CONN_RUNNING)
        icon_c = {'RUNNING': '✅', 'READ_ONLY': '🟡', 'HALTED': '🛑'}.get(conn_state, '❓')
        daily_pnl = bot_state.get('live_daily_realized', 0.0)
        daily_hit = bot_state.get('live_daily_hit', False)
        total_open = sum(len(ss.get('gann_open_trades', {})) for ss in bot_state['symbol_state'].values())
        ws_status = '✅ حية' if live_quotes else '🛑 لا توجد تغذية'
        hit_msg = '🛑 مفعّل (الإدخالات الجديدة متوقفة)' if daily_hit else '✅ طبيعي'
        text = (
            f"<b>📊 لوحة التحكم المباشرة</b>\n\n"
            f"📡 حالة الاتصال: {icon_c} <b>{conn_state}</b>\n"
            f"💰 ربح/خسارة يومي محقق: <b>${daily_pnl:+.2f}</b>\n"
            f"🔒 قفل الحماية اليومي: {hit_msg}\n"
            f"📈 صفقات مفتوحة: <b>{total_open}</b>\n"
            f"📶 تغذية الأسعار اللحظية (WS): {ws_status}\n"
        )
        await _show(chat_id, msg_id, text, get_main_keyboard())
        return
    if d == 'manual_resume_step1':
        current_state = bot_state.get('connection_state', CONN_RUNNING)
        if current_state == CONN_RUNNING:
            await send_tg_msg("✅ البوت أصلاً في حالة RUNNING."); return
        await send_tg_msg(
            f"⚠️ <b>تأكيد الاستئناف اليدوي</b>\nالحالة الحالية: {current_state}\n"
            f"السبب: {bot_state.get('connection_state_reason', '-')}\n\n"
            f"هل تأكدت فعلياً من حساب الوسيط؟",
            reply_markup={'inline_keyboard': [
                [{'text': '✅ نعم، تأكدت -- استأنف الآن', 'callback_data': 'manual_resume_confirm'}],
                [{'text': '❌ إلغاء', 'callback_data': 'menu_main'}],
            ]})
        return
    if d == 'manual_resume_confirm':
        prior_state = bot_state.get('connection_state', CONN_RUNNING)
        import execution; import gann_monitor
        execution._consecutive_real_order_failures = 0
        gann_monitor._recon_consecutive_mismatches = 0
        await set_connection_state(CONN_RUNNING, f"Manually resumed by operator (was {prior_state}).")
        await send_tg_msg("✅ تم الاستئناف اليدوي. البوت الآن RUNNING.")
        return

    sym = bot_state['ui_selected_symbol']; sym_state = bot_state['symbol_state'][sym]

    # ════════════════════════════════════════════════════════════
    # TABLE-DRIVEN DISPATCH (Phase C) — replaces 240-line if/elif.
    # Exact matches: O(1) dict lookup.  Prefix matches: scan _PREFIX_MAP.
    # Complex handlers are separate async functions below.
    # ════════════════════════════════════════════════════════════

    if d in _EXACT_HANDLERS:
        await _EXACT_HANDLERS[d](chat_id, msg_id, sym, sym_state)
    else:
        for prefix, handler in _PREFIX_MAP:
            if d.startswith(prefix):
                await handler(d, chat_id, msg_id, sym, sym_state)
                break
        else:
            c_log(f'Unhandled callback: {d}')
    await _debounced_persist_save()


# ═══════════════════════════════════════════════════════════════
# DISPATCH TABLES & HANDLER FUNCTIONS (Phase C)
# ═══════════════════════════════════════════════════════════════

_EXACT_HANDLERS = {}

def _exact(key):
    def deco(fn):
        _EXACT_HANDLERS[key] = fn
        return fn
    return deco

_PREFIX_MAP = []

# ── Navigation ──
@_exact('menu_main')
async def _cb_menu_main(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, '<b>مرحباً بك في Gold Scalper Bot v9.5</b>', get_main_keyboard())

@_exact('menu_presets')
async def _cb_menu_presets(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, '<b>إدارة الإعدادات (Presets):</b>',
        {'inline_keyboard': [
            [{'text': '💾 حفظ كـ Preset 1', 'callback_data': 'save_preset_1'}, {'text': '📂 تحميل Preset 1', 'callback_data': 'load_preset_1'}],
            [{'text': '💾 حفظ كـ Preset 2', 'callback_data': 'save_preset_2'}, {'text': '📂 تحميل Preset 2', 'callback_data': 'load_preset_2'}],
            [{'text': '💾 حفظ كـ Preset 3', 'callback_data': 'save_preset_3'}, {'text': '📂 تحميل Preset 3', 'callback_data': 'load_preset_3'}],
            [{'text': '🔙 رجوع', 'callback_data': 'menu_main'}],
        ]})

@_exact('menu_protection')
async def _cb_menu_protection(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('menu_gann')
async def _cb_menu_gann(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('menu_gann_bt')
async def _cb_menu_gann_bt(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, 'اختر مدة الباكتيست:', get_gann_bt_keyboard())

@_exact('menu_lt')
async def _cb_menu_lt(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, '🧪 Live-Twin Simulator:', get_live_twin_keyboard())

@_exact('menu_lt_friction')
async def _cb_menu_lt_friction(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, '⚙️ إعدادات الاحتكاك:', get_live_twin_friction_keyboard())

# ── Protection toggles ──
@_exact('prot_toggle_multitf')
async def _cb_prot_toggle_multitf(chat_id, msg_id, sym, sym_state):
    bot_state['prot_allow_multi_tf'] = not bot_state['prot_allow_multi_tf']
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('prot_dec_dd')
async def _cb_prot_dec_dd(chat_id, msg_id, sym, sym_state):
    bot_state['prot_daily_dd_usd'] = max(50, bot_state['prot_daily_dd_usd'] - 50)
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('prot_inc_dd')
async def _cb_prot_inc_dd(chat_id, msg_id, sym, sym_state):
    bot_state['prot_daily_dd_usd'] = min(5000, bot_state['prot_daily_dd_usd'] + 50)
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('prot_dec_profit')
async def _cb_prot_dec_profit(chat_id, msg_id, sym, sym_state):
    bot_state['prot_daily_profit_usd'] = max(0, bot_state['prot_daily_profit_usd'] - 50)
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('prot_inc_profit')
async def _cb_prot_inc_profit(chat_id, msg_id, sym, sym_state):
    bot_state['prot_daily_profit_usd'] = min(10000, bot_state['prot_daily_profit_usd'] + 50)
    await _show(chat_id, msg_id, 'إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_sync')
async def _cb_tg_prot_sync(chat_id, msg_id, sym, sym_state):
    bot_state['prot_true_sync'] = not bot_state.get('prot_true_sync', True)
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_inval')
async def _cb_tg_prot_inval(chat_id, msg_id, sym, sym_state):
    bot_state['prot_cycle_inval'] = not bot_state.get('prot_cycle_inval', True)
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_cost')
async def _cb_tg_prot_cost(chat_id, msg_id, sym, sym_state):
    bot_state['prot_cost_be'] = not bot_state.get('prot_cost_be', True)
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_stale')
async def _cb_tg_prot_stale(chat_id, msg_id, sym, sym_state):
    bot_state['prot_stale_filter'] = not bot_state.get('prot_stale_filter', True)
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_dam_time')
async def _cb_tg_prot_dam_time(chat_id, msg_id, sym, sym_state):
    bot_state['prot_dam_time_filter'] = not bot_state.get('prot_dam_time_filter', True)
    await save_bot_persistence()
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())

@_exact('tg_prot_anchor')
async def _cb_tg_prot_anchor(chat_id, msg_id, sym, sym_state):
    bot_state['gann_anchor_tf'] = '4h' if bot_state.get('gann_anchor_tf', '1h') == '1h' else '1h'
    for sname, ss in bot_state['symbol_state'].items():
        ss['gann_last_h1_time'] = None; ss['gann_cycle_started_at'] = None
    await save_bot_persistence()
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())
    await send_tg_msg(f"✅ <b>تم تغيير الإطار المرجعي إلى {_anchor_label()}</b>")

@_exact('tg_gann_calc_mode')
async def _cb_tg_gann_calc_mode(chat_id, msg_id, sym, sym_state):
    new_mode = 'static_h1' if bot_state.get('gann_calculation_mode', 'static_h1') == 'dynamic_live' else 'dynamic_live'
    bot_state['gann_calculation_mode'] = new_mode
    for sname, ss in bot_state['symbol_state'].items():
        ss['gann_last_h1_time'] = None; ss['gann_cycle_started_at'] = None
    await save_bot_persistence()
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())
    await send_tg_msg(f"✅ <b>وضع حساب جان: {'⚡ حي (Dynamic Live)' if new_mode == 'dynamic_live' else '📌 كلاسيكي (Static)'}</b>")

@_exact('prot_reset_all')
async def _cb_prot_reset_all(chat_id, msg_id, sym, sym_state):
    was_daily_hit = bot_state.get('live_daily_hit', False); bot_state['live_daily_hit'] = False
    frozen_symbols = []
    for sname, ss in bot_state['symbol_state'].items():
        if ss.get('gann_close_used') is None and not ss.get('gann_levels'): continue
        frozen_symbols.append(sname); ss['gann_levels'] = []; ss['gann_close_used'] = None
        ss['gann_last_h1_time'] = None; ss['gann_cycle_started_at'] = None
    await save_bot_persistence()
    await _show(chat_id, msg_id, '🛡️ إعدادات الحماية:', get_protection_keyboard())
    summary = []
    if was_daily_hit: summary.append("• قفل حماية رأس المال اليومي — تم فكّه")
    if frozen_symbols: summary.append(f"• تجميد الدورة — تم فكّه لـ: {', '.join(frozen_symbols)}")
    if not summary: summary.append("لا توجد حمايات نشطة حالياً.")
    await send_tg_msg("🔄 <b>تصفير الحمايات</b>\n\n" + "\n".join(summary))

# ── Gann display/strategy ──
@_exact('gann_show_levels')
async def _cb_gann_show_levels(chat_id, msg_id, sym, sym_state):
    if not sym_state['gann_levels'] or not sym_state['gann_close_used']:
        await send_tg_msg(f'⏳ لا يوجد سلّم نشط لـ {sym}، جاري جلب آخر شمعة...')
        last_h1 = await _gann_fetch_last_closed_anchor(sym)
        if last_h1:
            h1_close = float(last_h1['close'])
            sym_state['gann_levels'] = gann_calc_levels(sym, h1_close)
            sym_state['gann_close_used'] = h1_close
            sym_state.update({'gann_last_h1_time': last_h1['time'],
                              'gann_cycle_started_at': datetime.now(timezone.utc),
                              'gann_cycle_active': True})
        else:
            await send_tg_msg('❌ تعذّر جلب البيانات.'); return
    await send_tg_msg(_gann_fmt_levels_msg(sym, sym_state['gann_close_used']))
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_show_last10')
async def _cb_gann_show_last10(chat_id, msg_id, sym, sym_state):
    anchor_tf = bot_state.get('gann_anchor_tf', '1h'); anchor_hours = _anchor_hours()
    offset = bot_state.get('broker_time_offset', 3)
    await send_tg_msg(f'⏳ جاري جلب آخر 10 شموع {_anchor_label()} لـ {sym}...')
    from market_data import fetch_candles
    candles = await fetch_candles(sym, anchor_tf, count=10)
    if not candles: await send_tg_msg('❌ تعذّر جلب الشموع.'); return
    candles = sorted(candles, key=lambda c: c['time'])[-10:]
    bot_pick = await _gann_fetch_last_closed_anchor(sym)
    bot_pick_time = bot_pick['time'] if bot_pick else None
    lines = [f'🕯️ <b>آخر 10 شموع {_anchor_label()} — {sym}</b>',
             f'(المصدر: OANDA | التوقيت: دمشق UTC+{offset})', '']
    for i, c in enumerate(candles, 1):
        t_utc = c['time'].to_pydatetime()
        t_dam_start = t_utc + timedelta(hours=offset)
        t_dam_end = t_dam_start + timedelta(hours=anchor_hours)
        marker = ' ✅ ← يعتمدها البوت' if bot_pick_time and t_utc == bot_pick_time else ''
        lines.append(f"{i}) {t_dam_start.strftime('%m-%d %H:%M')} → {t_dam_end.strftime('%H:%M')} دمشق\n    إغلاق: {float(c['close']):.5f}{marker}")
    if not bot_pick_time: lines.append('\n⚠️ لم يتمكن البوت من تحديد آخر شمعة مغلقة.')
    await send_tg_msg('\n'.join(lines)); await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_filter_help')
async def _cb_gann_filter_help(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id,
        "<b>⚙️ دليل تخصيص القيم:</b>\n\n"
        "<b>تغيير فلاتر الترند:</b>\n<code>/set ema 50</code>\n<code>/set vwap 100</code>\n\n"
        "<b>تخصيص الأهداف لكل فريم:</b>\n<code>/set 5m tp 40</code>\n<code>/set 15m sl 25</code>",
        get_gann_keyboard())

@_exact('gann_tpsl_tf')
async def _cb_gann_tpsl_tf(chat_id, msg_id, sym, sym_state):
    await _show(chat_id, msg_id, '⚙️ TP/SL مخصص لكل فريم:', get_gann_tpsl_tf_keyboard())

# ── Toggle handlers ──
@_exact('gann_toggle_entry')
async def _cb_gann_toggle_entry(chat_id, msg_id, sym, sym_state):
    sym_state['gann_entry_mode'] = 'pure_touch' if sym_state['gann_entry_mode'] == 'touch_trend' else 'touch_trend'
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_filter')
async def _cb_gann_toggle_filter(chat_id, msg_id, sym, sym_state):
    cur = sym_state['gann_zone_filter']
    sym_state['gann_zone_filter'] = 'star_fan' if cur == 'star' else 'all' if cur == 'star_fan' else 'star'
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_filter_type')
async def _cb_gann_toggle_filter_type(chat_id, msg_id, sym, sym_state):
    sym_state['trend_filter_type'] = 'ema' if sym_state['trend_filter_type'] == 'vwap' else 'vwap'
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_exec_mode')
async def _cb_gann_toggle_exec_mode(chat_id, msg_id, sym, sym_state):
    order = ['instant', 'close', 'hybrid', 'all_concurrent']
    cur = bot_state.get('gann_execution_mode', 'instant')
    bot_state['gann_execution_mode'] = order[(order.index(cur) + 1) % len(order)]
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_auto_trade')
async def _cb_gann_toggle_auto_trade(chat_id, msg_id, sym, sym_state):
    sym_state['auto_trade'] = not sym_state.get('auto_trade', False)
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_ttf')
async def _cb_gann_toggle_ttf(chat_id, msg_id, sym, sym_state):
    sym_state['trend_timeframe'] = '30m' if sym_state['trend_timeframe'] == '1h' else '1h'
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_be')
async def _cb_gann_toggle_be(chat_id, msg_id, sym, sym_state):
    sym_state['break_even_enabled'] = not sym_state['break_even_enabled']
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_toggle_tpsl')
async def _cb_gann_toggle_tpsl(chat_id, msg_id, sym, sym_state):
    sym_state['gann_tpsl_mode'] = 'atr' if sym_state['gann_tpsl_mode'] == 'fixed' else 'fixed'
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('cancel_bt')
async def _cb_cancel_bt(chat_id, msg_id, sym, sym_state):
    from backtest import _bt_progress
    if _bt_progress and bot_state['is_backtesting']: await _bt_progress.cancel()
    bot_state['is_backtesting'] = False
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('cancel_lt')
async def _cb_cancel_lt(chat_id, msg_id, sym, sym_state):
    from backtest import _lt_progress
    if _lt_progress and bot_state['is_live_twin_running']: await _lt_progress.cancel()
    bot_state['is_live_twin_running'] = False
    await _show(chat_id, msg_id, '🧪 Live-Twin Simulator:', get_live_twin_keyboard())

@_exact('lt_toggle_mode')
async def _cb_lt_toggle_mode(chat_id, msg_id, sym, sym_state):
    bot_state['lt_mode'] = 'idealized' if bot_state.get('lt_mode', 'realistic') == 'realistic' else 'realistic'
    await _show(chat_id, msg_id, '🧪 Live-Twin Simulator:', get_live_twin_keyboard())

@_exact('gann_dec_lot')
async def _cb_gann_dec_lot(chat_id, msg_id, sym, sym_state):
    sym_state['lot_size'] = round(max(0.01, sym_state['lot_size'] - 0.01), 2)
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

@_exact('gann_inc_lot')
async def _cb_gann_inc_lot(chat_id, msg_id, sym, sym_state):
    sym_state['lot_size'] = round(min(50.0, sym_state['lot_size'] + 0.01), 2)
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

# ── Numeric +/- handlers (generated) ──
for _spec in [
    ('gann_dec_be_pts',  'gann_be_trigger_points', 10, 200, -10, int),
    ('gann_inc_be_pts',  'gann_be_trigger_points', 10, 200, +10, int),
    ('gann_dec_vwap',    'trend_vwap_period',      10, 500, -10, int),
    ('gann_inc_vwap',    'trend_vwap_period',      10, 500, +10, int),
    ('gann_dec_ema',     'trend_ema_period',       10, 500, -10, int),
    ('gann_inc_ema',     'trend_ema_period',       10, 500, +10, int),
    ('gann_dec_margin',  'gann_touch_margin_pts',  1,  50,  -1, int),
    ('gann_inc_margin',  'gann_touch_margin_pts',  1,  50,  +1, int),
    ('gann_dec_hours',   'gann_cycle_hours',       1,  24,  -1, int),
    ('gann_inc_hours',   'gann_cycle_hours',       1,  24,  +1, int),
    ('gann_dec_tp10',    'gann_tp_points',         10, 1000, -10, int),
    ('gann_inc_tp10',    'gann_tp_points',         10, 1000, +10, int),
    ('gann_dec_sl10',    'gann_sl_points',         10, 1000, -10, int),
    ('gann_inc_sl10',    'gann_sl_points',         10, 1000, +10, int),
    ('gann_dec_atrp',    'gann_atr_period',        5,  50,  -1, int),
    ('gann_inc_atrp',    'gann_atr_period',        5,  50,  +1, int),
    ('gann_dec_atrsl',   'gann_atr_sl_mult',       0.5, 5.0, -0.5, 'round1'),
    ('gann_inc_atrsl',   'gann_atr_sl_mult',       0.5, 5.0, +0.5, 'round1'),
    ('gann_dec_atrtp',   'gann_atr_tp_mult',       0.5, 8.0, -0.5, 'round1'),
    ('gann_inc_atrtp',   'gann_atr_tp_mult',       0.5, 8.0, +0.5, 'round1'),
]:
    _key, _attr, _lo, _hi, _step, _cast = _spec
    def _make(_attr=_attr, _lo=_lo, _hi=_hi, _step=_step, _cast=_cast):
        async def _h(chat_id, msg_id, sym, sym_state):
            if _cast == 'round1':
                sym_state[_attr] = max(_lo, min(_hi, round(sym_state[_attr] + _step, 1)))
            else:
                sym_state[_attr] = _cast(max(_lo, min(_hi, sym_state[_attr] + _step)))
            await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())
        return _h
    _EXACT_HANDLERS[_key] = _make()

# ═══════════════════════════════════════════════════════════════
# PREFIX HANDLERS
# ═══════════════════════════════════════════════════════════════

async def _pfx_save_preset(d, chat_id, msg_id, sym, sym_state):
    p_num = d.split('_')[-1]; data = {}
    if os.path.exists(PRESETS_FILE):
        try:
            import json; data = json.load(open(PRESETS_FILE))
        except Exception as e:
            log_exception(f"save_preset_{p_num} (reading)", e); data = {}
    import json
    data[f'preset_{p_num}'] = {
        s_name: {k: v for k, v in s_data.items() if k not in _PRESET_EXCLUDED_KEYS}
        for s_name, s_data in bot_state['symbol_state'].items()
    }
    try:
        await asyncio.to_thread(_write_presets_file_sync, data)
        await send_tg_msg(f"✅ تم حفظ الإعدادات الحالية في Preset {p_num}")
    except Exception as e:
        log_exception(f"save_preset_{p_num} (writing)", e)
        await send_tg_msg(f"❌ فشل حفظ Preset {p_num}: {e}")

async def _pfx_load_preset(d, chat_id, msg_id, sym, sym_state):
    p_num = d.split('_')[-1]
    if not os.path.exists(PRESETS_FILE):
        await send_tg_msg("❌ لا يوجد ملف Presets محفوظ بعد."); return
    try:
        import json; data = json.load(open(PRESETS_FILE))
        if f'preset_{p_num}' in data:
            for s_name, s_data in data[f'preset_{p_num}'].items():
                if s_name in bot_state['symbol_state']:
                    for k, v in s_data.items():
                        if k not in _PRESET_EXCLUDED_KEYS:
                            bot_state['symbol_state'][s_name][k] = v
            await send_tg_msg(f"✅ تم تحميل الإعدادات من Preset {p_num} بنجاح!")
        else:
            await send_tg_msg("❌ لا يوجد إعدادات محفوظة في هذا الـ Preset.")
    except Exception as e:
        log_exception(f"load_preset_{p_num}", e); await send_tg_msg(f"❌ حدث خطأ: {e}")

async def _pfx_toggle_pair(d, chat_id, msg_id, sym, sym_state):
    pair = d[len('gann_toggle_pair_'):]
    bot_state['active_symbols'][pair] = not bot_state['active_symbols'][pair]
    if bot_state['active_symbols'][pair]: await _lq_subscribe_symbol(pair)
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

async def _pfx_select_pair(d, chat_id, msg_id, sym, sym_state):
    bot_state['ui_selected_symbol'] = d[len('gann_sel_pair_'):]
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

async def _pfx_toggle_tf(d, chat_id, msg_id, sym, sym_state):
    tfk = d[len('gann_tf_'):]
    if tfk in sym_state['gann_monitor_tfs']:
        sym_state['gann_monitor_tfs'][tfk] = not sym_state['gann_monitor_tfs'][tfk]
    await _show(chat_id, msg_id, 'إعدادات جان:', get_gann_keyboard())

async def _pfx_tptf_sel(d, chat_id, msg_id, sym, sym_state):
    sel = d[len('gann_tptf_sel_'):]
    await _show(chat_id, msg_id, f'⚙️ TP/SL [{sel}]:', get_gann_tpsl_tf_keyboard(sel))

def _make_tptf_handler(prefix, attr, delta):
    async def _h(d, chat_id, msg_id, sym, sym_state):
        tf = d[len(prefix):]
        sym_state[attr][tf] = max(0, sym_state[attr].get(tf, 0) + delta) if delta < 0 else sym_state[attr].get(tf, 0) + delta
        await _show(chat_id, msg_id, f'⚙️ TP/SL [{tf}]:', get_gann_tpsl_tf_keyboard(tf))
    return _h

async def _pfx_tptf_rst(d, chat_id, msg_id, sym, sym_state):
    tf = d[len('gann_tptf_rst_'):]
    sym_state['gann_tp_per_tf'][tf] = 0; sym_state['gann_sl_per_tf'][tf] = 0
    await _show(chat_id, msg_id, '⚙️ تمت إعادة الضبط:', get_gann_tpsl_tf_keyboard(tf))

async def _pfx_gbt(d, chat_id, msg_id, sym, sym_state):
    days = int(d.split('_')[1]); end_dt = datetime.now(timezone.utc); start_dt = end_dt - timedelta(days=days)
    if not bot_state['is_backtesting']:
        bot_state['is_backtesting'] = True
        from backtest import run_gann_backtest
        _safe_task(run_gann_backtest(start_dt, end_dt), 'backtest_preset')
    await _show(chat_id, msg_id, '⏳ باكتيست يعمل...', get_gann_bt_keyboard())

async def _pfx_lt_fric(d, chat_id, msg_id, sym, sym_state):
    key = d[len('lt_fric_'):]
    if key in bot_state['lt_friction']:
        bot_state['lt_friction'][key] = not bot_state['lt_friction'][key]
    await _show(chat_id, msg_id, '⚙️ إعدادات الاحتكاك:', get_live_twin_friction_keyboard())

async def _pfx_lt(d, chat_id, msg_id, sym, sym_state):
    try:
        days = int(d.split('_')[1]); end_dt = datetime.now(timezone.utc); start_dt = end_dt - timedelta(days=days)
        if not bot_state['is_live_twin_running']:
            bot_state['is_live_twin_running'] = True
            from backtest import run_live_twin_simulation, run_live_twin_forward
            _safe_task(run_live_twin_simulation(start_dt, end_dt), 'livetwin_preset')
            if bot_state.get('lt_mode', 'realistic') == 'realistic':
                _safe_task(run_live_twin_forward(), 'livetwin_forward')
        await _show(chat_id, msg_id, '⏳ Live-Twin يعمل...', get_live_twin_keyboard())
    except ValueError: pass

_PREFIX_MAP.extend([
    ('save_preset_',      _pfx_save_preset),
    ('load_preset_',      _pfx_load_preset),
    ('gann_toggle_pair_', _pfx_toggle_pair),
    ('gann_sel_pair_',    _pfx_select_pair),
    ('gann_tf_',          _pfx_toggle_tf),
    ('gann_tptf_sel_',    _pfx_tptf_sel),
    ('gann_tptf_itp_',    _make_tptf_handler('gann_tptf_itp_', 'gann_tp_per_tf', +10)),
    ('gann_tptf_dtp_',    _make_tptf_handler('gann_tptf_dtp_', 'gann_tp_per_tf', -10)),
    ('gann_tptf_isl_',    _make_tptf_handler('gann_tptf_isl_', 'gann_sl_per_tf', +10)),
    ('gann_tptf_dsl_',    _make_tptf_handler('gann_tptf_dsl_', 'gann_sl_per_tf', -10)),
    ('gann_tptf_rst_',    _pfx_tptf_rst),
    ('gbt_',              _pfx_gbt),
    ('lt_fric_',          _pfx_lt_fric),
    ('lt_',               _pfx_lt),
])


# ── Telegram Polling ──
_poll_task: asyncio.Task | None = None


async def telegram_polling_loop() -> None:
    c_log('Telegram polling started.')
    url = f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates'
    backoff = 1
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=28)
    sess = aiohttp.ClientSession(connector=connector, timeout=timeout)
    try:
        while True:
            try:
                async with sess.get(url, params={'offset': bot_state['last_update_id'] + 1, 'timeout': 20}) as resp:
                    if resp.status == 200:
                        backoff = 1; bot_state['last_poll_ok'] = datetime.now(timezone.utc).timestamp()
                        data = await resp.json()
                        for upd in data.get('result', []):
                            bot_state['last_update_id'] = upd['update_id']
                            asyncio.create_task(process_tg_update(upd))
                    else:
                        await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_exception('telegram_polling_loop', e)
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 30)
    finally:
        await sess.close()


async def telegram_watchdog() -> None:
    global _poll_task
    await asyncio.sleep(30)
    while True:
        await asyncio.sleep(20)
        last = bot_state.get('last_poll_ok', 0.0)
        age = datetime.now(timezone.utc).timestamp() - last
        if age > 60 and _poll_task is not None and not _poll_task.done():
            _poll_task.cancel()


async def process_tg_update(update: dict) -> None:
    if 'message' in update and 'text' in update['message']:
        msg = update['message']['text'].strip()
        bot_state['chat_id'] = update['message']['chat']['id']
        parts = msg.lower().split()
        if parts[0] == '/set':
            sym_state = bot_state['symbol_state'][bot_state['ui_selected_symbol']]
            if len(parts) == 3 and parts[1] in ['ema', 'vwap'] and parts[2].isdigit():
                val = int(parts[2])
                if parts[1] == 'ema': sym_state['trend_ema_period'] = val
                elif parts[1] == 'vwap': sym_state['trend_vwap_period'] = val
                await save_bot_persistence()
                await send_tg_msg(f"✅ <b>تم التحديث!</b>\n⚙️ {parts[1].upper()} الشامل: {val}")
                return
            elif len(parts) == 4:
                _, tf, param, val = parts
                if tf in _TFS and param in ['tp', 'sl'] and val.isdigit():
                    val = int(val)
                    if param == 'tp': sym_state['gann_tp_per_tf'][tf] = val
                    elif param == 'sl': sym_state['gann_sl_per_tf'][tf] = val
                    await save_bot_persistence()
                    await send_tg_msg(f"✅ <b>تم التحديث!</b>\n📌 الفريم: {tf}\n⚙️ {param.upper()}: {val}")
                    return
            await send_tg_msg("❌ <b>صيغة خاطئة!</b>\nأمثلة:\n<code>/set ema 50</code>\n<code>/set 5m tp 40</code>")
            return
        if parts[0] == '/backtest':
            try:
                if len(parts) == 2:
                    dam_midnight = datetime.strptime(parts[1], "%Y-%m-%d")
                    dt = (dam_midnight - DAM_OFF).replace(tzinfo=timezone.utc)
                    if not bot_state['is_backtesting']:
                        bot_state['is_backtesting'] = True
                        from backtest import run_gann_backtest
                        _safe_task(run_gann_backtest(dt, dt + timedelta(days=1)), 'backtest_cmd')
                    await send_tg_msg(f"⏳ جاري باكتيست ليوم {parts[1]}...")
                    return
                elif len(parts) == 3:
                    dam_midnight1 = datetime.strptime(parts[1], "%Y-%m-%d")
                    dam_midnight2 = datetime.strptime(parts[2], "%Y-%m-%d") + timedelta(days=1)
                    dt1 = (dam_midnight1 - DAM_OFF).replace(tzinfo=timezone.utc)
                    dt2 = (dam_midnight2 - DAM_OFF).replace(tzinfo=timezone.utc)
                    if not bot_state['is_backtesting']:
                        bot_state['is_backtesting'] = True
                        from backtest import run_gann_backtest
                        _safe_task(run_gann_backtest(dt1, dt2), 'backtest_range_cmd')
                    await send_tg_msg(f"⏳ جاري باكتيست من {parts[1]} إلى {parts[2]}...")
                    return
            except Exception:
                await send_tg_msg("❌ <b>خطأ في التاريخ!</b>\nالصيغة: <code>/backtest 2026-06-24</code>")
                return
        if parts[0] in ('/livetwin', '/backtestreal'):
            try:
                if len(parts) == 2:
                    dam_midnight = datetime.strptime(parts[1], "%Y-%m-%d")
                    dt = (dam_midnight - DAM_OFF).replace(tzinfo=timezone.utc)
                    if not bot_state['is_live_twin_running']:
                        bot_state['is_live_twin_running'] = True
                        from backtest import run_live_twin_simulation, run_live_twin_forward
                        _safe_task(run_live_twin_simulation(dt, dt + timedelta(days=1)), 'livetwin_cmd')
                        if bot_state.get('lt_mode', 'realistic') == 'realistic':
                            _safe_task(run_live_twin_forward(), 'livetwin_forward')
                    await send_tg_msg(f"⏳ جاري Live-Twin ليوم {parts[1]}...")
                    return
                elif len(parts) == 3:
                    dam_midnight1 = datetime.strptime(parts[1], "%Y-%m-%d")
                    dam_midnight2 = datetime.strptime(parts[2], "%Y-%m-%d") + timedelta(days=1)
                    dt1 = (dam_midnight1 - DAM_OFF).replace(tzinfo=timezone.utc)
                    dt2 = (dam_midnight2 - DAM_OFF).replace(tzinfo=timezone.utc)
                    if not bot_state['is_live_twin_running']:
                        bot_state['is_live_twin_running'] = True
                        from backtest import run_live_twin_simulation, run_live_twin_forward
                        _safe_task(run_live_twin_simulation(dt1, dt2), 'livetwin_range_cmd')
                        if bot_state.get('lt_mode', 'realistic') == 'realistic':
                            _safe_task(run_live_twin_forward(), 'livetwin_forward')
                    await send_tg_msg(f"⏳ جاري Live-Twin من {parts[1]} إلى {parts[2]}...")
                    return
            except Exception:
                await send_tg_msg("❌ <b>خطأ في التاريخ!</b>")
                return
        if not msg.startswith('/') and msg in bot_state.get('menu_button_map', {}):
            cb = bot_state['menu_button_map'][msg]
            if cb != 'noop': await _handle_callback(cb, bot_state['chat_id'], None)
            return
        if msg.startswith('/setsymbol '):
            new_sym = msg.split(' ')[1].strip()
            bot_state['symbol'] = new_sym
            await save_bot_persistence()
            await send_tg_msg(f"✅ تم تغيير الرمز الخاص بـ MetaTrader إلى: <b>{new_sym}</b>")
        elif msg == '/start': await send_tg_msg('<b>مرحباً بك في Gold Scalper Bot v9.5</b>', get_main_keyboard())
        else: await send_tg_msg("❌ أمر غير معروف. استخدم /start لعرض القائمة.")
        return

    if 'callback_query' not in update: return
    q = update['callback_query']; d = q['data']
    chat_id = q['message']['chat']['id']; msg_id = q['message']['message_id']
    bot_state['chat_id'] = chat_id
    _safe_task(answer_callback(q['id']), 'answer_callback')
    try: await _handle_callback(d, chat_id, msg_id)
    except Exception as e: log_exception(f'callback dispatch [{d}]', e)

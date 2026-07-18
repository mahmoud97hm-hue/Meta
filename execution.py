"""
execution.py — Order execution, trade management, fill monitoring, and tick-level
trade dispatch over the self-hosted DWXConnect MT5 bridge.

Owns:
  - _execute_smart_order (limit/market/FOK with IOC emulation)
  - _gann_open_trade (full entry lifecycle)
  - _gann_tick_fire_check (tick-driven level-touch detection)
  - _close_metaapi_trade / _close_metaapi_trades_batch  (close via bridge)
  - _fill_monitor_loop (event-driven fill detection via bridge.get_positions)
  - _ExecTracker (execution quality metrics)

All order routing now flows through mt5_bridge.MT5ExecutionClient, which maps
high-level intents onto DWXConnect MT_ORDER_SEND payloads using the
Correlation-ID command dispatcher. No MetaApi dependency remains.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from state import (
    bot_state, SYMBOL_INFO, CONN_RUNNING, CONN_HALTED, CONN_READ_ONLY,
    _daily_state_lock, _fail_counter_lock, _safe_float, _safe_task,
    _debounced_persist_save, save_bot_persistence, log_exception, c_log,
    is_trading_allowed, set_connection_state, _record_closed_trade_history,
)
from market_data import (
    live_quotes, _lq_price_with_fallback,
    _resolve_broker_symbol, _tick_semaphore, _gann_cache,
)
from mt5_bridge import get_execution_client
from strategy import (
    _gann_calc_tpsl, _gann_tf_tp, _gann_tf_sl,
    core_eval_break_even, core_eval_outcome,
)

# ── Execution Quality Tracker ──
class _ExecTracker:
    def __init__(self, maxlen=200):
        self.orders = []
        self.maxlen = maxlen

    def record(self, symbol, is_buy, level_price, fill_price, fill_source,
               latency_ms, method_used, success, error=None):
        slippage = None
        if fill_price is not None and level_price is not None:
            slippage = round(abs(fill_price - level_price), 5)
        err_str = str(error)[:200] if error else None
        self.orders.append({
            'ts': time.monotonic(), 'symbol': symbol, 'is_buy': is_buy,
            'level_price': level_price, 'fill_price': fill_price,
            'slippage': slippage, 'latency_ms': latency_ms,
            'method': method_used, 'success': success, 'error': err_str,
        })
        if len(self.orders) > self.maxlen:
            self.orders.pop(0)

    def avg_slippage(self, symbol=None, n=20):
        recent = [o for o in self.orders if o['slippage'] is not None
                  and (symbol is None or o['symbol'] == symbol)][-n:]
        return sum(o['slippage'] for o in recent) / len(recent) if recent else None

    def limit_fill_rate(self, symbol=None, n=50):
        recent = [o for o in self.orders if o['method'] == 'limit'
                  and (symbol is None or o['symbol'] == symbol)][-n:]
        return sum(1 for o in recent if o['success']) / len(recent) if recent else None


_exec_tracker = _ExecTracker()

# ── Fill Monitor ──
_fill_events: dict[str, asyncio.Event] = {}
_fill_results: dict[str, dict] = {}
_fill_monitor_started = False
_fill_monitor_task: asyncio.Task | None = None
_fill_monitor_lock = asyncio.Lock()


async def _start_fill_monitor():
    global _fill_monitor_started, _fill_monitor_task
    async with _fill_monitor_lock:
        if _fill_monitor_started:
            return
        _fill_monitor_started = True
        _fill_monitor_task = asyncio.create_task(_fill_monitor_loop())


async def _fill_monitor_loop():
    try:
        while True:
            await asyncio.sleep(0.05)
            if not _fill_events:
                continue
            try:
                client = get_execution_client()
                positions = await client.get_positions()
                if not positions:
                    continue
                for p in positions:
                    pid = str(p.get('id') or p.get('ticket') or p.get('position') or '')
                    if pid in _fill_events and pid not in _fill_results:
                        open_price = p.get('openPrice') or p.get('price') or p.get('open_price')
                        if open_price is not None:
                            _fill_results[pid] = {
                                'fill_price': float(open_price),
                                'fill_source': 'confirmed_position',
                                'trade_id': pid,
                            }
                            _fill_events[pid].set()
            except Exception as e:
                log_exception('_fill_monitor_loop', e)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log_exception('_fill_monitor_loop crashed', e)


# ── Global failure counters ──
_consecutive_real_order_failures = 0
_REAL_ORDER_FAILURE_HALT_THRESHOLD = 3


# ── Smart Order Execution ──
class _SkipLimitPhase(Exception):
    pass


async def _execute_smart_order(symbol: str, is_buy: bool, lot: float,
                                level_price: float, sl: float, tp: float,
                                t1_signal_ts: float,
                                max_slippage_points: int) -> dict:
    broker_symbol = _resolve_broker_symbol(symbol)
    trade_id = None; fill_price = None; fill_source = None
    method_used = None; error = None; ioc_fail_reason = None
    t_start_overall = time.monotonic()

    # ── Phase 1: Smart Limit Order ──
    q = live_quotes.get(symbol)
    spread = None
    if q and 'ask' in q and 'bid' in q:
        spread = q['ask'] - q['bid']

    if spread and spread > 0:
        smart_limit_price = level_price - spread / 2 if is_buy else level_price + spread / 2
        if is_buy and smart_limit_price > level_price:
            smart_limit_price = level_price
        if not is_buy and smart_limit_price < level_price:
            smart_limit_price = level_price
    else:
        smart_limit_price = level_price

    limit_price = smart_limit_price
    market_price = None
    try:
        q = live_quotes.get(symbol)
        if q and 'ask' in q and 'bid' in q:
            market_price = float(q['bid'] if is_buy else q['ask'])
    except Exception:
        pass

    if is_buy:
        if market_price is not None and limit_price > market_price:
            ioc_fail_reason = 'Skipped — level above market for buy limit'
            raise _SkipLimitPhase(ioc_fail_reason)
    else:
        if market_price is not None and limit_price < market_price:
            ioc_fail_reason = 'Skipped — level below market for sell limit'
            raise _SkipLimitPhase(ioc_fail_reason)

    margin = bot_state['symbol_state'][symbol]['gann_touch_margin_pts'] * SYMBOL_INFO[symbol]['pip_value']

    limit_opts = {
        'slippage': max_slippage_points,
        'comment': 'limit_buy_gann' if is_buy else 'limit_sell_gann',
        'expirationType': 'ORDER_TIME_SPECIFIED',
        'expiration': datetime.utcnow() + timedelta(seconds=30),
    }

    t_start = time.monotonic()
    try:
        client = get_execution_client()
        res = await client.send_limit_order(
            broker_symbol, is_buy, lot, limit_price, sl=sl, tp=tp,
            deviation=max_slippage_points, comment=limit_opts['comment'],
            expiration=limit_opts.get('expiration'),
        )
        t_ack = time.monotonic()
        latency_ms = round((t_ack - t_start) * 1000)

        # DWX result carries ticket/positionId; fall back to echo if absent
        trade_id_candidate = str(
            res.get('ticket') or res.get('positionId') or res.get('orderId') or '')

        await _start_fill_monitor()
        fill_event = asyncio.Event()
        _fill_events[trade_id_candidate] = fill_event

        ioc_emulation_timeout = 0.1
        try:
            await asyncio.wait_for(fill_event.wait(), timeout=ioc_emulation_timeout)
            fill_data = _fill_results.pop(trade_id_candidate, None)
            if fill_data:
                fill_price = fill_data['fill_price']
                trade_id = fill_data['trade_id']
                fill_source = fill_data['fill_source']
                method_used = 'limit'
        except asyncio.TimeoutError:
            fill_price = None

        _fill_events.pop(trade_id_candidate, None)
        _fill_results.pop(trade_id_candidate, None)

        if fill_price is not None:
            _exec_tracker.record(symbol, is_buy, level_price, fill_price,
                                 fill_source, latency_ms, 'limit', True)
            return {'success': True, 'trade_id': trade_id,
                    'fill_price': fill_price, 'fill_source': fill_source,
                    'latency_ms': latency_ms, 'method_used': 'limit',
                    'error': None, 'ioc_fail_reason': None}

        # Cancel pending limit before Phase 2
        cancel_ok = False
        try:
            await client.cancel_pending_order(broker_symbol, trade_id_candidate)
            cancel_ok = True
        except Exception as cancel_e:
            log_exception(f'_execute_smart_order cancel_pending [{symbol}/{trade_id_candidate}]', cancel_e)
            from telegram_ui import send_tg_msg
            await send_tg_msg(
                f"🚨 <b>خطر: فشل إلغاء أمر Limit معلق!</b>\n"
                f"الرمز: {symbol} | الأمر: {trade_id_candidate}\n"
                f"الخطأ: {cancel_e}\n\n"
                f"تم إلغاء مرحلة Market FOK (Phase 2) لمنع تنفيذ مزدوج.\n"
                f"⚠️ تحقق يدوياً من منصة MT5 — قد يكون الأمر ما زال معلقاً."
            )
            _exec_tracker.record(symbol, is_buy, level_price, None,
                                 None, round((time.monotonic() - t_start_overall) * 1000),
                                 'limit', False, error=cancel_e)
            return {'success': False, 'trade_id': None,
                    'fill_price': None, 'fill_source': None,
                    'latency_ms': round((time.monotonic() - t_start_overall) * 1000),
                    'method_used': None,
                    'error': RuntimeError(f'Pending limit cancel failed: {cancel_e}'),
                    'ioc_fail_reason': 'Phase 1 cancel failed — Phase 2 blocked to prevent double-fill'}

        ioc_fail_reason = 'Limit IOC emulation timeout — price moved away from level'

    except _SkipLimitPhase:
        ioc_fail_reason = ioc_fail_reason or 'Phase 1 skipped'
    except Exception as e:
        latency_ms = round((time.monotonic() - t_start) * 1000)
        error = e
        ioc_fail_reason = f'Limit order raised exception: {e}'
        _exec_tracker.record(symbol, is_buy, level_price, None,
                             None, latency_ms, 'limit', False, error=e)

    # ── Phase 2: Market FOK ──
    avg_slip = _exec_tracker.avg_slippage(symbol, n=50)
    adaptive_slip_pts = max_slippage_points
    if avg_slip is not None:
        pip_val = SYMBOL_INFO[symbol]['pip_value']
        avg_slip_pips = avg_slip / pip_val if pip_val > 0 else 0
        if avg_slip_pips > 2.0:
            adaptive_slip_pts = int(max_slippage_points * 1.5)

    market_opts = {
        'slippage': adaptive_slip_pts,
        'fillingModes': ['ORDER_FILLING_FOK'],
        'comment': 'market_buy_fbk' if is_buy else 'market_sell_fbk',
    }

    t_start = time.monotonic()
    try:
        client = get_execution_client()
        res = await client.send_market_order(
            broker_symbol, is_buy, lot, sl=sl, tp=tp,
            deviation=adaptive_slip_pts, comment=market_opts['comment'],
            type_filling=1,
        )
        t_ack = time.monotonic()
        latency_ms = round((t_ack - t_start) * 1000)

        trade_id = str(
            res.get('ticket') or res.get('positionId') or res.get('orderId') or '')

        await _start_fill_monitor()
        fill_event = asyncio.Event()
        _fill_events[trade_id] = fill_event
        try:
            await asyncio.wait_for(fill_event.wait(), timeout=2.0)
            fill_data = _fill_results.pop(trade_id, None)
            if fill_data:
                fill_price = fill_data['fill_price']
                fill_source = fill_data['fill_source']
                trade_id = fill_data['trade_id']
                method_used = 'market_fallback'
        except asyncio.TimeoutError:
            fill_price = None
        finally:
            _fill_events.pop(trade_id, None)
            _fill_results.pop(trade_id, None)

        if fill_price is None and res.get('price') is not None:
            fill_price = float(res['price'])
            fill_source = 'order_response'
            method_used = 'market_fallback'

        success = fill_price is not None
        _exec_tracker.record(symbol, is_buy, level_price, fill_price,
                             fill_source, latency_ms, method_used or 'market_fallback',
                             success, error=None if success else RuntimeError('No fill'))
        return {'success': success, 'trade_id': trade_id,
                'fill_price': fill_price, 'fill_source': fill_source,
                'latency_ms': latency_ms, 'method_used': method_used or 'market_fallback',
                'error': None if success else RuntimeError('Market fallback produced no fill'),
                'ioc_fail_reason': ioc_fail_reason}

    except Exception as e:
        latency_ms = round((time.monotonic() - t_start) * 1000)
        _exec_tracker.record(symbol, is_buy, level_price, None,
                             None, latency_ms, 'market_fallback', False, error=e)
        return {'success': False, 'trade_id': None,
                'fill_price': None, 'fill_source': None,
                'latency_ms': latency_ms, 'method_used': None,
                'error': e, 'ioc_fail_reason': ioc_fail_reason}


# ── Trade Entry ──
async def _gann_open_trade(symbol: str, is_buy: bool, level: dict, candles: list,
                            reason: str, tf: str, initial_px: float = None,
                            detect_time: datetime = None, t1_signal_ts: float = None,
                            feed_source: str = None, feed_age_ms: float = None,
                            trigger_type: str = None, combo_key: str = None) -> None:
    global _consecutive_real_order_failures
    sym_state = bot_state['symbol_state'][symbol]

    if not await is_trading_allowed():
        if bot_state.get('connection_state', CONN_RUNNING) != CONN_RUNNING:
            c_log(f"Skipped entry [{symbol} {tf}]: connection_state={bot_state.get('connection_state')}")
        else:
            c_log(f"Skipped entry [{symbol} {tf}]: inside restricted DAM trading window")
        return

    try:
        _lk = combo_key or level['key']
        is_real = sym_state.get('auto_trade', False)

        fresh_px, fresh_feed_source, fresh_feed_age_ms = initial_px, 'ws', feed_age_ms
        if is_real:
            fresh_px, fresh_feed_source, fresh_feed_age_ms = await _lq_price_with_fallback(symbol)
        margin = sym_state['gann_touch_margin_pts'] * SYMBOL_INFO[symbol]['pip_value']
        from market_data import _QUOTE_STALE_SECONDS

        if fresh_px is None:
            bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
            from telegram_ui import send_tg_msg
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم رفض الصفقة — تغذية السعر غير محدثة (أكبر من {_QUOTE_STALE_SECONDS}ث).\n"
                f"السبب: تجنب استدعاء OANDA REST البطيء ({50}-{150}ms) الذي يفسد السكالبينج."
            )
            return
        if abs(fresh_px - level['price']) > margin:
            bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
            from telegram_ui import send_tg_msg
            drift = abs(fresh_px - initial_px) if (fresh_px is not None and initial_px is not None) else None
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم تجاهل الفريم — السعر ابتعد عن المستوى أثناء التنفيذ "
                f"({'لا يمكن التأكد من السعر الحالي' if fresh_px is None else f'{fresh_px:.2f}'}) ولم يعد لمساً حقيقياً."
            )
            return

        price = fresh_px
        tp, sl = _gann_calc_tpsl(symbol, price, is_buy, candles, tf=tf)

        if is_buy and (price >= tp or price <= sl):
            bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
            from telegram_ui import send_tg_msg
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم إلغاء الأمر قبل الإرسال — السعر الحالي ({price:.2f}) تجاوز فعلياً "
                f"مستوى TP/SL المحسوب (TP:{tp} SL:{sl})."
            )
            return
        if not is_buy and (price <= tp or price >= sl):
            bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
            from telegram_ui import send_tg_msg
            await send_tg_msg(
                f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                f"المستوى: {level['price']:.2f}\n"
                f"تم إلغاء الأمر قبل الإرسال — السعر الحالي ({price:.2f}) تجاوز فعلياً "
                f"مستوى TP/SL المحسوب (TP:{tp} SL:{sl})."
            )
            return

        lot = sym_state['lot_size']
        tp_pts = _gann_tf_tp(symbol, tf); sl_pts = _gann_tf_sl(symbol, tf)
        tpsl_lbl = (f"ATR({sym_state['gann_atr_period']})×{sym_state['gann_atr_sl_mult']}/{sym_state['gann_atr_tp_mult']}\n"
                    if sym_state['gann_tpsl_mode'] == 'atr' else f"SL:{sl_pts}p TP:{tp_pts}p")
        be_lbl = " | 🛡️ BE Active" if sym_state['break_even_enabled'] else ""

        is_real = sym_state.get('auto_trade', False)
        trade_id = f"sim_{int(datetime.now().timestamp())}_{tf}"
        real_msg = ""
        execution_failed = False
        real_fill_price = None
        fill_price_source = 'simulated'
        exec_result = None

        if is_real:
            try:
                get_execution_client()
            except RuntimeError:
                real_msg = "\n⚠️ لا يوجد اتصال MT5 صالح (DWXConnect) — لم يتم فتح أي صفقة."
                is_real = False
                execution_failed = True
            else:
                max_slippage_points = int(bot_state.get('prot_max_slippage_points', 5))
                exec_result = await _execute_smart_order(
                    symbol, is_buy, lot, level['price'], sl, tp, t1_signal_ts, max_slippage_points)

                if exec_result['success']:
                    real_fill_price = exec_result['fill_price']
                    fill_price_source = exec_result['fill_source'] or 'simulated'
                    trade_id = exec_result['trade_id'] or trade_id
                    from telegram_ui import send_tg_msg
                    slippage_str = ''
                    if real_fill_price is not None and level['price'] is not None:
                        slip = abs(real_fill_price - level['price'])
                        slippage_str = f"\n📊 الانزلاق الفعلي عن المستوى: {slip:.2f} ({slip / SYMBOL_INFO[symbol]['pip_value']:.1f} نقطة)"
                    method_labels = {'limit': 'حدّي بسعر المستوى (Limit/IOC)',
                                     'market_fallback': 'سوقي بحماية الانزلاق (Market/FOK)'}
                    method_label = method_labels.get(exec_result['method_used'], 'غير معروف')
                    real_msg = (
                        f"\n🚀 <b>تم فتح الصفقة حقيقياً على حسابك!</b>"
                        + (f"\n⚠️ طريقة التنفيذ: {method_label}" if exec_result['method_used'] != 'limit' else '')
                        + f"\n⏱ وقت التنفيذ: {exec_result['latency_ms']}ms"
                        + f"\n📡 تغذية: {'WS (DWXConnect live)' if fresh_feed_source == 'ws' else 'OANDA REST (fallback)'}"
                        + f" | عمر السعر: {fresh_feed_age_ms if fresh_feed_age_ms is not None else 'n/a'}ms"
                        + slippage_str
                    )
                    async with _fail_counter_lock:
                        _consecutive_real_order_failures = 0
                else:
                    err = exec_result['error']
                    err_str = str(err) if err else 'Unknown error'
                    if any(code in err_str for code in ('REQUOTE', 'PRICE_CHANGED', 'OFF_QUOTES')):
                        real_msg = (
                            f"\n🛑 <b>تم رفض الصفقة لتجاوز حد الانزلاق السعري ({max_slippage_points} نقاط):</b> {err}"
                            f"\nلم يتم التنفيذ لحمايتك من دخول سيء."
                        )
                    else:
                        real_msg = (
                            f"\n❌ <b>فشل فتح الصفقة حقيقياً:</b> {err}"
                            f"\nلم يتم تتبعها كصفقة وهمية (لا يوجد تنفيذ فعلي)."
                        )
                    is_real = False
                    execution_failed = True
                    async with _fail_counter_lock:
                        _consecutive_real_order_failures += 1
                        failures = _consecutive_real_order_failures
                    if failures >= _REAL_ORDER_FAILURE_HALT_THRESHOLD:
                        await set_connection_state(
                            CONN_HALTED,
                            f"{failures} consecutive real order failures (last: {err}). Escalating to protect capital.")

        if execution_failed:
            bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
            from telegram_ui import send_tg_msg
            await send_tg_msg(f"<b>⏭️ [{symbol} - جان {tf}]</b>  {reason}\n"
                              f"المستوى: {level['price']:.2f}\n{real_msg}")
            return

        entry_final = real_fill_price if real_fill_price is not None else price
        exec_latency = exec_result.get('latency_ms') if is_real and exec_result else None
        exec_method = exec_result.get('method_used') if is_real and exec_result else None
        exec_ioc_fail = exec_result.get('ioc_fail_reason') if is_real and exec_result else None
        exec_slippage = round(abs(entry_final - level['price']), 5) if entry_final is not None and level['price'] is not None else None

        bot_state['symbol_state'][symbol]['gann_open_trades'][trade_id] = {
            'tf': tf, 'is_buy': is_buy, 'entry': entry_final, 'is_real': is_real,
            'sl': sl, 'tp': tp, 'opened_at': datetime.now(timezone.utc).isoformat(),
            'level_price': level['price'], 'feed_source': feed_source,
            'feed_age_ms': feed_age_ms, 'trigger_type': trigger_type,
            'exec_latency_ms': exec_latency, 'exec_method': exec_method,
            'exec_ioc_fail_reason': exec_ioc_fail, 'exec_slippage': exec_slippage,
        }
        bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
        await _debounced_persist_save()

        from telegram_ui import send_tg_msg
        entry_note = {'confirmed_position': ' (مؤكد من الوسيط)',
                      'order_response': ' (من استجابة الأمر)',
                      'simulated': ''}.get(fill_price_source, ' (تقديري)')
        slippage_line = ""
        if is_real and entry_final is not None:
            actual_slippage = abs(entry_final - level['price'])
            pv = SYMBOL_INFO[symbol]['pip_value']
            slippage_line = f"الانزلاق الفعلي عن المستوى: {actual_slippage:.2f} ({actual_slippage / pv:.1f} نقطة)\n"

        close_used = bot_state['symbol_state'][symbol].get('gann_close_used')
        close_label = f'{close_used:.5f}' if close_used is not None else '-'

        await send_tg_msg(
            f"<b>✅ {reason}</b>\n\n"
            f"المستوى: {level['price']:.2f}  |  الدخول: {entry_final:.2f}{entry_note}\n\n"
            f"TP: {tp}  SL: {sl}  |  {tpsl_lbl}{be_lbl}\n"
            f"{slippage_line}"
            f"إغلاق {bot_state.get('gann_anchor_tf', '1h').upper()}: {close_label}\n"
            f"{real_msg}"
        )
    except Exception as e:
        log_exception(f"_gann_open_trade [{symbol} {tf}]", e)
        bot_state['symbol_state'][symbol]['gann_level_status'][_lk] = 'used'
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"<b>❌ فشل تنفيذ الصفقة [{symbol} - جان {tf}]</b>\nالمستوى: {level['price']:.5f}\n{e}")


# ── Tick-Driven Trade Dispatch ──
async def _gann_tick_fire_check(symbol: str, live_px: float, feed_age_ms: float) -> None:
    if _tick_semaphore.locked():
        return
    async with _tick_semaphore:
        try:
            if bot_state.get('connection_state') != CONN_RUNNING:
                return
            if bot_state.get('live_daily_hit'):
                return
            cache = _gann_cache.get(symbol)
            if not cache:
                return
            sym_state = bot_state['symbol_state'][symbol]
            if not sym_state['gann_cycle_active'] or not sym_state['gann_levels']:
                return

            max_concurrent = max(1, int(bot_state.get('prot_max_concurrent_trades', 4)))
            open_count = sum(1 for v in sym_state['gann_open_trades'].values() if isinstance(v, dict))
            if open_count >= max_concurrent:
                return

            margin = cache['margin']; levels = cache['levels']; trend_up = cache['trend_up']
            entry_mode = sym_state['gann_entry_mode']
            exec_mode = bot_state.get('gann_execution_mode', 'instant')
            pv = SYMBOL_INFO[symbol]['pip_value']
            spike_limit = bot_state.get('gann_spike_limit_pts', 20) * pv
            flt_type = sym_state['trend_filter_type']; ttf = sym_state['trend_timeframe']
            detect_time = datetime.now(timezone.utc)

            for tf in cache['enabled_tfs']:
                if any(isinstance(v, dict) and v.get('tf') == tf for v in sym_state['gann_open_trades'].values()):
                    continue
                tf_data = cache['tf_data'].get(tf)
                if not tf_data:
                    continue
                candles = tf_data['candles']; closed_close = tf_data['closed_close']

                if entry_mode == 'touch_trend' and trend_up is None:
                    continue

                if exec_mode == 'all_concurrent':
                    channels = ['touch', 'close', 'hybrid']
                elif exec_mode == 'close':
                    channels = ['close']
                elif exec_mode == 'hybrid':
                    channels = ['hybrid']
                else:
                    channels = ['touch']

                for channel in channels:
                    for lv in levels:
                        k = lv['key']; dir_ = lv['dir']
                        base_combo = f"{k}_{tf}" if bot_state['prot_allow_multi_tf'] else k
                        combo_key = f"{base_combo}_{channel}" if exec_mode == 'all_concurrent' else base_combo
                        if sym_state['gann_level_status'].get(combo_key) == 'used':
                            continue
                        is_buy = (dir_ == 'dn')
                        if entry_mode == 'touch_trend':
                            if is_buy and not trend_up: continue
                            if not is_buy and trend_up: continue

                        q = live_quotes.get(symbol, {})
                        check_px = q.get('bid' if is_buy else 'ask') or live_px

                        if channel == 'close':
                            if abs(closed_close - lv['price']) > margin: continue
                        elif channel == 'hybrid':
                            if abs(check_px - lv['price']) > margin: continue
                            if abs(check_px - closed_close) > spike_limit: continue
                        else:
                            if abs(check_px - lv['price']) > margin: continue

                        sym_state['gann_level_status'][combo_key] = 'used'

                        if flt_type == 'vwap': flt_label = f"VWAP={sym_state['trend_vwap_period']}\n"
                        elif flt_type == 'ema': flt_label = f"EMA={sym_state['trend_ema_period']}\n"
                        else: flt_label = "VWAP+EMA"
                        trigger_lbl = {'touch': 'لمس مباشر ⚡', 'close': 'إغلاق شمعة ⏳', 'hybrid': 'تنفيذ هجين 🛡️'}[channel]
                        dir_word = 'BUY' if is_buy else 'SELL'
                        dir_emoji = '📈' if is_buy else '📉'
                        reason = f"{dir_word} {dir_emoji} [{symbol} - جان {tf}] {trigger_lbl} (مع {flt_label}_{ttf.upper()})"

                        t1_signal_ts = time.monotonic()
                        _safe_task(_gann_open_trade(
                            symbol, is_buy, lv, candles, reason=reason, tf=tf,
                            initial_px=live_px, detect_time=detect_time, t1_signal_ts=t1_signal_ts,
                            feed_source='ws', feed_age_ms=feed_age_ms, trigger_type=channel,
                            combo_key=combo_key,
                        ), f'trade_open_{symbol}_{tf}')
                        break
        except Exception as e:
            log_exception(f"_gann_tick_fire_check [{symbol}]", e)


# ── Trade Closure ──
async def _close_metaapi_trade(symbol: str, tid: str, sym_state: dict) -> bool:
    try:
        client = get_execution_client()
    except RuntimeError:
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"🛑 <b>تعذّر إغلاق صفقة {symbol} ({tid}):</b> لا يوجد اتصال MT5 (DWXConnect).")
        return False
    try:
        broker_symbol = _resolve_broker_symbol(symbol)
        await client.close_position(broker_symbol, tid)
        for _ in range(25):
            positions = await client.get_positions()
            if not any(str(p.get('id') or p.get('ticket') or p.get('position') or '') == str(tid)
                       for p in positions):
                from telegram_ui import send_tg_msg
                await send_tg_msg(f"✅ <b>تم إغلاق صفقة {symbol} (حقيقية) بنجاح لحماية الحساب!</b>")
                if tid in sym_state['gann_open_trades']:
                    del sym_state['gann_open_trades'][tid]
                    await save_bot_persistence()
                return True
            await asyncio.sleep(1.0)
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"⚠️ <b>لم يتم تأكيد إغلاق {symbol} ({tid}) خلال المهلة.</b> يرجى التحقق يدوياً من الحساب.")
        return False
    except Exception as e:
        log_exception(f"_close_metaapi_trade [{symbol}/{tid}]", e)
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"⚠️ <b>فشل الإغلاق الآلي:</b> صفقة {symbol} (خطأ: {e})\nيرجى التحقق يدوياً من الحساب.")
        return False


_EMERGENCY_CLOSE_POLL_BUDGET_SECONDS = 25


async def _close_metaapi_trades_batch(closures: list) -> None:
    def _trade_detail_line(tr: dict) -> str:
        pl = tr.get('last_known_pl', 0.0)
        px = tr.get('last_known_px', tr.get('entry'))
        outcome_lbl = 'ربح ✅' if pl >= 0 else 'خسارة ❌'
        return (f"[جان {tr.get('tf')}] {'BUY 📈' if tr.get('is_buy') else 'SELL 📉'}\n"
                f"الدخول: {tr.get('entry')}  |  آخر سعر معروف: {px}\n"
                f"TP: {tr.get('tp')}  SL: {tr.get('sl')}\n"
                f"النتيجة: {outcome_lbl} ({pl}$)")

    if not closures:
        return
    try:
        client = get_execution_client()
    except RuntimeError:
        from telegram_ui import send_tg_msg
        detail = "\n\n".join(f"{symbol}: {_trade_detail_line(tr)}" for symbol, _, _, tr in closures)
        await send_tg_msg(f"🛑 <b>تعذّر إغلاق {len(closures)} صفقة:</b> لا يوجد اتصال MT5 (DWXConnect).\n\n{detail}")
        return

    pending = {}
    close_errors = []
    for symbol, tid, sym_state, tr in closures:
        try:
            broker_symbol = _resolve_broker_symbol(symbol)
            await client.close_position(broker_symbol, tid)
            pending[str(tid)] = (symbol, sym_state, tr)
        except Exception as e:
            log_exception(f"_close_metaapi_trades_batch close_position [{symbol}/{tid}]", e)
            close_errors.append(f"{symbol} ({tid}): {e}")
    if close_errors:
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"⚠️ <b>فشل إرسال {len(close_errors)}/{len(closures)} أمر إغلاق</b>\n" + "\n".join(close_errors)[:3500])

    if not pending:
        return

    for _ in range(_EMERGENCY_CLOSE_POLL_BUDGET_SECONDS):
        if not pending:
            break
        try:
            positions = await client.get_positions()
            if not isinstance(positions, list):
                raise TypeError(f"get_positions() returned {type(positions).__name__}, expected list")
        except Exception as e:
            log_exception("_close_metaapi_trades_batch get_positions", e)
            await asyncio.sleep(1.0)
            continue

        still_open_ids = {str(p.get('id') or p.get('ticket') or p.get('position') or '') for p in positions}
        for tid in list(pending.keys()):
            if tid not in still_open_ids:
                symbol, sym_state, tr = pending.pop(tid)
                from telegram_ui import send_tg_msg
                await send_tg_msg(f"✅ <b>تم إغلاق صفقة {symbol} (حقيقية) بنجاح لحماية الحساب!</b>\n\n{_trade_detail_line(tr)}")
                pl = tr.get('last_known_pl', 0.0)
                px = tr.get('last_known_px', tr.get('entry'))
                await _record_closed_trade_history(
                    symbol, tid, tr, exit_px=px, pnl=pl,
                    outcome_label=('WIN' if pl > 0 else 'LOSS' if pl < 0 else 'BREAK_EVEN'),
                    close_reason='daily_capital_protection_forced_close', pnl_confirmed=False)
                if tid in sym_state['gann_open_trades']:
                    del sym_state['gann_open_trades'][tid]
                    await save_bot_persistence()

        if pending:
            await asyncio.sleep(1.0)

    for tid, (symbol, sym_state, tr) in pending.items():
        from telegram_ui import send_tg_msg
        await send_tg_msg(f"⚠️ <b>لم يتم تأكيد إغلاق {symbol} ({tid}) خلال المهلة.</b>\n\n{_trade_detail_line(tr)}")

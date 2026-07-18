"""
main.py — Entry point and task orchestration.

v9.5 — Refactored from monolithic Script.py into modular architecture:
  state.py         — global state, config, persistence, connection FSM
  market_data.py   — OANDA REST, MetaApi WebSocket, live quotes
  strategy.py      — Gann levels, trend filters, ATR, TP/SL
  execution.py     — order execution, trade management, fill monitor
  backtest.py      — backtest & Live-Twin engines
  telegram_ui.py   — Telegram keyboards, messages, callback dispatch
  gann_monitor.py  — live scanner, cycle manager, diagnostics
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

from state import (
    bot_state, get_http, c_log, log_exception, _safe_task,
    CONN_RUNNING, CONN_READ_ONLY, CONN_HALTED,
)
from market_data import init_metaapi
from gann_monitor import (
    gann_monitor_scanner, gann_cycle_manager, global_ledger_reconciliation,
)
from telegram_ui import (
    telegram_polling_loop, telegram_watchdog,
)


# ── Supervised task wrapper ──
_poll_task: asyncio.Task | None = None


async def supervised(coro_fn, *args, label: str = '') -> None:
    """Wrap a long-running coroutine with crash-restart + backoff + escalation."""
    global _poll_task
    _MAX_CONSECUTIVE_CRASHES = 10
    _MAX_BACKOFF_SECONDS = 120
    crash_count = 0
    while True:
        try:
            task = asyncio.current_task()
            if label == 'tg_polling':
                _poll_task = task
            await coro_fn(*args)
            crash_count = 0
        except asyncio.CancelledError:
            await asyncio.sleep(2)
        except Exception as e:
            crash_count += 1
            log_exception(f'supervised task "{label}" crashed (crash {crash_count}/{_MAX_CONSECUTIVE_CRASHES})', e)
            if crash_count <= 3 or crash_count % 5 == 0:
                try:
                    from telegram_ui import send_tg_msg
                    await send_tg_msg(
                        f"⚠️ <b>تعطل متكرر: {label}</b>\n"
                        f"الانهيار رقم {crash_count} (الحد الأقصى: {_MAX_CONSECUTIVE_CRASHES})\n"
                        f"الخطأ: {e}\n\n"
                        f"{'🛑 سيتم إيقاف إعادة المحاولة إذا استمر.' if crash_count >= _MAX_CONSECUTIVE_CRASHES - 3 else '⚙️ جاري إعادة المحاولة تلقائياً...'}"
                    )
                except Exception:
                    pass
            if crash_count >= _MAX_CONSECUTIVE_CRASHES:
                c_log(f'FATAL: supervised task "{label}" exceeded {_MAX_CONSECUTIVE_CRASHES} consecutive crashes.')
                try:
                    from telegram_ui import send_tg_msg
                    await send_tg_msg(
                        f"🛑 <b>توقف تام: {label}</b>\n"
                        f"تعطل {_MAX_CONSECUTIVE_CRASHES} مرات متتالية. تم إيقاف إعادة التشغيل التلقائي.\n"
                        f"يجب إعادة تشغيل البوت يدوياً."
                    )
                except Exception:
                    pass
                return
            backoff = min(2 ** min(crash_count, 6), _MAX_BACKOFF_SECONDS)
            await asyncio.sleep(backoff)


# ── Web Server ──
async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="Bot is running smoothly!")


# ── Main Entry Point ──
async def main() -> None:
    get_http()
    await init_metaapi()

    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 10000))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    c_log(f'Web server started on port {port}')

    bot_state['last_poll_ok'] = datetime.now(timezone.utc).timestamp()

    tasks = [
        asyncio.create_task(supervised(telegram_polling_loop, label='tg_polling')),
        asyncio.create_task(supervised(telegram_watchdog, label='tg_watchdog')),
        asyncio.create_task(supervised(gann_monitor_scanner, label='gann_monitor')),
        asyncio.create_task(supervised(gann_cycle_manager, label='gann_cycle')),
        asyncio.create_task(supervised(global_ledger_reconciliation, label='global_reconciliation')),
    ]

    c_log('Gold Scalper Bot v9.5 (Modular Architecture) started successfully.')

    try:
        await asyncio.gather(*tasks)
    finally:
        from state import get_http as gh
        http = gh()
        if http and not http.closed:
            await http.close()


if __name__ == '__main__':
    asyncio.run(main())

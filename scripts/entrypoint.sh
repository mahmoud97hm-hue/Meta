#!/usr/bin/env bash
# scripts/entrypoint.sh — mt5-bridge unified startup
#
# 1. Start redis-server (state management for the bridge).
# 2. Start a headless Xvfb virtual display (MT5 requires a display).
# 3. Launch the MT5 terminal under Wine (portable mode).
# 4. Launch the FastAPI + Socket.IO bridge (backend/api/main.py) under
#    Wine's Windows Python so the MetaTrader5 lib can attach to the terminal.

set -e

BRIDGE_PORT="${BRIDGE_PORT:-3000}"
BRIDGE_HOST="${BRIDGE_HOST:-0.0.0.0}"
MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-/root/.wine-mt5-terminal1/drive_c/MetaTrader5/terminal64.exe}"
MT5_WINE_PREFIX="${MT5_WINE_PREFIX:-/root/.wine-mt5-terminal1}"
DISPLAY="${DISPLAY:-:99}"

echo "[entrypoint] starting redis-server..."
redis-server --daemonize yes --save "" --appendonly no
sleep 1

echo "[entrypoint] starting headless Xvfb on ${DISPLAY}..."
Xvfb ${DISPLAY} -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!
sleep 2

# Convert the Linux MT5 path to a Wine C: path, e.g.
# /root/.wine-mt5-terminal1/drive_c/MetaTrader5/terminal64.exe
#   -> C:\MetaTrader5\terminal64.exe
MT5_WIN_PATH="C:\\MetaTrader5\\terminal64.exe"

echo "[entrypoint] launching MT5 terminal under Wine (portable)..."
export WINEPREFIX="${MT5_WINE_PREFIX}" WINEARCH=win64 WINEDEBUG=-all DISPLAY
wine64 "${MT5_WIN_PATH}" /portable &
MT5_PID=$!

echo "[entrypoint] launching FastAPI + Socket.IO bridge under Wine Python..."
WINEPREFIX="${MT5_WINE_PREFIX}" WINEARCH=win64 WINEDEBUG=-all \
PYTHONHASHSEED=0 \
BRIDGE_HOST="${BRIDGE_HOST}" BRIDGE_PORT="${BRIDGE_PORT}" \
MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH}" \
MT5_LOGIN="${MT5_LOGIN}" MT5_PASSWORD="${MT5_PASSWORD}" MT5_SERVER="${MT5_SERVER}" \
BRIDGE_API_KEY="${BRIDGE_API_KEY}" \
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}" \
  wine64 C:\\Python310\\python.exe /opt/mt5bridge/backend/api/main.py &
BRIDGE_PID=$!

cleanup() {
  echo "[entrypoint] shutting down..."
  kill -TERM "${BRIDGE_PID}" 2>/dev/null || true
  kill -TERM "${MT5_PID}" 2>/dev/null || true
  kill -TERM "${XVFB_PID}" 2>/dev/null || true
  redis-cli shutdown nosave 2>/dev/null || true
  exit 0
}
trap cleanup SIGTERM SIGINT

wait

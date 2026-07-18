#!/usr/bin/env bash
# entrypoint.sh — headless launcher for the MT5 socket.io bridge
#
# No VNC. No window manager. Xvfb provides the only (virtual) display MT5
# requires. The Python socket.io server (server.py) drives the auto-installed
# MT5 terminal via the MetaTrader5 Python lib. A lightweight memory watchdog
# recycles the container's MT5 memory growth, keeping us under Railway's limit.

set -e

BRIDGE_PORT="${BRIDGE_PORT:-3000}"
BRIDGE_HOST="${BRIDGE_HOST:-0.0.0.0}"
MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-/opt/mt5/terminal/terminal64.exe}"
MEM_LIMIT_MB="${MEM_LIMIT_MB:-1800}"

echo "[entrypoint] starting headless Xvfb virtual display on ${DISPLAY}"
Xvfb ${DISPLAY} -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!

# Wait for the virtual display to come up
sleep 2

# The MetaTrader5 Python lib actually launches the terminal process; we track
# its memory via the wine service host. Keep a handle for the watchdog.
echo "[entrypoint] launching socket.io bridge server under Wine Python (drives MT5 via Windows MetaTrader5 lib)"
launch_server() {
  # Wine's embedded Python can't reach Windows entropy sources in a headless
  # container, so hash randomization init fails. Pinning the seed avoids that.
  PYTHONHASHSEED=0 \
  BRIDGE_HOST="${BRIDGE_HOST}" BRIDGE_PORT="${BRIDGE_PORT}" \
    MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH}" \
    MT5_LOGIN="${MT5_LOGIN}" MT5_PASSWORD="${MT5_PASSWORD}" MT5_SERVER="${MT5_SERVER}" \
    BRIDGE_API_KEY="${BRIDGE_API_KEY}" \
    WINEPREFIX=/root/.wine WINEDEBUG=-all \
    wine C:\\Python310\\python.exe /opt/mt5bridge/server.py &
  echo $!
}

SERVER_PID=$(launch_server)

# ---- memory watchdog: keep Railway happy ----
# Monitor the python bridge (which hosts the MT5 terminal) RSS and restart
# the whole service cleanly if it exceeds the ceiling.
watchdog() {
  while true; do
    sleep 30
    [ -z "$SERVER_PID" ] && continue
    RSS_KB=$(awk '{print $2}' /proc/${SERVER_PID}/statm 2>/dev/null || echo 0)
    RSS_MB=$(( RSS_KB / 1024 ))
    if [ "$RSS_MB" -gt "$MEM_LIMIT_MB" ]; then
      echo "[watchdog] bridge RSS=${RSS_MB}MB exceeds ${MEM_LIMIT_MB}MB — recycling"
      kill -TERM "$SERVER_PID" 2>/dev/null || true
      sleep 5
      SERVER_PID=$(launch_server)
    fi
  done
}
watchdog &

# ---- graceful shutdown ----
cleanup() {
  echo "[entrypoint] shutting down..."
  kill -TERM "$SERVER_PID" 2>/dev/null || true
  kill -TERM "$XVFB_PID" 2>/dev/null || true
  exit 0
}
trap cleanup SIGTERM SIGINT

wait

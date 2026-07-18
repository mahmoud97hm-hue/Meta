# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64). Optimizations vs upstream FortesenseLabs:
#   * NO VNC server, NO Openbox WM, NO x11vnc/websockify -> saves ~150-250MB RAM
#   * Xvfb virtual display only (MT5 needs a display); no network VNC listener
#   * MT5 installed AUTOMATICALLY at build time (no Windows machine needed):
#       - official MetaQuotes installer fetched via curl (license-compliant;
#         we never redistribute MetaQuotes binaries)
#       - installed silently under Wine into /opt/mt5/terminal
#   * Broker config (server/datacenter) is fetched at runtime by the
#     MetaTrader5 Python lib via mt5.initialize(server="<BROKER-SERVER>")
#   * Single Python socket.io server + MT5; minimal footprint.

FROM alpine:3.20

ENV LANG=C.UTF-8 \
    DISPLAY=:99 \
    WINEPREFIX=/root/.wine \
    WINEDEBUG=-all \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    MT5_TERMINAL_PATH=/opt/mt5/terminal/terminal64.exe \
    MT5_DIR=/opt/mt5/terminal

# ---- system deps: xvfb (virtual display) + wine + python + download tools ----
RUN apk add --no-cache \
        bash \
        coreutils \
        curl \
        ca-certificates \
        python3 \
        py3-pip \
        xvfb \
        mesa-dri-gallium \
        font-dejavu \
        && apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
        wine \
    && pip3 install --no-cache-dir --break-system-packages \
        python-socketio==5.11.4 \
        aiohttp \
    && rm -rf /var/cache/apk/*

# ===========================================================================
# OPTIONAL winetricks bootstrap layer (COMMENTED OUT BY DEFAULT — keep the
# image lean for Railway RAM limits). Enable ONLY if the silent mt5setup.exe
# install hangs or crashes due to missing fonts / .NET (mono) / VC++ runtimes.
#
# To enable: uncomment the two RUN blocks below. They install winetricks +
# corefonts + wine-mono + common VC++ runtimes, then prime the Wine prefix
# with `wineboot` so the first real MT5 launch doesn't trigger interactive
# first-run setup dialogs.
# ---------------------------------------------------------------------------
# RUN apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
#         winetricks \
#     && apk add --no-cache \
#         ttf-dejavu \
#         ttf-liberation \
#         terminus-font \
#         && rm -rf /var/cache/apk/*
#
# RUN WINEPREFIX=/root/.wine WINEDEBUG=-all winetricks -q \
#         corefonts vcrun2015 winhttp \
#     && WINEPREFIX=/root/.wine WINEDEBUG=-all wineboot --init \
#     && echo "[build] winetricks bootstrap complete"
# ===========================================================================

WORKDIR /opt/mt5bridge

# ---- Auto-install the MT5 terminal at BUILD TIME (no Windows host required) ----
# 1. Fetch the official MetaQuotes MT5 installer from the public CDN.
# 2. Run it silently under Wine with /portable so it extracts the terminal
#    into MT5_DIR without a GUI and without prompting.
# 3. The MetaTrader5 Python lib later calls mt5.initialize(server=...) to
#    download the broker-specific datacenter config on first login.
RUN mkdir -p /opt/mt5/install /opt/mt5/terminal \
    && curl -fsSL -o /opt/mt5/install/mt5setup.exe \
        "https://download.mql5.com/cdn/web/ilhamov.fl/installer/mt5setup.exe" \
    || curl -fsSL -o /opt/mt5/install/mt5setup.exe \
        "https://www.metatrader5.com/en/download/file/2024/mt5setup.exe" \
    && echo "[build] MT5 installer downloaded" \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 /opt/mt5/install/mt5setup.exe \
        /portable /silent /skipupdate /dir="C:\\terminal" \
    && echo "[build] MT5 installed headlessly" \
    && rm -rf /opt/mt5/install

# ---- Bridge server + deps ----
COPY server.py     /opt/mt5bridge/server.py
COPY requirements-bridge.txt /opt/mt5bridge/requirements-bridge.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/mt5bridge/requirements-bridge.txt || true

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

# Memory watchdog: if RSS exceeds 1.8G the entrypoint restarts MT5 cleanly.
ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

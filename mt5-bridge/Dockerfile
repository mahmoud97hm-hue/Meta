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
        xvfb \
        mesa-dri-gallium \
        font-dejavu \
        && apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
        wine \
    && rm -rf /var/cache/apk/*

# ---------------------------------------------------------------------------
# Install a WINDOWS Python 3.11 inside the Wine prefix.
# The MetaTrader5 PyPI package is Windows-only (it loads terminal DLLs), so
# server.py MUST run under Wine's Windows Python, NOT Alpine's native python3.
#
# We use the FULL Windows installer (not the embeddable zip) because the
# embeddable build links a newer UCRT than Wine ships: it calls
# ucrtbase.dll.crealf, which Wine's bundled ucrtbase lacks -> crash. The full
# installer deploys its own matching UCRT into the prefix, avoiding that.
# The full installer also bundles pip, so no get-pip.py / _pth hack needed.
# ---------------------------------------------------------------------------
ENV WINEPYTHON=C:\\Python311

RUN mkdir -p /opt/winpy /opt/mt5/install \
    && curl -fsSL -o /opt/winpy/python311.exe \
        "https://www.python.org/ftp/python/3.11.4/python-3.11.4-amd64.exe" \
    && echo "[build] Windows Python installer downloaded" \
    && echo "[build] installing Windows Python 3.11 into C:\\Python311 via Wine..." \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 /opt/winpy/python311.exe \
        /quiet TargetDir=C:\\Python311 PrependPath=0 Include_pip=1 Include_launcher=0 \
    && echo "[build] installing MetaTrader5 + socketio + uvicorn into Wine Python..." \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 \
        C:\\Python311\\python.exe -m pip install --no-cache-dir \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] Wine Python + MetaTrader5 ready" \
    && rm -rf /opt/winpy /opt/mt5/install

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
RUN mkdir -p /opt/mt5/install /opt/mt5/terminal && curl -fsSL -o /opt/mt5/install/mt5setup.exe "https://download.metatrader.com/cdn/web/fundingpips.corp.2/mt5/fundingpips25setup.exe" && echo "[build] MT5 installer downloaded" && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 /opt/mt5/install/mt5setup.exe /portable /silent /skipupdate /dir="C:\terminal" && echo "[build] MT5 installed headlessly" && rm -rf /opt/mt5/install

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

# Memory watchdog: if RSS exceeds 1.8G the entrypoint restarts MT5 cleanly.
ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

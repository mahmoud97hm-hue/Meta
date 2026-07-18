# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64).
#   * NO VNC, NO WM — Xvfb virtual display only.
#   * MT5 installed AUTOMATICALLY at build time under Wine into C:\terminal.
#   * A WINDOWS Python 3.10.11 (embeddable) runs INSIDE Wine; the Windows-only
#     MetaTrader5 package is pip-installed into it. server.py runs via
#     `wine64 C:\Python310\python.exe`.
#   * Python 3.10 links the UCRT that Wine already ships, so NO winetricks /
#     ucrtbase patching is required (3.11 needed crealf which Wine lacked).

FROM alpine:3.20

ENV LANG=C.UTF-8 \
    DISPLAY=:99 \
    WINEPREFIX=/root/.wine \
    WINEDEBUG=-all \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    MT5_TERMINAL_PATH=/opt/mt5/terminal/terminal64.exe \
    MT5_DIR=/opt/mt5/terminal

# ---- system deps: xvfb (virtual display) + wine + tools ----
RUN apk add --no-cache \
        bash \
        coreutils \
        curl \
        ca-certificates \
        unzip \
        xvfb \
        mesa-dri-gallium \
        font-dejavu \
        && apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/v3.20/community \
        wine \
    && rm -rf /var/cache/apk/*

# ---------------------------------------------------------------------------
# Install a WINDOWS Python 3.10.11 inside the Wine prefix (HEADLESS).
# The MetaTrader5 PyPI package is Windows-only (it loads terminal DLLs), so
# server.py MUST run under Wine's Windows Python, NOT Alpine's native python3.
#
# We use the EMBEDDABLE zip (no GUI installer -> no ShellExecute/display
# issues under headless Wine). Python 3.10 links the UCRT Wine already
# provides, so no winetricks/ucrtbase patching is needed. We enable
# site-packages via python310._pth and bootstrap pip + the required packages.
# ---------------------------------------------------------------------------
ENV WINEPYTHON=C:\\Python310

RUN mkdir -p /opt/winpy /opt/mt5/install \
    && curl -fsSL -o /opt/winpy/python310.zip \
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip" \
    && echo "[build] Windows Python 3.10.11 (embeddable) downloaded" \
    && mkdir -p /root/.wine/drive_c/Python310 \
    && unzip -o /opt/winpy/python310.zip -d /root/.wine/drive_c/Python310/ \
    && echo "[build] enabling site-packages in embeddable Python (uncomment import site)" \
    && sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python310/python310._pth \
    && curl -fsSL -o /root/.wine/drive_c/Python310/get-pip.py \
        "https://bootstrap.pypa.io/get-pip.py" \
    && echo "[build] bootstrapping pip in Wine Python..." \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe C:\\Python310\\get-pip.py --no-warn-script-location \
    && echo "[build] installing MetaTrader5 + socketio + uvicorn into Wine Python..." \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe -m pip install --no-cache-dir \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] Wine Python + MetaTrader5 ready" \
    && rm -rf /opt/winpy /opt/mt5/install

WORKDIR /opt/mt5bridge

# ---- Auto-install the MT5 terminal at BUILD TIME (no Windows host required) ----
RUN mkdir -p /opt/mt5/install && curl -fsSL -o /opt/mt5/install/mt5setup.exe "https://download.metatrader.com/cdn/web/fundingpips.corp.2/mt5/fundingpips25setup.exe" && echo "[build] MT5 installer downloaded" && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 /opt/mt5/install/mt5setup.exe /portable /silent /skipupdate /dir="C:\terminal" && echo "[build] MT5 installed headlessly" && rm -rf /opt/mt5/install

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

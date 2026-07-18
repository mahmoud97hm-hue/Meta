# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64).
#   * NO VNC, NO WM — Xvfb virtual display only.
#   * Base: Ubuntu 22.04 — provides a modern Wine with working UCRT (no manual
#     DLL injection needed). Python 3.10.11 embeddable runs inside Wine.
#   * MT5 installed AUTOMATICALLY at build time under Wine into C:\terminal.
#   * server.py runs via `wine64 C:\Python310\python.exe`.

FROM ubuntu:22.04

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    WINEPREFIX=/root/.wine \
    WINEDEBUG=-all \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    MT5_TERMINAL_PATH=/opt/mt5/terminal/terminal64.exe \
    MT5_DIR=/opt/mt5/terminal \
    PYTHONHASHSEED=0

# ---- system deps: xvfb, wine, python3 (native for wheel fetch), tools ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        curl \
        ca-certificates \
        unzip \
        xvfb \
        python3 \
        python3-pip \
        fontconfig \
        fonts-dejavu-core \
        libgl1-mesa-glx \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
        libxi6 \
        libxrandr2 \
        libxxf86vm1 \
    && apt-get install -y --no-install-recommends \
        wine64 \
        wine32 \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Install a WINDOWS Python 3.10.11 inside the Wine prefix (HEADLESS).
# The MetaTrader5 PyPI package is Windows-only (it loads terminal DLLs), so
# server.py MUST run under Wine's Windows Python, NOT the native python3.
#
# Ubuntu's Wine includes a working UCRT, so no manual ucrtbase.dll injection
# is required. We use the EMBEDDABLE zip (no GUI installer -> no ShellExecute/
# display issues). We enable site-packages via python310._pth, fetch Windows
# wheels with native pip3, then offline-install into Wine Python.
# ---------------------------------------------------------------------------
ENV WINEPYTHON=C:\\Python310

RUN mkdir -p /opt/winpy /opt/mt5/install /opt/whl \
    && curl -fsSL -o /opt/winpy/python310.zip \
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip" \
    && echo "[build] Windows Python 3.10.11 (embeddable) downloaded" \
    && mkdir -p /root/.wine/drive_c/Python310 \
    && unzip -o /opt/winpy/python310.zip -d /root/.wine/drive_c/Python310/ \
    && echo "[build] enabling site-packages in embeddable Python (uncomment import site)" \
    && sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python310/python310._pth \
    && echo "[build] downloading WINDOWS wheels with native pip3 (no Wine networking)..." \
    && pip3 install --no-cache-dir --break-system-packages --target=/opt/whl pip setuptools wheel \
    && pip3 download --no-cache-dir --dest=/opt/whl \
        --platform win_amd64 --python-version 310 --abi cp310 --only-binary=:all: \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] offline-installing packages into Wine Python (no network under Wine)..." \
    && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe -m pip install --no-index --find-links=Z:\\opt\\whl \
        pip setuptools wheel \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] Wine Python + MetaTrader5 ready" \
    && rm -rf /opt/winpy /opt/mt5/install /opt/whl

WORKDIR /opt/mt5bridge

# ---- Auto-install the MT5 terminal at BUILD TIME (no Windows host required) ----
RUN mkdir -p /opt/mt5/install && curl -fsSL -o /opt/mt5/install/mt5setup.exe "https://download.metatrader.com/cdn/web/fundingpips.corp.2/mt5/fundingpips25setup.exe" && echo "[build] MT5 installer downloaded" && WINEPREFIX=/root/.wine WINEDEBUG=-all wine64 /opt/mt5/install/mt5setup.exe /portable /silent /skipupdate /dir="C:\terminal" && echo "[build] MT5 installed headlessly" && rm -rf /opt/mt5/install

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]
# Dockerfile — mt5-bridge (unified, automated)
#
# Railway-ready, headless MT5 bridge:
#   * Ubuntu 22.04 + native wine64 (pure 64-bit, no i386/wine32).
#   * Redis for bridge state management.
#   * MT5 terminal downloaded from MediaFire into the Wine prefix
#     /root/.wine-mt5-terminal1/drive_c/MetaTrader5/.
#   * Wine-embedded Python 3.10.11 + MetaTrader5 lib runs the FastAPI +
#     Socket.IO bridge (backend/api/main.py).
#   * Entrypoint: redis -> Xvfb -> MT5(under wine) -> bridge(under wine python).

FROM ubuntu:22.04

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    WINEARCH=win64 \
    WINEPREFIX=/root/.wine-mt5-terminal1 \
    WINEDEBUG=-all \
    MT5_TERMINAL_PATH=/root/.wine-mt5-terminal1/drive_c/MetaTrader5/terminal64.exe \
    MT5_WINE_PREFIX=/root/.wine-mt5-terminal1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    PATH="/usr/lib/wine:/opt/wine-stable/bin:/usr/bin:/usr/local/bin:${PATH}"

# ---- system deps: wine64 + redis + python3 (host) + tools ----
# Note: the bridge itself runs under Wine's embedded Python, but we keep
# native python3/pip for the offline wheel download step.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates unzip xvfb \
        redis-server python3 python3-pip fontconfig fonts-dejavu-core \
        libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 libxi6 \
        libxrandr2 libxxf86vm1 wine64 wget \
    && rm -rf /var/lib/apt/lists/*

# ---- locate Wine binaries on PATH before using them ----
RUN echo "[build] locating Wine binaries..." \
    && WINE_BIN=$(dirname $(find /usr/lib /opt/wine-stable -name 'wine64' -type f 2>/dev/null | head -1)) \
    && echo "[build] wine bin dir: ${WINE_BIN}" \
    && export PATH="${WINE_BIN}:/usr/lib/wine:/opt/wine-stable/bin:$PATH" \
    && which wine64 wineboot wineserver \
    && echo "[build] initializing 64-bit Wine prefix..." \
    && wineboot --init 2>&1 || true \
    && wineserver -w \
    && wine64 --version

# ---- Wine-embedded Python 3.10.11 + MetaTrader5 lib ----
ENV WINEPYTHON=C:\\Python310
RUN mkdir -p /opt/winpy /opt/whl \
    && curl -fsSL -o /opt/winpy/python310.zip \
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip" \
    && mkdir -p /root/.wine-mt5-terminal1/drive_c/Python310 \
    && unzip -o /opt/winpy/python310.zip -d /root/.wine-mt5-terminal1/drive_c/Python310/ \
    && sed -i 's/^#import site/import site/' /root/.wine-mt5-terminal1/drive_c/Python310/python310._pth \
    && pip3 download --no-cache-dir --dest=/opt/whl \
        --platform win_amd64 --python-version 310 --abi cp310 --only-binary=:all: \
        pip setuptools wheel \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" "fastapi" "redis" "colorama" \
    && mkdir -p /root/.wine-mt5-terminal1/drive_c/Python310/Lib/site-packages \
    && unzip -q /opt/whl/pip-*.whl -d /root/.wine-mt5-terminal1/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/setuptools-*.whl -d /root/.wine-mt5-terminal1/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/wheel-*.whl -d /root/.wine-mt5-terminal1/drive_c/Python310/Lib/site-packages/ \
    && WINEPREFIX=/root/.wine-mt5-terminal1 WINEARCH=win64 WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe -m pip install --no-index --find-links=Z:\\opt\\whl \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" "fastapi" "redis" "colorama" \
    && rm -rf /opt/winpy /opt/whl

# ---- download + extract MT5 terminal (MediaFire) ----
RUN echo "[build] Downloading MT5 portable from MediaFire..." \
    && curl -L -o /tmp/mt5_portable.zip "https://download1336.mediafire.com/9tb8b4scinwg_e1ZbYoVCEzQOndXbFc6KUWS8g7KSnTxu_v8VWSID-BTENcPcPsr2-TJV2Lt1ai48nfwJOAINciJvqONgbIKQtOcEcPdRy9KXmsVUdolBxZeBLW0lzpRN95312HfhNNKSujxz9iK33Om1sq_GNlGZfIjxJnI1nDikw/95pxuqd3nzegjs1/FundingPips+2+MT5+Terminal.zip" \
    && echo "[build] Extracting MT5 archive..." \
    && mkdir -p /root/.wine-mt5-terminal1/drive_c/MetaTrader5 \
    && unzip -q /tmp/mt5_portable.zip -d /root/.wine-mt5-terminal1/drive_c/MetaTrader5/ \
    && rm /tmp/mt5_portable.zip \
    && echo "[build] MT5 portable deployment complete."

# ---- application code ----
WORKDIR /opt/mt5bridge
COPY backend/ /opt/mt5bridge/backend/
COPY scripts/entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

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
    WINEDLLOVERRIDES="api-ms-win-crt-runtime-l1-1-0=n,b;ucrtbase=n,b;vcruntime140=n,b;vcruntime140_1=n,b;msvcp140=n,b;concrt140=n,b" \
    MT5_TERMINAL_PATH=/root/.wine-mt5-terminal1/drive_c/MetaTrader5/terminal64.exe \
    MT5_WINE_PREFIX=/root/.wine-mt5-terminal1 \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    REDIS_URL=redis://127.0.0.1:6379/0 \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    PATH="/usr/lib/wine:/opt/wine-stable/bin:/usr/bin:/usr/local/bin:${PATH}"

# ---- system deps: WineHQ (stable) + redis + python3 + tools ----
# Note: the bridge itself runs under Wine's embedded Python, but we keep
# native python3/pip for the offline wheel download step.
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash coreutils curl ca-certificates unzip xvfb \
        redis-server python3 python3-pip fontconfig fonts-dejavu-core \
        libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 libxi6 \
        libxrandr2 libxxf86vm1 wget gnupg2 software-properties-common cabextract \
    && mkdir -pm755 /etc/apt/keyrings \
    && wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key \
    && wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/jammy/winehq-jammy.sources \
    && apt-get update \
    && apt-get install -y --install-recommends winehq-stable \
    && rm -rf /var/lib/apt/lists/*

# ---- initialize Wine prefix + set Windows 10 mode via registry ----
RUN echo "[build] initializing 64-bit Wine prefix..." \
    && wineboot --init \
    && wineserver -w \
    && echo "[build] setting Windows 10 mode via registry..." \
    && wine reg add "HKEY_CURRENT_USER\Software\Wine" /v Version /t REG_SZ /d "win10" /f \
    && wineserver -w \
    && wine --version

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
    && WINEPREFIX=/root/.wine-mt5-terminal1 WINEARCH=win64 WINEDEBUG=-all wine \
        C:\\Python310\\python.exe -m pip install --no-index --find-links=Z:\\opt\\whl \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" "fastapi" "redis" "colorama" \
    && rm -rf /opt/winpy /opt/whl

# ---- download + extract MT5 terminal (MediaFire) ----
RUN echo "[build] Downloading MT5 portable from MediaFire..." \
    && curl -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" -o /tmp/mt5_portable.zip "https://download1336.mediafire.com/t28n4007b17g6zqmVmLgjCK-q3fUncc6ZvDGgrp9CaGwvM7Bu8aGXysNvDSxrCU7k_TXYC5SpeQNYmXJ4WYfh7rOhgsVSk9KOarG9Pvixt0bEE0UmnFeTJLuipTFI7IGqXECGgjAwaTkn6A1Jrt9_DEXJSFNAT4yTU4Zx74hzGxxGQ/95pxuqd3nzegjs1/FundingPips+2+MT5+Terminal.zip" \
    && echo "[build] Extracting MT5 archive..." \
    && mkdir -p /root/.wine-mt5-terminal1/drive_c/MetaTrader5 \
    && unzip -q /tmp/mt5_portable.zip -d /root/.wine-mt5-terminal1/drive_c/MetaTrader5/ \
    && rm /tmp/mt5_portable.zip \
    && echo "[build] MT5 portable deployment complete."

# ---- install Visual C++ 2015-2022 redistributable (required by modern MT5) ----
RUN echo "[build] Downloading VC++ redistributable..." \
    && curl -L -o /tmp/vc_redist.x64.exe "https://aka.ms/vs/17/release/vc_redist.x64.exe" \
    && echo "[build] Installing VC++ redistributable into Wine prefix..." \
    && xvfb-run -a wine /tmp/vc_redist.x64.exe /q /norestart \
    && wineserver -w \
    && rm /tmp/vc_redist.x64.exe \
    && echo "[build] VC++ runtime installation complete."

# ---- application code ----
WORKDIR /opt/mt5bridge
COPY backend/ /opt/mt5bridge/backend/
COPY scripts/entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

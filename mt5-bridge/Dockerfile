# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64).
#   * Base: scottyhardy/docker-wine:latest — Debian + official WineHQ Wine (v8+),
#     UCRT-capable, no manual Wine install / prefix init needed. Runs as the
#     built-in `wine` user with WINEPREFIX=/home/wine/.wine.
#   * No i386/wine32 assumptions; the image is 64-bit ready.
#   * Python 3.10.11 embeddable (Windows) runs inside Wine; the Windows-only
#     MetaTrader5 package is pip-installed into it. server.py runs via
#     `wine64 C:\Python310\python.exe`.
#   * MT5 terminal fetched from MediaFire at build time (not committed to Git).

FROM scottyhardy/docker-wine:latest

# The base image already provides: wine, wine64, winetricks, Xvfb-friendly env,
# and WINEPREFIX=/home/wine/.wine for the `wine` user.
ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    WINEARCH=win64 \
    WINEPREFIX=/home/wine/.wine \
    WINEDEBUG=-all \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    MT5_TERMINAL_PATH=C:\\MetaTrader5\\terminal64.exe \
    MT5_DIR=C:\\MetaTrader5 \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    WINEDLLOVERRIDES="*api-ms-win-crt*=n,b;ucrtbase=n,b"

USER root

# ---- ensure tooling present (base image is minimal Debian) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        unzip \
        python3 \
        python3-pip \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && echo "[build] installing winetricks..." \
    && curl -fsSL -o /usr/local/bin/winetricks \
        "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks" \
    && chmod +x /usr/local/bin/winetricks

# ---- install native UCRT runtime (forced 64-bit) ----
# WineHQ already supports modern UCRT functions; vcrun2019 supplies the native
# DLLs. Registry overrides (native,builtin) are written for safety.
RUN echo "[build] installing UCRT (vcrun2019) via winetricks, forced win64..." \
    && WINEARCH=win64 WINEPREFIX=/home/wine/.wine \
        winetricks -q --force vcrun2019 \
    && echo "[build] winetricks vcrun2019 done" \
    && echo "[build] writing UCRT DLL overrides into registry..." \
    && for dll in ucrtbase api-ms-win-crt-runtime-l1-1-0 api-ms-win-crt-private-l1-1-0; do \
         wine64 reg add 'HKCU\Software\Wine\DllOverrides' \
            /v "${dll}" /t REG_SZ /d "native,builtin" /f; \
       done \
    && wine64 reg add 'HKCU\Software\Wine\DllOverrides' \
            /v "*api-ms-win-crt*" /t REG_SZ /d "native,builtin" /f

# ---- Install a WINDOWS Python 3.10.11 inside the Wine prefix (HEADLESS) ----
ENV WINEPYTHON=C:\\Python310

RUN mkdir -p /opt/winpy /opt/whl \
    && curl -fsSL -o /opt/winpy/python310.zip \
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip" \
    && echo "[build] Windows Python 3.10.11 (embeddable) downloaded" \
    && mkdir -p /home/wine/.wine/drive_c/Python310 \
    && unzip -o /opt/winpy/python310.zip -d /home/wine/.wine/drive_c/Python310/ \
    && echo "[build] enabling site-packages in embeddable Python (uncomment import site)" \
    && sed -i 's/^#import site/import site/' /home/wine/.wine/drive_c/Python310/python310._pth \
    && echo "[build] downloading WINDOWS wheels with native pip3 (no Wine networking)..." \
    && pip3 download --no-cache-dir --dest=/opt/whl \
        --platform win_amd64 --python-version 310 --abi cp310 --only-binary=:all: \
        pip setuptools wheel colorama \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] unzipping pip/setuptools/wheel directly into Wine Python site-packages..." \
    && mkdir -p /home/wine/.wine/drive_c/Python310/Lib/site-packages \
    && unzip -q /opt/whl/pip-*.whl -d /home/wine/.wine/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/setuptools-*.whl -d /home/wine/.wine/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/wheel-*.whl -d /home/wine/.wine/drive_c/Python310/Lib/site-packages/ \
    && echo "[build] installing MetaTrader5 + socketio + uvicorn into Wine Python..." \
    && WINEPREFIX=/home/wine/.wine WINEARCH=win64 WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe -m pip install --no-index --find-links=Z:\\opt\\whl \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" colorama \
    && echo "[build] Wine Python + MetaTrader5 ready" \
    && rm -rf /opt/winpy /opt/whl

WORKDIR /opt/mt5bridge

# ---- Download the MT5 portable archive at BUILD TIME ----
# Fetched from MediaFire; extracted into the Wine C: drive at
# C:\MetaTrader5\terminal64.exe. Not committed to Git (terminal64.exe > 100MB).
RUN echo "[build] Downloading MT5 portable from MediaFire..." \
    && curl -L -o /tmp/mt5_portable.zip "https://download1336.mediafire.com/9tb8b4scinwg_e1ZbYoVCEzQOndXbFc6KUWS8g7KSnTxu_v8VWSID-BTENcPcPsr2-TJV2Lt1ai48nfwJOAINciJvqONgbIKQtOcEcPdRy9KXmsVUdolBxZeBLW0lzpRN95312HfhNNKSujxz9iK33Om1sq_GNlGZfIjxJnI1nDikw/95pxuqd3nzegjs1/FundingPips+2+MT5+Terminal.zip" \
    && echo "[build] Extracting MT5 archive..." \
    && mkdir -p /home/wine/.wine/drive_c/MetaTrader5 \
    && unzip -q /tmp/mt5_portable.zip -d /home/wine/.wine/drive_c/MetaTrader5/ \
    && rm /tmp/mt5_portable.zip \
    && echo "[build] MT5 portable deployment complete."

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py
COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

# Files created under /home/wine must be owned by the wine user.
RUN chown -R wine:wine /home/wine/.wine /opt/mt5bridge

USER wine

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]

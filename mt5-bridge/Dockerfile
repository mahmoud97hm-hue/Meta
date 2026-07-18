# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64).
#   * PURE 64-BIT. No i386, no wine32. winetricks runs FORCED 64-bit only.
#   * UCRT (Universal C Runtime) installed via winetricks vcrun2019 (forced
#     win64) with a native DLL-injection fallback for strict 64-bit kernels.
#   * Base: Ubuntu 22.04 with native wine64.
#   * Wine prefix explicitly initialized (wineboot) before any Python step.
#   * Python 3.10.11 embeddable (Windows) runs inside Wine; the Windows-only
#     MetaTrader5 package is pip-installed into it. server.py runs via
#     `wine64 C:\Python310\python.exe`.

FROM ubuntu:22.04

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    WINEARCH=win64 \
    WINEPREFIX=/root/.wine \
    WINEDEBUG=-all \
    BRIDGE_HOST=0.0.0.0 \
    BRIDGE_PORT=3000 \
    MT5_TERMINAL_PATH=C:\\MetaTrader5\\terminal64.exe \
    MT5_DIR=C:\\MetaTrader5 \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    WINEDLLOVERRIDES="api-ms-win-crt-runtime-l1-1-0=n,b;api-ms-win-crt-private-l1-1-0=n,b;ucrtbase=n,b" \
    PATH="/usr/lib/wine:/usr/bin:/usr/local/bin:${PATH}"

# ---- system deps: xvfb + native wine64 + winetricks + tools ----
# STRICTLY 64-BIT: no wine32 / i386 anywhere.
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
        wine64 \
        wget \
    && rm -rf /var/lib/apt/lists/* \
    && echo "[build] installing winetricks..." \
    && curl -fsSL -o /usr/local/bin/winetricks \
        "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks" \
    && chmod +x /usr/local/bin/winetricks

# ---- locate Wine binaries and add them to PATH before using them ----
RUN echo "[build] locating Wine binaries..." \
    && WINE_PATH=$(find /usr/lib -name 'wine' -type d 2>/dev/null | head -1) \
    && echo "[build] wine binary dir: ${WINE_PATH}" \
    && export PATH="${WINE_PATH}:/usr/lib/wine:$PATH" \
    && echo "[build] verifying wine/wine64/wineboot/wineserver are on PATH:" \
    && which wine wine64 wineboot wineserver \
    # ---- initialize Wine prefix (generates kernel32.dll, avoids c0000135) ----
    && echo "[build] initializing 64-bit Wine prefix..." \
    && wineboot --init 2>&1 || true \
    && wineserver -w \
    && wine64 --version

# --------------------------------------------------------------------------
# UCRT INSTALL via winetricks, FORCED 64-BIT (no i386 / wine32)
# --------------------------------------------------------------------------
# Railway's kernel forbids 32-bit execution, so winetricks must run purely
# under the 64-bit Wine loader. We force WINEARCH=win64 and skip any 32-bit
# prefix work; vcrun2019 provides ucrtbase.dll + api-ms-win-crt-*.dll for the
# 64-bit side only. If winetricks still fails (e.g. it pulls a 32-bit dep),
# we fall back to downloading pre-extracted 64-bit UCRT DLLs and injecting
# them directly into system32.
RUN echo "[build] installing UCRT (vcrun2019) via winetricks, forced win64..." \
    && WINEARCH=win64 WINEPREFIX=/root/.wine \
        winetricks -q --force vcrun2019 \
    && echo "[build] winetricks vcrun2019 done" \
    || { \
         echo "[build] winetricks failed — FALLBACK: injecting pre-extracted 64-bit UCRT DLLs"; \
         mkdir -p /tmp/ucrt /root/.wine/drive_c/windows/system32; \
         curl -fsSL -o /tmp/ucrt/ucrt.zip \
            "https://github.com/mirror/ucrt/archive/refs/heads/master.zip" 2>/dev/null \
            || curl -fsSL -o /tmp/ucrt/ucrt.zip \
            "https://github.com/oganm/ucrt/archive/refs/heads/master.zip"; \
         unzip -o -j /tmp/ucrt/ucrt.zip '*.dll' -d /tmp/ucrt 2>/dev/null || true; \
         find /tmp/ucrt -type f \( -iname 'ucrtbase.dll' -o -iname 'api-ms-win-crt-*.dll' \) \
            -exec cp -v {} /root/.wine/drive_c/windows/system32/ \; ; \
         wine64 reg add 'HKCU\Software\Wine\DllOverrides' \
            /v "ucrtbase" /t REG_SZ /d "native" /f 2>/dev/null || true; \
         echo "[build] UCRT fallback injection complete"; \
         rm -rf /tmp/ucrt; \
       }

# ---- Install a WINDOWS Python 3.10.11 inside the Wine prefix (HEADLESS) ----
# MetaTrader5 is Windows-only, so server.py runs under Wine's Windows Python.
# We use the EMBEDDABLE zip (no GUI installer). We enable site-packages via
# python310._pth, fetch Windows wheels with native pip3, unzip pip/setuptools
# directly into site-packages, then offline-install MetaTrader5 etc.
ENV WINEPYTHON=C:\\Python310

RUN mkdir -p /opt/winpy /opt/whl \
    && curl -fsSL -o /opt/winpy/python310.zip \
        "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip" \
    && echo "[build] Windows Python 3.10.11 (embeddable) downloaded" \
    && mkdir -p /root/.wine/drive_c/Python310 \
    && unzip -o /opt/winpy/python310.zip -d /root/.wine/drive_c/Python310/ \
    && echo "[build] enabling site-packages in embeddable Python (uncomment import site)" \
    && sed -i 's/^#import site/import site/' /root/.wine/drive_c/Python310/python310._pth \
    && echo "[build] downloading WINDOWS wheels with native pip3 (no Wine networking)..." \
    && pip3 download --no-cache-dir --dest=/opt/whl \
        --platform win_amd64 --python-version 310 --abi cp310 --only-binary=:all: \
        pip setuptools wheel colorama \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" \
    && echo "[build] unzipping pip/setuptools/wheel directly into Wine Python site-packages..." \
    && mkdir -p /root/.wine/drive_c/Python310/Lib/site-packages \
    && unzip -q /opt/whl/pip-*.whl -d /root/.wine/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/setuptools-*.whl -d /root/.wine/drive_c/Python310/Lib/site-packages/ \
    && unzip -q /opt/whl/wheel-*.whl -d /root/.wine/drive_c/Python310/Lib/site-packages/ \
    && echo "[build] installing MetaTrader5 + socketio + uvicorn into Wine Python..." \
    && WINEPREFIX=/root/.wine WINEARCH=win64 WINEDEBUG=-all wine64 \
        C:\\Python310\\python.exe -m pip install --no-index --find-links=Z:\\opt\\whl \
        "MetaTrader5==5.0.45" "python-socketio==5.11.4" "aiohttp" "uvicorn" colorama \
    && echo "[build] Wine Python + MetaTrader5 ready" \
    && rm -rf /opt/winpy /opt/whl

WORKDIR /opt/mt5bridge

# ---- Download the MT5 portable archive at BUILD TIME ----
# The portable directory is NOT committed to Git (terminal64.exe > 100MB).
# It is fetched from Google Drive and extracted straight into the Wine C:
# drive so the terminal lives at C:\MetaTrader5\terminal64.exe. The UCRT
# native DLLs injected earlier are still required for it to run.
RUN echo "[build] Downloading MT5 portable from MediaFire..." \
    && curl -L -o /tmp/mt5_portable.zip "https://download1336.mediafire.com/9tb8b4scinwg_e1ZbYoVCEzQOndXbFc6KUWS8g7KSnTxu_v8VWSID-BTENcPcPsr2-TJV2Lt1ai48nfwJOAINciJvqONgbIKQtOcEcPdRy9KXmsVUdolBxZeBLW0lzpRN95312HfhNNKSujxz9iK33Om1sq_GNlGZfIjxJnI1nDikw/95pxuqd3nzegjs1/FundingPips+2+MT5+Terminal.zip" \
    && echo "[build] Extracting MT5 archive..." \
    && mkdir -p /root/.wine/drive_c/MetaTrader5 \
    && unzip -q /tmp/mt5_portable.zip -d /root/.wine/drive_c/MetaTrader5/ \
    && rm /tmp/mt5_portable.zip \
    && echo "[build] MT5 portable deployment complete."

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]
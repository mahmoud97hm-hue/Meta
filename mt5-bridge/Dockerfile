# mt5-bridge/Dockerfile — HEADLESS, VNC-FREE, SELF-CONTAINED MT5 bridge
#
# Built for Railway (Linux/amd64).
#   * NO VNC, NO WM — Xvfb virtual display only.
#   * Base: Ubuntu 22.04 with native wine64 (no WineHQ repo, no i386).
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
    MT5_TERMINAL_PATH=/opt/mt5/terminal/terminal64.exe \
    MT5_DIR=/opt/mt5/terminal \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    PATH="/usr/lib/wine:/usr/bin:/usr/local/bin:${PATH}"

# ---- system deps: xvfb + native wine64 + python3 + tools ----
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
        cabextract \
    && rm -rf /var/lib/apt/lists/*

# ---- winetricks: pull the live script from upstream, not apt's stale one ----
# Ubuntu 22.04's apt winetricks (20210206) hardcodes sha256 checksums for MS
# redistributable downloads. Microsoft rotates the binary behind the aka.ms
# "latest" alias URLs periodically, so a 5-year-old script's checksums drift
# out of date and every download fails verification. Upstream's script is
# updated to track this; always use it instead of the distro package.
RUN curl -fsSL -o /usr/local/bin/winetricks \
        https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks \
    && chmod +x /usr/local/bin/winetricks \
    && winetricks --version

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
    && echo "[build] installing vcrun2019 (real UCRT) into prefix..." \
    && xvfb-run -a env WINEPREFIX=/root/.wine WINE=wine64 WINEARCH=win64 winetricks -q vcrun2019 \
    && wineserver -w \
    && echo "[build] verifying vcrun2019 actually completed (not just exited 0)..." \
    && (test -f /root/.wine/winetricks.log && grep -qx 'vcrun2019' /root/.wine/winetricks.log \
        && echo "[build] vcrun2019 confirmed in winetricks.log" \
        || (echo "[build] FATAL: vcrun2019 did not complete — winetricks.log missing the verb. Build aborted so this doesn't silently ship a broken UCRT." && exit 1)) \
    && echo "[build] verifying wine64:" \
    && wine64 --version

# ---- Install a WINDOWS Python 3.10.11 inside the Wine prefix (HEADLESS) ----
# MetaTrader5 is Windows-only, so server.py runs under Wine's Windows Python.
# We use the EMBEDDABLE zip (no GUI installer). We enable site-packages via
# python310._pth, fetch Windows wheels with native pip3, unzip pip/setuptools
# directly into site-packages, then offline-install MetaTrader5 etc.
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
    && rm -rf /opt/winpy /opt/mt5/install /opt/whl

WORKDIR /opt/mt5bridge

# ---- Auto-install the MT5 terminal at BUILD TIME (no Windows host required) ----
RUN mkdir -p /opt/mt5/install && curl -fsSL -o /opt/mt5/install/mt5setup.exe "https://download.metatrader.com/cdn/web/fundingpips.corp.2/mt5/fundingpips25setup.exe" && echo "[build] MT5 installer downloaded" && WINEPREFIX=/root/.wine WINEARCH=win64 WINEDEBUG=-all wine64 /opt/mt5/install/mt5setup.exe /portable /silent /skipupdate /dir="C:\terminal" && echo "[build] MT5 installed headlessly" && rm -rf /opt/mt5/install

# ---- Bridge server (runs under Wine Python at runtime) ----
COPY server.py     /opt/mt5bridge/server.py

COPY entrypoint.sh /opt/mt5bridge/entrypoint.sh
RUN chmod +x /opt/mt5bridge/entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/opt/mt5bridge/entrypoint.sh"]
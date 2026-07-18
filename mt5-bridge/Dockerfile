# mt5-bridge/Dockerfile — HEADLESS MT5 bridge (streamlined, Wine 11.0)
#
# Built for Railway (Linux/amd64).
#   * Base: scottyhardy/docker-wine:latest (Wine 11.0, UCRT-native — no
#     winetricks / vcrun2019 needed; fetestexcept works out of the box).
#   * Clean 64-bit prefix, MediaFire MT5 terminal, run headless via wine64.
#   * Pure 64-bit: no i386, no wine32.

FROM scottyhardy/docker-wine:latest

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    WINEARCH=win64 \
    WINEPREFIX=/home/wine/.wine \
    WINEDEBUG=-all \
    MT5_TERMINAL_PATH=C:\\MetaTrader5\\terminal64.exe \
    PYTHONHASHSEED=0 \
    XDG_RUNTIME_DIR=/tmp \
    PATH="/opt/wine-stable/bin:/usr/bin:/usr/local/bin:${PATH}"

USER root

# ---- minimal tooling ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates unzip xvfb \
    && rm -rf /var/lib/apt/lists/*

# ---- initialize a clean 64-bit Wine prefix ----
RUN echo "[build] initializing clean 64-bit Wine prefix..." \
    && WINEARCH=win64 WINEPREFIX=/home/wine/.wine wineboot -i 2>&1 || true \
    && wineserver -w \
    && wine64 --version

WORKDIR /opt/mt5

# ---- download + extract the MT5 portable archive (MediaFire) ----
RUN echo "[build] Downloading MT5 portable from MediaFire..." \
    && curl -L -o /tmp/mt5_portable.zip "https://download1336.mediafire.com/9tb8b4scinwg_e1ZbYoVCEzQOndXbFc6KUWS8g7KSnTxu_v8VWSID-BTENcPcPsr2-TJV2Lt1ai48nfwJOAINciJvqONgbIKQtOcEcPdRy9KXmsVUdolBxZeBLW0lzpRN95312HfhNNKSujxz9iK33Om1sq_GNlGZfIjxJnI1nDikw/95pxuqd3nzegjs1/FundingPips+2+MT5+Terminal.zip" \
    && echo "[build] Extracting MT5 archive..." \
    && mkdir -p /home/wine/.wine/drive_c/MetaTrader5 \
    && unzip -q /tmp/mt5_portable.zip -d /home/wine/.wine/drive_c/MetaTrader5/ \
    && rm /tmp/mt5_portable.zip \
    && echo "[build] MT5 portable deployment complete."

# Files under /home/wine must be owned by the wine user.
RUN chown -R wine:wine /home/wine/.wine

USER wine

# ---- runtime: virtual display + headless MT5 terminal ----
CMD ["sh", "-c", "Xvfb ${DISPLAY} -screen 0 1024x768x16 -nolisten tcp & sleep 2 && wine64 C:\\\\MetaTrader5\\\\terminal64.exe /portable"]

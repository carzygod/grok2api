#!/usr/bin/env bash
set -euo pipefail

mkdir -p /config /tmp/grok2api-browser
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
rm -f /config/SingletonCookie /config/SingletonLock /config/SingletonSocket

export DISPLAY="${DISPLAY:-:99}"
DISPLAY_WIDTH="${DISPLAY_WIDTH:-1440}"
DISPLAY_HEIGHT="${DISPLAY_HEIGHT:-900}"
START_URL="${START_URL:-https://grok.com/}"
CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}"
CHROME_DEBUG_PORT_INTERNAL="${CHROME_DEBUG_PORT_INTERNAL:-9223}"
CHROME_BIN="${CHROME_BIN:-}"
if [ -z "$CHROME_BIN" ]; then
  CHROME_BIN="$(find /ms-playwright -path "*/chrome-linux/chrome" -type f 2>/dev/null | sort | tail -n 1 || true)"
fi
if [ -z "$CHROME_BIN" ]; then
  CHROME_BIN="$(command -v chromium || command -v google-chrome || command -v chrome || true)"
fi
if [ -z "$CHROME_BIN" ]; then
  echo "No Chromium/Chrome binary found." >&2
  exit 1
fi

Xvfb "$DISPLAY" -screen 0 "${DISPLAY_WIDTH}x${DISPLAY_HEIGHT}x24" -ac +extension RANDR &
XVFB_PID=$!

sleep 1
fluxbox >/tmp/grok2api-browser/fluxbox.log 2>&1 &
FLUXBOX_PID=$!

VNC_ARGS=(-display "$DISPLAY" -forever -shared -rfbport 5900)
if [ -n "${VNC_PASSWORD:-}" ]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/grok2api-browser/vnc.pass >/dev/null
  VNC_ARGS+=(-rfbauth /tmp/grok2api-browser/vnc.pass)
else
  VNC_ARGS+=(-nopw)
fi
x11vnc "${VNC_ARGS[@]}" >/tmp/grok2api-browser/x11vnc.log 2>&1 &
XVNC_PID=$!

websockify --web=/usr/share/novnc 5800 localhost:5900 >/tmp/grok2api-browser/novnc.log 2>&1 &
NOVNC_PID=$!

socat "TCP-LISTEN:${CHROME_DEBUG_PORT},fork,reuseaddr,bind=0.0.0.0" "TCP:127.0.0.1:${CHROME_DEBUG_PORT_INTERNAL}" >/tmp/grok2api-browser/cdp-proxy.log 2>&1 &
CDP_PROXY_PID=$!

cleanup() {
  kill "${CHROME_PID:-}" "${CDP_PROXY_PID:-}" "${NOVNC_PID:-}" "${XVNC_PID:-}" "${FLUXBOX_PID:-}" "$XVFB_PID" 2>/dev/null || true
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
}
trap cleanup EXIT TERM INT

"$CHROME_BIN" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --disable-breakpad \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --use-mock-keychain \
  --user-data-dir=/config \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="${CHROME_DEBUG_PORT_INTERNAL}" \
  --remote-allow-origins="*" \
  --window-size="${DISPLAY_WIDTH},${DISPLAY_HEIGHT}" \
  "$START_URL" >/tmp/grok2api-browser/chromium.log 2>&1 &
CHROME_PID=$!

wait "$CHROME_PID"

#!/usr/bin/env bash
set -euo pipefail

mkdir -p /config /tmp/grok2api-browser
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
rm -f /config/SingletonCookie /config/SingletonLock /config/SingletonSocket

CHROME_USER="${CHROME_USER:-pwuser}"
if ! id "$CHROME_USER" >/dev/null 2>&1; then
  CHROME_USER=chrome
  if ! id "$CHROME_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$CHROME_USER" >/dev/null 2>&1 || CHROME_USER=""
  fi
fi
if [ -n "$CHROME_USER" ] && id "$CHROME_USER" >/dev/null 2>&1; then
  chown -R "$CHROME_USER:$CHROME_USER" /config /tmp/grok2api-browser
fi

export DISPLAY="${DISPLAY:-:99}"
export TZ="${TZ:-Asia/Taipei}"
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
  for candidate in \
    /opt/google/chrome/chrome \
    /usr/lib/chromium/chromium \
    /usr/bin/google-chrome-stable \
    /usr/bin/google-chrome \
    /usr/bin/chrome \
    /usr/bin/chromium; do
    if [ -x "$candidate" ]; then
      CHROME_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$CHROME_BIN" ]; then
  echo "No Chromium/Chrome binary found." >&2
  exit 1
fi

if command -v dbus-daemon >/dev/null 2>&1; then
  mkdir -p /run/dbus
  dbus-daemon --system --fork >/tmp/grok2api-browser/dbus-system.log 2>&1 || true
  DBUS_INFO="$(dbus-daemon --session --fork --print-address --print-pid 2>/tmp/grok2api-browser/dbus-session.log || true)"
  if [ -n "$DBUS_INFO" ]; then
    export DBUS_SESSION_BUS_ADDRESS="$(printf '%s\n' "$DBUS_INFO" | sed -n '1p')"
    DBUS_PID="$(printf '%s\n' "$DBUS_INFO" | sed -n '2p')"
  fi
fi

Xvfb "$DISPLAY" -screen 0 "${DISPLAY_WIDTH}x${DISPLAY_HEIGHT}x24" -ac -noreset +extension RANDR +extension GLX +extension RENDER &
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
  kill "${CHROME_PID:-}" "${CDP_PROXY_PID:-}" "${NOVNC_PID:-}" "${XVNC_PID:-}" "${FLUXBOX_PID:-}" "${DBUS_PID:-}" "$XVFB_PID" 2>/dev/null || true
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
}
trap cleanup EXIT TERM INT

CHROME_ARGS=(
  --user-data-dir=/config
  --remote-debugging-address=127.0.0.1
  --remote-debugging-port="${CHROME_DEBUG_PORT_INTERNAL}"
  "$START_URL"
)
if [ -n "${CHROME_PROXY_SERVER:-}" ]; then
  CHROME_ARGS+=(--proxy-server="${CHROME_PROXY_SERVER}")
fi
if [ -n "${CHROME_PROXY_BYPASS_LIST:-}" ]; then
  CHROME_ARGS+=(--proxy-bypass-list="${CHROME_PROXY_BYPASS_LIST}")
fi

if [ -n "$CHROME_USER" ] && id "$CHROME_USER" >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
  runuser -u "$CHROME_USER" -- "$CHROME_BIN" "${CHROME_ARGS[@]}" >/tmp/grok2api-browser/chromium.log 2>&1 &
elif [ -n "$CHROME_USER" ] && id "$CHROME_USER" >/dev/null 2>&1 && command -v su >/dev/null 2>&1; then
  su -s /bin/bash "$CHROME_USER" -c "$(printf '%q ' "$CHROME_BIN" "${CHROME_ARGS[@]}")" >/tmp/grok2api-browser/chromium.log 2>&1 &
else
  "$CHROME_BIN" "${CHROME_ARGS[@]}" >/tmp/grok2api-browser/chromium.log 2>&1 &
fi
CHROME_PID=$!

wait "$CHROME_PID"

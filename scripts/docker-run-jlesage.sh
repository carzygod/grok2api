#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${GROK2API_DATA_DIR:-$(pwd)/data}"
PROFILE="${1:-default}"
PORT="${GROK2API_NOVNC_PORT:-6080}"
VNC_PASSWORD="${GROK2API_VNC_PASSWORD:-change-me-vnc-password}"

mkdir -p "${DATA_DIR}/profiles/${PROFILE}"

docker run -d --name "grok2api-browser-${PROFILE}" \
  --restart unless-stopped \
  -p "${PORT}:5800" \
  -p "$((PORT + 1000)):9222" \
  -e DISPLAY_WIDTH=1440 \
  -e DISPLAY_HEIGHT=900 \
  -e VNC_PASSWORD="${VNC_PASSWORD}" \
  -v "${DATA_DIR}/profiles/${PROFILE}:/config" \
  grok2api-browser:latest

echo "Chromium noVNC URL: http://127.0.0.1:${PORT}"

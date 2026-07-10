#!/usr/bin/env bash
set -euo pipefail

BRIDGE_IP="${GROK2API_WARP_BRIDGE_IP:-172.17.0.1}"
WARP_PROXY_PORT="${GROK2API_WARP_PROXY_PORT:-40000}"
BRIDGE_PORT="${GROK2API_WARP_BRIDGE_PORT:-40001}"
SERVICE_NAME="${GROK2API_WARP_BRIDGE_SERVICE:-grok2api-warp-proxy-bridge.service}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

. /etc/os-release
CODENAME="${VERSION_CODENAME:-jammy}"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg socat

if ! command -v warp-cli >/dev/null 2>&1; then
  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
    | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${CODENAME} main" \
    >/etc/apt/sources.list.d/cloudflare-client.list
  apt-get update
  apt-get install -y cloudflare-warp
fi

warp-cli --accept-tos registration show >/dev/null 2>&1 || warp-cli --accept-tos registration new

cat >/etc/systemd/system/"${SERVICE_NAME}" <<EOF
[Unit]
Description=grok2api browser WARP SOCKS bridge
After=network-online.target docker.service warp-svc.service
Wants=network-online.target docker.service warp-svc.service

[Service]
Type=simple
ExecStartPre=/usr/bin/warp-cli --accept-tos mode proxy
ExecStartPre=/usr/bin/warp-cli --accept-tos proxy port ${WARP_PROXY_PORT}
ExecStartPre=/usr/bin/warp-cli --accept-tos connect
ExecStart=/usr/bin/socat TCP-LISTEN:${BRIDGE_PORT},bind=${BRIDGE_IP},fork,reuseaddr TCP:127.0.0.1:${WARP_PROXY_PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "WARP bridge service: ${SERVICE_NAME}"
echo "Browser proxy: socks5://${BRIDGE_IP}:${BRIDGE_PORT}"

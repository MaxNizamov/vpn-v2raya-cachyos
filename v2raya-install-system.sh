#!/bin/bash
# v2rayA system integration script.
# Installs /etc/default/v2raya (Xray backend, DoH-ready) and enables the service.
# Run this from a terminal where you can authenticate with sudo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/v2raya.default"
DST=/etc/default/v2raya

echo "[1/4] Installing $DST ..."
sudo install -Dm644 "$SRC" "$DST"

echo "[2/4] Ensuring /etc/v2raya config dir ..."
sudo install -d -m755 /etc/v2raya

echo "[3/4] Ensuring log dir ..."
sudo install -d -o root -g root -m755 /var/log/v2raya

echo "[4/4] Enabling + starting v2raya.service ..."
sudo systemctl enable --now v2raya.service

echo
echo "=== status ==="
systemctl --no-pager --full status v2raya.service | head -20
echo
echo "=== listening ==="
ss -tlnp 2>/dev/null | grep -E '2017|v2raya' || echo "(nothing on :2017 yet)"
echo
echo "Web UI: http://127.0.0.1:2017"

#!/bin/bash
# Install v2rayA subscription fetcher as a systemd user service.
# No root required: binds to 127.0.0.1:8798, runs under your user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_NAME="v2raya-sub-fetcher.service"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "[1/6] Verifying python3 ..."
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
python3 -m py_compile "$HERE/sub-fetcher.py" && echo "  sub-fetcher.py compiles OK"

echo "[2/6] Installing unit to $USER_UNIT_DIR ..."
mkdir -p "$USER_UNIT_DIR"
# The unit file ships with a default path (%h/Dev/vpn-v2raya-cachyos/...).
# Rewrite it to point at the actual install location, so the repo can be
# cloned anywhere and `install.sh` still produces a working unit.
tmp_unit="$(mktemp)"
sed "s|%h/Dev/vpn-v2raya-cachyos/v2raya-sub-fetcher|$HERE|g" \
    "$HERE/$UNIT_NAME" > "$tmp_unit"
install -Dm644 "$tmp_unit" "$USER_UNIT_DIR/$UNIT_NAME"
rm -f "$tmp_unit"
echo "  installed unit points at: $HERE/sub-fetcher.py"

echo "[3/6] Reload user systemd ..."
systemctl --user daemon-reload

echo "[4/6] Enable + start ..."
systemctl --user enable --now "$UNIT_NAME"

echo "[5/6] Status (first 15 lines) ..."
sleep 1
systemctl --user --no-pager --full status "$UNIT_NAME" | head -15

echo
echo "[6/6] Smoke test ..."
curl -sS -w "  /healthz -> %{http_code}\n" -o /dev/null http://127.0.0.1:8798/healthz
curl -sS -w "  /sub     -> %{http_code} (%{size_download} bytes)\n" \
     -o /dev/null http://127.0.0.1:8798/sub

cat <<EOF

==========================================================
 v2rayA subscription fetcher is running.

   Local URL:  http://127.0.0.1:8798/sub
   Health:     http://127.0.0.1:8798/healthz
   Force sync: curl http://127.0.0.1:8798/force-refresh

 In v2rayA web UI (http://127.0.0.1:2017):
   Servers -> Import -> Subscription
   Address: http://127.0.0.1:8798/sub

 Manage:
   systemctl --user status  v2raya-sub-fetcher
   systemctl --user restart v2raya-sub-fetcher
   journalctl --user -u v2raya-sub-fetcher -f
==========================================================
EOF

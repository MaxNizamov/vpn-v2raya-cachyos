#!/bin/bash
# Install v2rayA grouping helper + health-check monitor as user systemd services.
# No root required: monitor talks to v2rayA's HTTP API only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "[1/6] Verifying python3 ..."
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
python3 -m py_compile "$HERE/v2raya-grouping.py" && echo "  v2raya-grouping.py compiles OK"

echo "[2/6] Checking .env exists ..."
if [ ! -f "$HERE/.env" ]; then
  echo "  .env MISSING — copy .env.example to .env and fill USERNAME/PASSWORD."
  exit 1
fi
chmod 600 "$HERE/.env"

echo "[3/6] Installing unit to $USER_UNIT_DIR ..."
mkdir -p "$USER_UNIT_DIR"
# The unit file ships with a default path (%h/Dev/vpn-v2raya-cachyos/...).
# Rewrite it to point at the actual install location, so the repo can be
# cloned anywhere and `install.sh` still produces a working unit.
tmp_unit="$(mktemp)"
sed "s|%h/Dev/vpn-v2raya-cachyos/v2raya-grouping|$HERE|g" \
    "$HERE/v2raya-monitor.service" > "$tmp_unit"
install -Dm644 "$tmp_unit" "$USER_UNIT_DIR/v2raya-monitor.service"
rm -f "$tmp_unit"
echo "  installed unit points at: $HERE/v2raya-grouping.py"

echo "[4/6] Reload user systemd ..."
systemctl --user daemon-reload

echo "[5/6] Enable + start monitor ..."
systemctl --user enable --now v2raya-monitor.service

echo "[6/6] Status (first 20 lines) ..."
sleep 2
systemctl --user --no-pager --full status v2raya-monitor.service | head -20

echo
echo "=== Smoke test (status) ==="
python3 "$HERE/v2raya-grouping.py" status | head -25

cat <<EOF

==========================================================
 v2rayA grouping + monitor is running.

 Monitor: every 5 min, checks active server in each group,
          switches if latency > 2000ms or a faster
          alternative is found (>30% improvement).

 Commands:
   python3 $HERE/v2raya-grouping.py status
   python3 $HERE/v2raya-grouping.py apply      # re-pick best
   python3 $HERE/v2raya-grouping.py monitor    # one-shot check
   python3 $HERE/v2raya-grouping.py setup      # apply TUN/DoH/RoutingA

 Manage:
   systemctl --user status  v2raya-monitor
   systemctl --user restart v2raya-monitor
   journalctl --user -u v2raya-monitor -f
==========================================================
EOF

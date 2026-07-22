#!/bin/bash
# Install the multi-server balancer core-hook for v2rayA.
# Run from a terminal where you can authenticate with sudo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_SRC="$HOME/.cache/v2raya-balancer/overlay.json"
OVERLAY_DST="/etc/v2raya/balancer-overlay.json"
HOOK_SRC="$HERE/balancer-hook.py"
HOOK_DST="/etc/v2raya/core-hook"
DEFAULTS_FILE="/etc/default/v2raya"

echo "=== [1/5] Verify overlay exists ==="
if [ ! -f "$OVERLAY_SRC" ]; then
  echo "Overlay not found at $OVERLAY_SRC"
  echo "Generate it first: python3 $HERE/v2raya-grouping.py balancer"
  exit 1
fi
echo "  overlay: $(wc -c < "$OVERLAY_SRC") bytes"

echo
echo "=== [2/5] Install overlay to $OVERLAY_DST ==="
sudo install -Dm644 "$OVERLAY_SRC" "$OVERLAY_DST"
echo "  installed"

echo
echo "=== [3/5] Install hook + balancer module to $HOOK_DST ==="
sudo install -Dm755 "$HOOK_SRC" "$HOOK_DST"
sudo install -Dm644 "$HERE/balancer.py" "/etc/v2raya/balancer.py"
echo "  installed"

echo
echo "=== [4/5] Configure V2RAYA_CORE_HOOK ==="
# Add the hook line if not already present
if grep -q "^V2RAYA_CORE_HOOK=" "$DEFAULTS_FILE" 2>/dev/null; then
  sudo sed -i "s|^V2RAYA_CORE_HOOK=.*|V2RAYA_CORE_HOOK=$HOOK_DST|" "$DEFAULTS_FILE"
  echo "  updated existing line"
else
  echo "V2RAYA_CORE_HOOK=$HOOK_DST" | sudo tee -a "$DEFAULTS_FILE" > /dev/null
  echo "  appended new line"
fi
grep "V2RAYA_CORE_HOOK" "$DEFAULTS_FILE"

echo
echo "=== [5/5] Test: validate merged config with xray ==="
# Create a merged config in /tmp and validate with xray -test
sudo cat /etc/v2raya/config.json | python3 -c "
import json, sys
sys.path.insert(0, '$HERE')
from balancer import apply_overlay_to_config

original = json.load(sys.stdin)
with open('$OVERLAY_DST') as f:
    overlay = json.load(f)
modified = apply_overlay_to_config(original, overlay)

orig_ob = len(original['outbounds'])
mod_ob = len(modified['outbounds'])
bal = modified['routing'].get('balancers', [])
rules = modified['routing'].get('rules', [])
bal_rules = sum(1 for r in rules if 'balancerTag' in r)

print(f'  Outbounds: {orig_ob} -> {mod_ob}  (+{mod_ob - orig_ob})')
print(f'  Balancers: {len(bal)}  {[b[\"tag\"]+\":\"+str(len(b[\"selector\"])) for b in bal]}')
print(f'  Rules: {len(rules)}, {bal_rules} use balancerTag')
dups = [t for t in set(o['tag'] for o in modified['outbounds']) 
        if sum(1 for o in modified['outbounds'] if o['tag']==t) > 1]
if dups:
    print(f'  WARNING: duplicate tags: {dups}')
    sys.exit(1)
print(f'  No duplicate tags')

with open('/tmp/xray-balancer-test-config.json', 'w') as f:
    json.dump(modified, f, indent=2)
print(f'  Config written to /tmp/xray-balancer-test-config.json')
"
sudo /usr/sbin/xray -test -config /tmp/xray-balancer-test-config.json 2>&1 || {
  echo "  WARNING: xray validation failed — check config manually"
  echo "  The hook will pass-through the original config."
}

echo
echo "=== Done ==="
echo "  Hook installed: $HOOK_DST"
echo "  Overlay:        $OVERLAY_DST"
echo "  Next step:      sudo systemctl restart v2raya"
echo
echo "  To verify tun0 came up with balancer:"
echo "    sudo grep -c balancerTag /etc/v2raya/config.json"
echo "    ip link show tun0"

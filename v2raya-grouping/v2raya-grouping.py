#!/usr/bin/env python3
"""
v2rayA grouping + health-check helper.

Three jobs, one tool:

  1. CLASSIFY + ASSIGN
     Match subscription servers to named v2rayA outbounds by display name.
     v2rayA on the Xray backend only allows ONE server per outbound, so we
     pick the best server per group (lowest HTTP latency) and connect it.

  2. MONITOR (cron / systemd timer / long-running --watch loop)
     Periodically test the active server in each group via /api/httpLatency.
     If it timed out or its latency exceeded the threshold, switch the group
     to the currently best server in the same group.

  3. STATUS
     Print current running state, outbounds, connected servers, and the
     expected group sizes.

Credentials are read from .env (USERNAME, PASSWORD, BASE_URL).
See .env.example.

Grouping rules are defined in GROUPING_RULES below.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Load .env without external dependency.
_ENV_PATH = Path(__file__).parent / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


# --- Grouping rules ---------------------------------------------------------
# Each rule: (outbound_name, predicate(name) -> bool, enabled_by_default)
# Evaluated top-down; first match wins. Outbound name MUST be <= 10 chars
# (v2rayA UI constraint). Reserved: proxy, direct, block.

def _is_auto(name: str) -> bool:
    return name.lstrip().startswith("⚡") and "Авто" in name

def _is_white_list(name: str) -> bool:
    return ("Белые списки" in name) or ("Белые Списки" in name)

def _is_ru(name: str) -> bool:
    return (not _is_white_list(name)) and ("Россия" in name)

def _is_plus(name: str) -> bool:
    stripped = re.sub(r"\s*·\s*\d+\s*$", "", name)
    return stripped.rstrip().endswith("+")

def _is_eu_country(name: str) -> bool:
    if _is_auto(name) or _is_white_list(name) or _is_ru(name):
        return False
    return bool(re.match(r"^\s*[\U0001F1E6-\U0001F1FF]{2}\s+\S", name))


GROUPING_RULES = [
    ("proxy",   _is_auto,                                  True),
    ("wl",      _is_white_list,                            True),
    ("ru",      _is_ru,                                    False),  # disabled per user request
    # NOTE: RoutingA parser doesn't accept '-' in outbound names, only [A-Za-z0-9_].
    ("eu_plus", lambda n: _is_eu_country(n) and _is_plus(n),   True),
    ("eu",      lambda n: _is_eu_country(n) and not _is_plus(n), True),
]

RESERVED = {"proxy", "direct", "block"}

# Latency probe: v2rayA's httpLatency actually downloads a small page through
# each server. ~1s per server in parallel batches. Default test URL is gstatic.
LATENCY_TEST_URL = "https://www.gstatic.com/generate_204"


# --- HTTP client ------------------------------------------------------------

class V2rayA:
    def __init__(self, base_url: str, username: str, password: str, verbose: bool = False):
        self.base = base_url.rstrip("/").rstrip("/api") + "/api"
        self.username = username
        self.password = password
        self.verbose = verbose
        self.token: str | None = None

    def _req(self, method: str, path: str, body=None, query=None, _allow_relogin: bool = True) -> dict:
        url = f"{self.base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", self.token)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                payload = {"code": "FAIL", "message": f"HTTP {e.code}: {e.reason}", "data": None}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            payload = {"code": "FAIL", "message": f"network error: {e}", "data": None}

        if self.verbose:
            print(f"  {method} {path} -> code={payload.get('code')} msg={payload.get('message')}")
        if payload.get("code") != "SUCCESS":
            # Auto re-login on token expiry / invalidation, then retry once.
            msg = (payload.get("message") or "").lower()
            is_auth_err = (
                payload.get("code") == "FAIL" and (
                    "token" in msg or "unauthorized" in msg or "auth" in msg
                    or "session" in msg or "login" in msg
                    or payload.get("message") == "no token present in request"
                )
            )
            if _allow_relogin and is_auth_err:
                if self.verbose:
                    print(f"  (auth error, re-login and retry)")
                try:
                    self.login()
                except RuntimeError:
                    pass  # re-login failed: fall through to raise original error
                else:
                    return self._req(method, path, body, query, _allow_relogin=False)
            raise RuntimeError(f"{method} {path} failed: {payload.get('message')}")
        return payload.get("data") or {}

    # --- auth ---
    def login(self) -> None:
        data = self._req("POST", "/login", {"username": self.username, "password": self.password})
        self.token = data["token"]

    # --- reads ---
    def get_touch(self) -> dict:
        return self._req("GET", "/touch")

    def get_outbounds(self) -> list[str]:
        return self._req("GET", "/outbounds").get("outbounds", [])

    def get_setting(self) -> dict:
        return self._req("GET", "/setting").get("setting", {})

    # --- writes (serialized — v2rayA global mutex) ---
    def create_outbound(self, name: str) -> list[str]:
        return self._req("POST", "/outbound", {"outbound": name}).get("outbounds", [])

    def delete_outbound(self, name: str) -> list[str]:
        return self._req("DELETE", "/outbound", {"outbound": name}).get("outbounds", [])

    def connect(self, which: dict) -> dict:
        return self._req("POST", "/connection", which)

    def disconnect(self, which: dict) -> dict:
        return self._req("DELETE", "/connection", which)

    def put_setting(self, setting: dict) -> None:
        self._req("PUT", "/setting", setting)

    def put_routing_a(self, rules: str) -> None:
        self._req("PUT", "/routingA", {"routingA": rules})

    def refresh_subscription(self, sub_id: int) -> dict:
        return self._req("PUT", "/subscription", {"id": sub_id, "_type": "subscription"})

    def start_v2ray(self) -> dict:
        return self._req("POST", "/v2ray")

    def stop_v2ray(self) -> dict:
        return self._req("DELETE", "/v2ray")

    # --- latency testing ---
    def http_latency(self, whiches: list[dict], test_url: str = LATENCY_TEST_URL) -> list[dict]:
        """
        Test HTTP latency for a batch of servers. Returns each `which` augmented
        with `pingLatency`. Format: {"whiches": [...]}.
        """
        if not whiches:
            return []
        return self._req("GET", "/httpLatency",
                         query={"whiches": json.dumps(whiches), "testUrl": test_url}).get("whiches", [])


# --- Grouping helpers -------------------------------------------------------

def classify(name: str) -> tuple[str, bool]:
    """Return (outbound, enabled) for a server name."""
    for outbound, predicate, enabled in GROUPING_RULES:
        if predicate(name):
            return outbound, enabled
    return "proxy", True


def collect_subscription_servers(touch_data: dict):
    """Yield {sub_id, sub_index, server_id, name, outbound, enabled}."""
    touch_inner = touch_data.get("touch") or {}
    subs = touch_inner.get("subscriptions") or []
    for sub_index, sub in enumerate(subs):
        sub_id = sub.get("id", sub_index + 1)
        for server in sub.get("servers", []) or []:
            sid = server.get("id")
            name = server.get("name", "")
            outbound, enabled = classify(name)
            yield {
                "sub_id": sub_id,
                "sub_index": sub_index,
                "server_id": sid,
                "name": name,
                "outbound": outbound,
                "enabled": enabled,
            }


def get_connected_map(touch_data: dict) -> dict:
    """Return {(sub_index, server_id): outbound}."""
    out = {}
    touch_inner = touch_data.get("touch") or {}
    for entry in touch_inner.get("connectedServer") or []:
        if entry.get("_type") == "subscriptionServer":
            out[(entry.get("sub"), entry.get("id"))] = entry.get("outbound")
    return out


def parse_latency_ms(latency_str: str | None) -> int | None:
    """'120ms' -> 120, 'timeout' -> None, None -> None."""
    if not latency_str:
        return None
    s = latency_str.strip().lower()
    if s in ("timeout", "failed", "error", ""):
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


# --- Latency probing --------------------------------------------------------

def probe_group(api: V2rayA, group_servers: list[dict], test_url: str,
                sample_size: int = 10) -> list[tuple[int | None, dict]]:
    """
    Test latency of up to `sample_size` servers in a group (to bound runtime).
    Returns list of (latency_ms_or_None, server) sorted best-first.
    Servers that timed out have latency=None and sort last (stable).
    """
    # Probe a random sample if the group is large; keep it reproducible-ish.
    candidates = group_servers if len(group_servers) <= sample_size else group_servers[:sample_size]
    whiches = [
        {"_type": "subscriptionServer", "id": s["server_id"], "sub": s["sub_index"], "outbound": s["outbound"]}
        for s in candidates
    ]
    try:
        results = api.http_latency(whiches, test_url=test_url)
    except RuntimeError as e:
        print(f"  httpLatency call failed: {e}", file=sys.stderr)
        return [(None, s) for s in candidates]
    # Map back to servers; latency is in pingLatency.
    lat_by_id: dict[tuple[int, int], int | None] = {}
    for r in results:
        lat_by_id[(r.get("sub"), r.get("id"))] = parse_latency_ms(r.get("pingLatency"))
    scored = [(lat_by_id.get((s["sub_index"], s["server_id"])), s) for s in candidates]
    scored.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0))
    return scored


# --- Commands ---------------------------------------------------------------

def cmd_status(api: V2rayA) -> int:
    touch = api.get_touch()
    print(f"v2ray running: {touch.get('running', False)}")
    print(f"outbounds:     {api.get_outbounds()}")
    touch_inner = touch.get("touch") or {}
    connected = touch_inner.get("connectedServer") or []
    print(f"\nConnected servers ({len(connected)}):")
    for e in connected:
        print(f"  {e.get('_type'):20s} sub={e.get('sub')} id={e.get('id')} -> {e.get('outbound')}")
    counts: dict[str, int] = {}
    for s in collect_subscription_servers(touch):
        counts[s["outbound"]] = counts.get(s["outbound"], 0) + 1
    print("\nGroup sizes (would-be):")
    for ob, n in sorted(counts.items()):
        print(f"  {ob:10s} {n}")
    return 0


def cmd_apply(api: V2rayA, args) -> int:
    """Create outbounds and connect the best server per enabled group."""
    if args.refresh_sub:
        touch = api.get_touch()
        subs = (touch.get("touch") or {}).get("subscriptions") or []
        if not subs:
            print("No subscriptions found.")
            return 1
        for sub in subs:
            print(f"Refreshing subscription id={sub.get('id')} ({sub.get('remarks', sub.get('address', '?'))}) ...")
            api.refresh_subscription(sub["id"])
            time.sleep(1)

    touch = api.get_touch()
    all_servers = list(collect_subscription_servers(touch))

    # Group servers by outbound.
    by_group: dict[str, list[dict]] = {}
    for s in all_servers:
        by_group.setdefault(s["outbound"], []).append(s)

    # 1. Ensure outbounds exist for enabled groups that have servers.
    existing = set(api.get_outbounds())
    for ob in sorted(by_group.keys()):
        if ob in RESERVED or ob in existing:
            continue
        if args.dry_run:
            print(f"[dry-run] create outbound: {ob}")
        else:
            print(f"create outbound: {ob}")
            api.create_outbound(ob)

    # 2. For each ENABLED group with >0 servers, pick the best and connect it.
    summary = []
    for ob in sorted(by_group.keys()):
        servers = by_group[ob]
        # enabled-by-default state from GROUPING_RULES
        enabled_default = next((e for name, _, e in GROUPING_RULES if name == ob), True)
        if not enabled_default and not args.enable_disabled:
            print(f"\n[{ob}] skipping disabled-by-default group ({len(servers)} servers)")
            summary.append((ob, len(servers), "skipped (disabled)"))
            continue

        print(f"\n[{ob}] picking best of {len(servers)} servers via httpLatency ...")
        scored = probe_group(api, servers, args.test_url, sample_size=args.sample_size)
        for lat, s in scored[:5]:
            print(f"  {str(lat):>6} ms  {s['name']}")
        best_lat, best = scored[0]
        if best_lat is None:
            print(f"  WARNING: all sampled servers in '{ob}' failed latency test")
            summary.append((ob, len(servers), "all failed"))
            continue
        which = {
            "_type": "subscriptionServer",
            "id": best["server_id"],
            "sub": best["sub_index"],
            "outbound": ob,
        }
        if args.dry_run:
            print(f"[dry-run] connect '{best['name']}' ({best_lat}ms) -> {ob}")
        else:
            print(f"connect '{best['name']}' ({best_lat}ms) -> {ob}")
            try:
                api.connect(which)
            except RuntimeError as e:
                print(f"  ERROR: {e}")
        summary.append((ob, len(servers), f"{best['name']} ({best_lat}ms)"))

    print("\n=== Summary ===")
    for ob, n, picked in summary:
        print(f"  {ob:10s} pool={n:<4d} -> {picked}")
    return 0


def cmd_monitor(api: V2rayA, args) -> int:
    """
    Once-shot health check + failover. Wrap in a loop with --watch for daemon.
    For each ENABLED group, look at the currently connected server; if its
    latency is None (timeout) or > threshold, switch to the best in the group.
    """
    if args.watch:
        return _watch_loop(api, args)

    return _run_health_check(api, args)


def _tun_is_up() -> bool:
    """Return True if the v2rayA TUN interface exists and is UP."""
    import subprocess
    try:
        r = subprocess.run(["ip", "-o", "link", "show", "tun0"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        # If `ip` is unavailable for some reason, assume up and let the
        # API-side running check below do the real work.
        return True
    if r.returncode != 0:
        return False
    # State is reported as `<UP,LOWER_UP>` etc.; if "UP" isn't in the line
    # the interface is administratively down.
    return "UP" in r.stdout


def _ensure_v2ray_running(api: V2rayA) -> bool:
    """
    Detect "v2ray-core died but v2raya is alive" — the TUN interface is gone
    even though v2raya.service looks fine. Restart v2ray via the API.

    Returns True if (re)started, False if already healthy.
    """
    touch = api.get_touch()
    running = bool(touch.get("running"))
    tun_up = _tun_is_up()
    if running and tun_up:
        return False

    reason = []
    if not running:
        reason.append("v2rayA reports running=False")
    if not tun_up:
        reason.append("tun0 interface missing")
    print(f"[tunnel] {'; '.join(reason)} -> restarting v2ray via API")
    try:
        # Stop first to clear any half-dead state, then start.
        try:
            api.stop_v2ray()
        except RuntimeError:
            pass
        api.start_v2ray()
        print("[tunnel] restart issued")
        return True
    except RuntimeError as e:
        print(f"[tunnel] restart FAILED: {e}", file=sys.stderr)
        return False


def _run_health_check(api: V2rayA, args) -> int:
    # 0. Tunnel liveness: recover from "v2ray-core died, v2raya still alive".
    #    This can happen after a hot failover — skip group checks this cycle.
    if _ensure_v2ray_running(api):
        print("[tunnel] just recovered; skipping group checks this cycle")
        return 0

    touch = api.get_touch()
    all_servers = list(collect_subscription_servers(touch))
    connected = get_connected_map(touch)
    by_group: dict[str, list[dict]] = {}
    for s in all_servers:
        by_group.setdefault(s["outbound"], []).append(s)

    # Reverse map: outbound -> currently connected server (sub_index, server_id, name)
    current_by_outbound: dict[str, dict] = {}
    for (sub_idx, sid), ob in connected.items():
        # find server name
        match = next((s for s in all_servers
                      if s["sub_index"] == sub_idx and s["server_id"] == sid), None)
        current_by_outbound[ob] = {"sub_index": sub_idx, "server_id": sid,
                                   "name": match["name"] if match else "?"}

    actions = []
    for ob, servers in by_group.items():
        enabled_default = next((e for name, _, e in GROUPING_RULES if name == ob), True)
        if not enabled_default and not args.enable_disabled:
            continue
        current = current_by_outbound.get(ob)
        if not current:
            print(f"[{ob}] no current server — skipping (run 'apply' first)")
            continue
        print(f"\n[{ob}] current: {current['name']}")

        # Build probe list: current + a sample of alternatives.
        alternatives = [s for s in servers
                        if not (s["sub_index"] == current["sub_index"]
                                and s["server_id"] == current["server_id"])]
        # Always probe current + top N alternatives.
        sample = alternatives[:args.sample_size - 1]
        probe_list = [
            {"_type": "subscriptionServer", "id": current["server_id"],
             "sub": current["sub_index"], "outbound": ob}
        ] + [
            {"_type": "subscriptionServer", "id": s["server_id"],
             "sub": s["sub_index"], "outbound": ob}
            for s in sample
        ]
        try:
            results = api.http_latency(probe_list, test_url=args.test_url)
        except RuntimeError as e:
            print(f"  httpLatency failed: {e}")
            continue

        lat_by_key: dict[tuple[int, int], int | None] = {}
        for r in results:
            lat_by_key[(r.get("sub"), r.get("id"))] = parse_latency_ms(r.get("pingLatency"))

        cur_lat = lat_by_key.get((current["sub_index"], current["server_id"]))
        print(f"  current latency: {cur_lat}")
        for s in sample[:5]:
            lat = lat_by_key.get((s["sub_index"], s["server_id"]))
            print(f"    alt {str(lat):>6}  {s['name']}")

        # Decision: switch if current is None OR > threshold OR an alternative is
        # meaningfully better (>= args.min_improvement_pct faster).
        best_alt = None
        best_alt_lat = None
        for s in sample:
            lat = lat_by_key.get((s["sub_index"], s["server_id"]))
            if lat is None:
                continue
            if best_alt_lat is None or lat < best_alt_lat:
                best_alt_lat = lat
                best_alt = s

        should_switch = False
        reason = ""
        if cur_lat is None:
            should_switch = True
            reason = "current timed out"
        elif cur_lat > args.threshold_ms:
            should_switch = True
            reason = f"current {cur_lat}ms > threshold {args.threshold_ms}ms"
        elif best_alt_lat is not None and best_alt_lat < cur_lat * (1 - args.min_improvement_pct / 100):
            # Only switch if alternative is at least min_improvement_pct faster.
            should_switch = True
            reason = f"alt {best_alt_lat}ms is >{args.min_improvement_pct}% faster than current {cur_lat}ms"

        if should_switch and best_alt is not None:
            print(f"  -> switching: {reason}")
            if args.dry_run:
                print(f"[dry-run] would connect {best_alt['name']} ({best_alt_lat}ms) to {ob}")
            else:
                # Switch order: CONNECT the new server FIRST, then disconnect
                # the old one. On the Xray backend, connecting a server into
                # an outbound replaces the previous one, so the old is already
                # gone. Calling disconnect first fails for the `proxy` default
                # outbound because RoutingA depends on it being non-empty.
                try:
                    api.connect({"_type": "subscriptionServer",
                                 "id": best_alt["server_id"],
                                 "sub": best_alt["sub_index"],
                                 "outbound": ob})
                    # If the old server is still connected (rare on Xray but
                    # possible on v2ray-core variant), disconnect it now.
                    # Failure here is non-fatal — the connect already succeeded.
                    if (current["sub_index"], current["server_id"]) != \
                       (best_alt["sub_index"], best_alt["server_id"]):
                        try:
                            api.disconnect({"_type": "subscriptionServer",
                                            "id": current["server_id"],
                                            "sub": current["sub_index"],
                                            "outbound": ob})
                        except RuntimeError as e:
                            # Log but don't treat as error: connect won.
                            print(f"  (disconnect of old server ignored: {e})")
                    print(f"  switched to {best_alt['name']} ({best_alt_lat}ms)")
                    actions.append((ob, current["name"], best_alt["name"], reason))
                except RuntimeError as e:
                    print(f"  ERROR during switch: {e}")
        else:
            print(f"  -> keep current ({reason or 'no better alternative'})")

    if actions:
        print("\n=== Failover actions this run ===")
        for ob, old, new, reason in actions:
            print(f"  [{ob}] {old} -> {new}  ({reason})")
    else:
        print("\nNo failover actions.")
    return 0


def _watch_loop(api: V2rayA, args) -> int:
    print(f"monitor: running every {args.interval}s (threshold={args.threshold_ms}ms, "
          f"min_improvement={args.min_improvement_pct}%)")
    while True:
        try:
            _run_health_check(api, args)
        except Exception as e:
            print(f"health-check iteration error: {e}", file=sys.stderr)
        print(f"\n--- sleeping {args.interval}s ---\n")
        time.sleep(args.interval)


def cmd_setup(api: V2rayA, args) -> int:
    """Apply v2rayA settings: whitelist transparent + system_tun + DoH + RoutingA."""
    print("=== Reading current settings ===")
    cur = api.get_setting()
    print(f"transparent={cur.get('transparent')} type={cur.get('transparentType')} "
          f"antipollution={cur.get('antipollution')} pacMode={cur.get('pacMode')}")

    new = dict(cur)
    # whitelist mode: ALL traffic via proxy UNLESS a RoutingA rule says ->direct
    new["transparent"] = "whitelist"
    new["transparentType"] = "system_tun"     # kernel TUN device
    new["antipollution"] = "doh"              # DNS over HTTPS
    new["pacMode"] = "routingA"               # use RoutingA rules
    new["inboundSniffing"] = "http,tls,quic"  # sniff domain for routing decisions

    if args.dry_run:
        print("\n[dry-run] would PUT setting:")
        print(json.dumps(new, indent=2))
    else:
        print("\n=== Applying settings (transparent=whitelist, type=system_tun, DoH, RoutingA) ===")
        api.put_setting(new)
        print("settings applied")

    # RoutingA rules
    rules = _default_routing_a()
    if args.dry_run:
        print("\n[dry-run] would PUT routingA:")
        print(rules)
    else:
        print("\n=== Applying RoutingA rules ===")
        api.put_routing_a(rules)
        print("routingA applied")
    return 0


def _default_routing_a() -> str:
    # whitelist semantics: default is `proxy` (everything via VPN); explicit
    # rules carve out exceptions that go `direct` (bypass VPN).
    # NOTE: RoutingA parser takes ONE argument per domain()/ip() call.
    # Multiple targets need separate rules.
    return r"""default: proxy

# --- bypass VPN: local/private ---
ip(geoip:private)->direct

# --- bypass VPN: Russian GeoIP/Site ---
# These go direct so RU services see your real IP and stay fast.
domain(geosite:category-ru)->direct
domain(geosite:yandex)->direct
domain(geosite:vk)->direct
domain(geosite:mailru)->direct
ip(geoip:ru)->direct

# --- bypass VPN: China (rarely needs VPN; saves bandwidth) ---
domain(geosite:cn)->direct
ip(geoip:cn)->direct

# --- block: ads & tracking ---
domain(geosite:category-ads-all)->block

# --- streaming: route to eu_plus (premium pool) ---
domain(geosite:netflix)->eu_plus
domain(geosite:disney)->eu_plus
domain(geosite:hbo)->eu_plus
domain(geosite:hulu)->eu_plus
domain(geosite:youtube)->eu_plus
"""


def main() -> int:
    p = argparse.ArgumentParser(description="v2rayA grouping + health-check helper")
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:2017"))
    p.add_argument("--username", default=os.environ.get("USERNAME"))
    p.add_argument("--password", default=os.environ.get("PASSWORD"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("status", help="print current state and exit")

    pa = sub.add_parser("apply", help="create outbounds + connect best server per group")
    pa.add_argument("--refresh-sub", action="store_true", help="refresh subscription first")
    pa.add_argument("--enable-disabled", action="store_true",
                    help="also connect servers in disabled-by-default groups (e.g. ru)")
    pa.add_argument("--sample-size", type=int, default=10,
                    help="max servers per group to latency-test (default 10, raise to probe more)")
    pa.add_argument("--test-url", default=LATENCY_TEST_URL)

    pm = sub.add_parser("monitor", help="health-check active servers, failover if degraded")
    pm.add_argument("--watch", action="store_true", help="run forever, sleep --interval between checks")
    pm.add_argument("--interval", type=int, default=300, help="seconds between checks in --watch (default 300)")
    pm.add_argument("--threshold-ms", type=int, default=2000,
                    help="switch if active server latency exceeds this (default 2000)")
    pm.add_argument("--min-improvement-pct", type=float, default=30,
                    help="switch only if alternative is at least this percent faster (default 30)")
    pm.add_argument("--enable-disabled", action="store_true")
    pm.add_argument("--sample-size", type=int, default=10)
    pm.add_argument("--test-url", default=LATENCY_TEST_URL)

    pc = sub.add_parser("setup", help="apply v2rayA settings: TUN + DoH + RoutingA")

    pb = sub.add_parser("balancer", help="generate multi-server balancer overlay for core-hook")
    pb.add_argument("--max-servers", type=int, default=6,
                    help="max servers per balancer group (default 6)")
    pb.add_argument("--install", action="store_true",
                    help="copy overlay to /etc/v2raya/balancer-overlay.json (needs sudo)")

    args = p.parse_args()

    if not args.username or not args.password:
        print("ERROR: set USERNAME and PASSWORD in .env", file=sys.stderr)
        return 2

    api = V2rayA(args.base_url, args.username, args.password, verbose=args.verbose)
    try:
        api.login()
    except RuntimeError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1

    if args.cmd == "status":
        return cmd_status(api)
    if args.cmd == "apply":
        return cmd_apply(api, args)
    if args.cmd == "monitor":
        return cmd_monitor(api, args)
    if args.cmd == "setup":
        return cmd_setup(api, args)
    if args.cmd == "balancer":
        from balancer import cmd_generate_balancer
        return cmd_generate_balancer(classify, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())

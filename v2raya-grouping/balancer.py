"""
Xray balancer config generator.

Parses vless:// URIs from a subscription into Xray outbound objects,
groups them, and produces a config overlay that a v2rayA core-hook
can apply to add multi-server balancing.

vless:// URI format:
  vless://<uuid>@<host>:<port>?<params>#<name>

Relevant query params:
  type       = tcp | grpc | ws | ...
  encryption = none
  security   = reality
  pbk        = REALITY public key (base64)
  sid        = shortId (hex)
  sni        = serverName for TLS SNI
  fp         = fingerprint (chrome, firefox, ...)
  flow       = xtls-rprx-vision (tcp only)
  serviceName= gRPC service name (grpc only)
  path       = WebSocket path
  host       = HTTP Host header
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# vless:// parser
# ---------------------------------------------------------------------------

def parse_vless_uri(uri: str) -> dict[str, Any]:
    """Parse a vless:// URI into a structured dict. Raises ValueError on failure."""
    if not uri.startswith("vless://"):
        raise ValueError("not a vless:// URI")

    # strip prefix
    rest = uri[len("vless://"):]

    # extract fragment (#name)
    fragment = ""
    if "#" in rest:
        rest, fragment = rest.rsplit("#", 1)
        fragment = urllib.parse.unquote_plus(fragment)

    # split at @ to get userinfo and host:port?params
    userinfo, host_port_params = rest.split("@", 1)
    # userinfo is just the UUID (can contain hyphens)
    uuid = userinfo.strip()

    # host:port?params
    host_port, _, params_str = host_port_params.partition("?")
    host, _, port_str = host_port.rpartition(":")
    host = host.strip().strip("[]")  # remove brackets from IPv6
    port = int(port_str)

    # parse query params
    params: dict[str, str] = {}
    if params_str:
        for part in params_str.split("&"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            params[k.strip()] = urllib.parse.unquote_plus(v.strip())

    return {
        "_uri": uri,
        "uuid": uuid,
        "host": host,
        "port": port,
        "fragment": fragment,
        "params": params,
    }


# ---------------------------------------------------------------------------
# vless URI -> Xray outbound JSON
# ---------------------------------------------------------------------------

def vless_to_xray_outbound(parsed: dict[str, Any], tag: str) -> dict[str, Any]:
    """
    Convert a parsed vless URI into a complete Xray outbound object.
    Matching the structure v2rayA uses so the config looks familiar.
    """
    uuid = parsed["uuid"]
    host = parsed["host"]
    port = parsed["port"]
    p = parsed["params"]

    network = p.get("type", "tcp")
    security = p.get("security", "none")
    encryption = p.get("encryption", "none")
    fingerprint = p.get("fp", "chrome")
    sni = p.get("sni", "")
    pbk = p.get("pbk", "")
    sid = p.get("sid", "")
    flow = p.get("flow", "")
    service_name = p.get("serviceName", "")
    path = p.get("path", "")

    # --- vnext (vless protocol settings) ---
    settings: dict[str, Any] = {
        "vnext": [{
            "address": host,
            "port": port,
            "users": [{
                "id": uuid,
                "encryption": encryption,
            }],
        }],
    }
    if flow and network == "tcp":
        settings["vnext"][0]["users"][0]["flow"] = flow

    # --- streamSettings ---
    stream: dict[str, Any] = {
        "network": network,
        "security": security,
    }
    # TLS/REALITY
    if security == "reality":
        stream["realitySettings"] = {
            "fingerprint": fingerprint,
            "serverName": sni,
            "publicKey": pbk,
            "shortId": sid,
            "spiderX": "",
        }
    elif security == "tls":
        stream["tlsSettings"] = {
            "serverName": sni,
        }

    # Transport-specific
    if network == "grpc":
        stream["grpcSettings"] = {"serviceName": service_name}
    elif network == "ws":
        stream["wsSettings"] = {
            "path": path,
            "headers": {},
        }
    elif network == "tcp":
        if flow and "http" not in flow:
            stream["tcpSettings"] = {"header": {"type": "none"}}
        else:
            stream["tcpSettings"] = {}
    elif network == "kcp":
        stream["kcpSettings"] = {
            "mtu": 1350,
            "tti": 50,
            "uplinkCapacity": 5,
            "downlinkCapacity": 20,
            "congestion": False,
            "readBufferSize": 2,
            "writeBufferSize": 2,
        }

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": settings,
        "streamSettings": stream,
    }


# ---------------------------------------------------------------------------
# Subscription fetcher (parses base64 subscription body)
# ---------------------------------------------------------------------------

def fetch_vless_uris(subscription_url: str | None = None, fetcher_url: str = "http://127.0.0.1:8798/sub") -> list[str]:
    """
    Fetch the current subscription and decode into individual vless:// URIs.
    If subscription_url is None, uses the local fetcher at fetcher_url.
    """
    import urllib.request

    url = subscription_url or fetcher_url
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read().decode()

    import base64
    decoded = base64.b64decode(body).decode("utf-8", errors="replace")
    uris = [line.strip() for line in decoded.splitlines() if line.strip().startswith("vless://")]
    return uris


# ---------------------------------------------------------------------------
# Balancer overlay generation
# ---------------------------------------------------------------------------

CACHE_DIR = Path(os.path.expanduser("~/.cache/v2raya-balancer"))
CACHE_FILE = CACHE_DIR / "groups.json"
OVERLAY_FILE = CACHE_DIR / "overlay.json"
SYSTEM_OVERLAY = "/etc/v2raya/balancer-overlay.json"


def generate_balancer_overlay(
    classify_func,
    max_servers_per_group: int = 6,
) -> dict[str, Any]:
    """
    Fetch the subscription, classify servers into groups, and generate a
    config overlay that:
      - adds extra outbounds (one per server beyond the first)
      - adds balancer entries referencing all outbounds
      - adds observatory entries for leastping

    The overlay IS NOT a complete config — it's merged into v2rayA's
    generated config by the core-hook.

    Returns the overlay AND saves a cache to CACHE_FILE.
    """
    uris = fetch_vless_uris()
    parsed = []
    for uri in uris:
        try:
            parsed.append(parse_vless_uri(uri))
        except ValueError:
            continue

    # Group by classify() on the fragment (name)
    groups: dict[str, list[dict]] = {}
    for p in parsed:
        name = p.get("fragment", "")
        outbound, enabled = classify_func(name)
        if not enabled:
            continue
        groups.setdefault(outbound, []).append(p)

    # Sort each group by... we can't easily latency-test at this stage,
    # but we can prioritise tcp over grpc (tcp tends to be more stable)
    # and limit to max_servers_per_group.
    overlay_outbounds: list[dict] = []
    balancers: list[dict] = []
    observers: list[dict] = []

    cache: dict[str, list[dict]] = {}

    for group_name, servers in groups.items():
        if len(servers) <= 1:
            continue  # only 1 server — nothing to balance

        # Cap to top N
        limited = servers[:max_servers_per_group]
        tags: list[str] = []

        for i, s in enumerate(limited):
            tag = f"{group_name}_{i}"
            outbound = vless_to_xray_outbound(s, tag)
            overlay_outbounds.append(outbound)
            tags.append(tag)

        balancers.append({
            "tag": group_name,
            "selector": tags,
            "strategy": {
                "type": "leastping",
                "settings": {
                    "observerTag": group_name,
                },
            },
        })
        observers.append({
            "tag": group_name,
            "settings": {
                "subjectSelector": tags,
                "probeURL": "https://www.gstatic.com/generate_204",
                "probeInterval": "5m0s",
            },
        })

        cache[group_name] = [
            {"tag": tag, "host": s["host"], "port": s["port"], "name": s.get("fragment", "")}
            for tag, s in zip(tags, limited)
        ]

    overlay = {
        "outbounds": overlay_outbounds,
        "balancers": balancers,
        "observers": observers,
    }

    # Save cache AND overlay
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    OVERLAY_FILE.write_text(json.dumps(overlay, indent=2))

    return overlay


# ---------------------------------------------------------------------------
# Config merger (used by the core-hook)
# ---------------------------------------------------------------------------

def apply_overlay_to_config(
    v2raya_config: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge the generated overlay into v2rayA's config.

    Steps:
    1. Add overlay outbounds to config.outbounds
    2. Add overlay balancers to config.routing.balancers
    3. Add overlay observers to config.multiObservatory.observers
    4. For each routing rule whose outboundTag matches a balancer tag,
       REPLACE outboundTag with balancerTag (Xray uses balancerTag to
       route through a balancer instead of a direct outbound).
    """
    import copy
    config = copy.deepcopy(v2raya_config)

    # 1. Add outbounds
    if "outbounds" not in config or config["outbounds"] is None:
        config["outbounds"] = []
    existing_tags = {o["tag"] for o in config["outbounds"] if "tag" in o}
    for ob in overlay.get("outbounds", []):
        if ob["tag"] not in existing_tags:
            config["outbounds"].append(ob)

    # 2. Ensure routing section
    if "routing" not in config or config["routing"] is None:
        config["routing"] = {}
    routing = config["routing"]

    # 3. Add balancers — prepend original outbound tag to selector.
    overlay_balancer_tags = set()
    if "balancers" not in routing or routing["balancers"] is None:
        routing["balancers"] = []
    for bal in overlay.get("balancers", []):
        tag = bal["tag"]
        overlay_balancer_tags.add(tag)
        if tag in existing_tags and tag not in bal.get("selector", []):
            bal = dict(bal)
            bal["selector"] = [tag] + bal.get("selector", [])
        routing["balancers"].append(bal)

    # 4. Rewrite routing rules: outboundTag -> balancerTag for balancer groups
    if "rules" in routing and routing["rules"]:
        for rule in routing["rules"]:
            ot = rule.get("outboundTag")
            if ot in overlay_balancer_tags:
                del rule["outboundTag"]
                rule["balancerTag"] = ot

    # 5. Add observatory — prepend original tag to subjectSelector.
    obs = overlay.get("observers", [])
    if obs:
        if "observatory" not in config or config["observatory"] is None:
            config["observatory"] = {}
        top_obs = config["observatory"]
        if "observers" not in top_obs or top_obs["observers"] is None:
            top_obs["observers"] = []
        for o in obs:
            tag = o["tag"]
            sel = o.get("settings", {}).get("subjectSelector", o.get("subjectSelector", []))
            if tag in existing_tags and tag not in sel:
                o = dict(o)
                o["settings"] = dict(o.get("settings", {}))
                o["settings"]["subjectSelector"] = [tag] + sel
            top_obs["observers"].append(o)

        # Observers need ObservatoryService in api.services
        if "api" in config and config["api"] is not None:
            services = config["api"].get("services", [])
            if isinstance(services, list) and "ObservatoryService" not in services:
                config["api"]["services"] = list(services) + ["ObservatoryService"]

    return config


# ---------------------------------------------------------------------------
# CLI entry point (called from v2raya-grouping.py)
# ---------------------------------------------------------------------------

def cmd_generate_balancer(classify_func, args=None) -> int:
    """
    CLI: fetch subscription, classify, generate balancer overlay,
    save cache. Safe to run while VPN is up — does not modify v2rayA state.

    With --install: copies the overlay to SYSTEM_OVERLAY (needs sudo).
    """
    print("Fetching subscription URIs ...")
    uris = fetch_vless_uris()
    print(f"  {len(uris)} URIs fetched")

    parsed = []
    for u in uris:
        try:
            parsed.append(parse_vless_uri(u))
        except ValueError as e:
            print(f"  skip malformed: {e}")
    print(f"  {len(parsed)} parsed successfully")

    # Group
    groups: dict[str, list[dict]] = {}
    for p in parsed:
        name = p.get("fragment", "")
        ob, enabled = classify_func(name)
        if not enabled:
            continue
        groups.setdefault(ob, []).append(p)

    for name, srvs in sorted(groups.items()):
        print(f"  {name:10s} {len(srvs):4d} servers")

    max_per_group = getattr(args, "max_servers", 6) if args else 6
    overlay = generate_balancer_overlay(classify_func, max_servers_per_group=max_per_group)

    print(f"\nBalancer overlay generated:")
    print(f"  extra outbounds: {len(overlay['outbounds'])}")
    print(f"  balancer groups: {len(overlay['balancers'])}")
    print(f"  observers:       {len(overlay['observers'])}")
    print(f"  local overlay:   {OVERLAY_FILE}")

    for bal in overlay.get("balancers", []):
        print(f"  [{bal['tag']}] {len(bal['selector'])} servers: {', '.join(bal['selector'][:5])}")

    # Install to system location if requested
    install = getattr(args, "install", False) if args else False
    if install:
        print(f"\nInstalling overlay to {SYSTEM_OVERLAY} (sudo may be needed) ...")
        import subprocess
        overlay_json = json.dumps(overlay, indent=2)
        try:
            # Use sudo tee to write as root
            r = subprocess.run(
                ["sudo", "tee", SYSTEM_OVERLAY],
                input=overlay_json.encode(), capture_output=True, timeout=10)
            if r.returncode == 0:
                print(f"  installed: {SYSTEM_OVERLAY}")
            else:
                print(f"  sudo failed: {r.stderr.decode().strip()}")
                print(f"  Try manually: sudo cp {OVERLAY_FILE} {SYSTEM_OVERLAY}")
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  install error: {e}")
            print(f"  Try manually: sudo cp {OVERLAY_FILE} {SYSTEM_OVERLAY}")

    return 0

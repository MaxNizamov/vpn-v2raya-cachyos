#!/usr/bin/env python3
"""
v2rayA core-hook — multi-server balancer injection.

Called by v2rayA at xray lifecycle stages (--stage pre-start|post-start|...).

At pre-start:
  1. Reads /etc/v2raya/config.json (v2rayA's generated config)
  2. Reads pre-generated balancer overlay from ~/.cache/v2raya-balancer/overlay.json
  3. Merges them: adds extra outbounds + balancers + observatory
  4. Writes modified config back

If the overlay cache is missing or outdated, exits 0 (pass-through).

To set up:
  python3 v2raya-grouping.py balancer          # generates the overlay cache
  sudo ln -sf $PWD/balancer-hook.py /etc/v2raya/core-hook
  # add V2RAYA_CORE_HOOK=/etc/v2raya/core-hook to /etc/default/v2raya

Config path: /etc/v2raya/config.json
Overlay path: ~/.cache/v2raya-balancer/overlay.json (as root — /root/.cache/...)
"""

import argparse
import json
import os
import sys
from pathlib import Path


CONFIG_PATH = "/etc/v2raya/config.json"
# Overlay is placed here by `v2raya-grouping.py balancer --install`.
OVERLAY_PATH = "/etc/v2raya/balancer-overlay.json"


def apply_overlay(config: dict, overlay: dict) -> dict:
    """Merge balancer overlay into v2rayA's xray config."""
    import copy
    config = copy.deepcopy(config)

    # 1. Add extra outbounds
    if "outbounds" not in config or config["outbounds"] is None:
        config["outbounds"] = []
    existing_tags = {o["tag"] for o in config["outbounds"] if "tag" in o}
    for ob in overlay.get("outbounds", []):
        if ob["tag"] not in existing_tags:
            config["outbounds"].append(ob)
            existing_tags.add(ob["tag"])

    # 2. Routing section
    if "routing" not in config or config["routing"] is None:
        config["routing"] = {}

    # 3. Add balancers — also prepend the original v2rayA outbound tag to the
    #    selector so the balancer uses ALL outbounds (original + extras).
    routing = config["routing"]
    if "balancers" not in routing or routing["balancers"] is None:
        routing["balancers"] = []
    balancer_tags = {b["tag"] for b in overlay.get("balancers", [])}
    for b in overlay.get("balancers", []):
        # If the original config already has an outbound with this group name,
        # prepend it to the selector (it's the v2rayA-managed server).
        if b["tag"] in existing_tags and b["tag"] not in b.get("selector", []):
            b = dict(b)
            b["selector"] = [b["tag"]] + b["selector"]
        routing["balancers"].append(b)

    # 4. Rewrite routing rules: outboundTag -> balancerTag for balancer groups
    for rule in routing.get("rules", []) or []:
        ot = rule.get("outboundTag")
        if ot in balancer_tags:
            del rule["outboundTag"]
            rule["balancerTag"] = ot

    # 5. Add observatory — also prepend original tag to subjectSelector.
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

        # CRITICAL: observatory requires ObservatoryService in the API
        # services list, otherwise xray fails with "not all dependencies
        # are resolved". v2rayA only adds ObservatoryService when it
        # generates its own observatory entries (which don't exist for
        # single-server outbounds), so we must add it here.
        if "api" in config and config["api"] is not None:
            services = config["api"].get("services", [])
            if isinstance(services, list) and "ObservatoryService" not in services:
                services = list(services) + ["ObservatoryService"]
                config["api"]["services"] = services

    return config


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--stage", required=True,
                   choices=["pre-start", "post-start", "pre-stop", "post-stop"])
    args, _ = p.parse_known_args()  # ignore extra args v2rayA may pass

    if args.stage != "pre-start":
        return 0

    if not os.path.exists(CONFIG_PATH):
        print(f"balancer-hook: {CONFIG_PATH} not found, skip", flush=True)
        return 0

    if not Path(OVERLAY_PATH).exists():
        print(f"balancer-hook: {OVERLAY_PATH} not found, pass-through. "
              f"Run 'v2raya-grouping.py balancer --install' to generate.", flush=True)
        return 0

    # Read overlay
    try:
        overlay = json.loads(Path(OVERLAY_PATH).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"balancer-hook: cannot read overlay: {e}, pass-through", flush=True)
        return 0

    if not overlay.get("balancers"):
        print("balancer-hook: overlay has no balancers, pass-through", flush=True)
        return 0

    # Read v2rayA config
    try:
        original = json.loads(Path(CONFIG_PATH).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"balancer-hook: cannot read config: {e}, pass-through", flush=True)
        return 0

    # Apply
    try:
        modified = apply_overlay(original, overlay)
    except Exception as e:
        print(f"balancer-hook: merge failed: {e}, pass-through", flush=True)
        return 0

    # Backup + write
    backup = CONFIG_PATH + ".bak"
    try:
        Path(backup).write_text(json.dumps(original, indent=2))
        Path(CONFIG_PATH).write_text(json.dumps(modified, indent=2))
    except OSError as e:
        print(f"balancer-hook: write failed: {e}", flush=True)
        # Restore backup if we partially wrote
        try:
            if Path(backup).exists():
                Path(backup).rename(CONFIG_PATH)
        except OSError:
            pass
        return 0

    print(f"balancer-hook: +{len(overlay['outbounds'])} outbounds, "
          f"+{len(overlay['balancers'])} balancers, "
          f"backup={backup}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

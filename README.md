# vpn-v2raya-cachyos

Personal VPN setup on **CachyOS (Arch Linux)** using
[v2rayA](https://github.com/v2rayA/v2rayA) + **Xray-core**, with:

- **REALITY** proxy servers (from a subscription that filters by `User-Agent`)
- A **local subscription fetcher** that adds the right `User-Agent` so v2rayA
  can import the feed
- A **grouping + health-check helper** that auto-picks the best server per
  outbound group and switches when a server degrades
- **TUN-mode transparent proxy**, **DoH**, and **RoutingA** rules
  (whitelist: everything via VPN except GeoIP/Site bypass)

Tested on:
- CachyOS (Arch), kernel 7.1.x, x86_64
- v2rayA 2.2.7.5 + Xray-core 26.3.27
- A subscription from `sub.skippnet.com` (skippnet — adjust to your provider)

## Architecture

```
                  ┌─────────────────────────────────┐
   subscription   │ v2raya-sub-fetcher              │
   server ──────> │ :8798  (User-Agent: Hiddify)    │ ─── v2rayA subscribes to
                  │ systemd user service            │      http://127.0.0.1:8798/sub
                  └─────────────────────────────────┘

                  ┌─────────────────────────────────┐
                  │ v2rayA (:2017 web UI)           │
                  │  Xray backend, TUN mode, DoH    │
   traffic  ────> │  RoutingA: whitelist            │ <─── OS routes via tun0
                  │  Outbounds: proxy eu eu_plus wl │
                  └─────────────────────────────────┘
                                ▲
                                │ status / httpLatency / connect / disconnect
                                │
                  ┌─────────────────────────────────┐
                  │ v2raya-grouping (helper)        │
                  │  - classify servers by name     │
                  │  - pick best per group          │
                  │  - health-check every 5 min     │
                  │  - auto failover if degraded    │
                  │ systemd user service (monitor)  │
                  └─────────────────────────────────┘
```

## Repository layout

```
.
├── v2raya.default                       # /etc/default/v2raya (Xray backend config)
├── v2raya-install-system.sh             # installs v2raya.default, enables system service
├── happ-uninstall.sh                    # removes a previous Happ VPN install
├── linux-networking-101.md              # general Linux networking reference
│
├── v2raya-sub-fetcher/                  # UA-spoofing subscription proxy
│   ├── sub-fetcher.py
│   ├── v2raya-sub-fetcher.service
│   ├── install.sh
│   └── README.md
│
└── v2raya-grouping/                     # grouping + health-check helper
    ├── v2raya-grouping.py
    ├── v2raya-monitor.service
    ├── install.sh
    ├── .env.example                     # copy to .env, fill in v2rayA creds
    └── README.md (this project's main docs are here)
```

## Setup

### Prerequisites (CachyOS / Arch)

```bash
# v2rayA itself (AUR) + Xray backend
yay -S --needed xray-bin v2raya

# v2rayA also pulls in `v2ray` from extra — its geoip.dat / geosite.dat
# are reused by Xray (xray-bin does not ship assets).
```

### 1. Configure v2rayA system service (root)

```bash
git clone https://github.com/MaxNizamov/vpn-v2raya-cachyos
cd vpn-v2raya-cachyos
./v2raya-install-system.sh
```

This installs `/etc/default/v2raya` (pointing `V2RAYA_V2RAY_BIN` at Xray) and
enables `v2raya.service`. Open http://127.0.0.1:2017 and create the admin
account.

### 2. Subscription fetcher (no root)

If your provider filters subscriptions by `User-Agent`, install the local
fetcher:

```bash
cd v2raya-sub-fetcher
./install.sh
```

Edit the installed unit to point at your subscription URL:

```bash
systemctl --user edit v2raya-sub-fetcher
# add:
# [Service]
# Environment=UPSTREAM_URL=https://your-provider.example.com/sub/<uuid>
```

In v2rayA's web UI, add a subscription with address
`http://127.0.0.1:8798/sub` and update it.

### 3. Grouping + health-check helper (no root)

```bash
cd v2raya-grouping
cp .env.example .env
# edit .env: USERNAME, PASSWORD (your v2rayA admin creds), BASE_URL
chmod 600 .env
```

Run the helper once to classify servers, create outbound groups, and pick the
best server per group:

```bash
python3 v2raya-grouping.py apply
```

Apply the TUN + DoH + RoutingA configuration:

```bash
python3 v2raya-grouping.py setup
```

Start the proxy from the v2rayA web UI (top-left "Ready" button), then install
the health-check monitor so it auto-failovers:

```bash
./install.sh
```

## Grouping rules

Servers are matched to outbound groups by display name (first match wins).
Adjust `GROUPING_RULES` in `v2raya-grouping.py` to match your provider's
naming.

Default rules (tuned for skippnet's `Hiddify` UA output):

| Name pattern                         | Outbound  | Enabled |
|--------------------------------------|-----------|---------|
| `⚡ Авто · N` / `⚡ Авто+ · N`        | `proxy`   | yes (default) |
| `🌩 Белые списки | …` / `[Быстрый]…` | `wl`      | yes |
| `🇷🇺 Россия · N` (without wl prefix) | `ru`      | **no** |
| `<Country>+ · N`                     | `eu_plus` | yes |
| `<Country> · N`                      | `eu`      | yes |

**Important:** on the **Xray backend**, v2rayA allows only **one server per
outbound** (each new connect replaces the previous one). So each group holds
its single best server, and the monitor handles failover by switching groups
to a better server when latency degrades. Real multi-server load balancing
would require the v2ray-core backend, which lacks REALITY/XTLS support.

## RoutingA (whitelist)

`v2raya-grouping.py setup` writes these RoutingA rules (whitelist semantics:
**default is `proxy`**; explicit rules carve out bypass exceptions):

- `ip(geoip:private) -> direct` — LAN/local
- `domain(geosite:category-ru/yandex/vk/mailru) -> direct` — RU stays direct
- `ip(geoip:ru) -> direct`
- `domain(geosite:cn) -> direct`, `ip(geoip:cn) -> direct`
- `domain(geosite:category-ads-all) -> block`
- `domain(geosite:netflix/disney/hbo/hulu/youtube) -> eu_plus` — premium pool
- everything else → `proxy`

RoutingA's parser only accepts `[A-Za-z0-9_]` in outbound names — that's why
the premium group is `eu_plus`, not `eu-plus`.

## Commands

```bash
# Current state
python3 v2raya-grouping/v2raya-grouping.py status

# Re-pick best server per group (after subscription refresh)
python3 v2raya-grouping/v2raya-grouping.py apply

# One-shot health check + failover
python3 v2raya-grouping/v2raya-grouping.py monitor

# Apply TUN + DoH + RoutingA (idempotent)
python3 v2raya-grouping/v2raya-grouping.py setup

# Force-refresh subscription cache
curl http://127.0.0.1:8798/force-refresh

# Tail monitor logs
journalctl --user -u v2raya-monitor -f
```

## Tunables (systemd overrides)

```bash
# Monitor: switch faster, sample more servers, etc.
systemctl --user edit v2raya-monitor
# [Service]
# Environment=V2RAYA_THRESHOLD_MS=1500
# Environment=V2RAYA_MIN_IMPROVEMENT_PCT=25
# Environment=V2RAYA_SAMPLE_SIZE=12

# Fetcher: change upstream or UA
systemctl --user edit v2raya-sub-fetcher
# [Service]
# Environment=UPSTREAM_URL=https://your-provider/sub/<uuid>
# Environment=USER_AGENT=Hiddify
```

## What's NOT supported (and why)

- **Multi-server load balancing on Xray backend** — v2rayA's Xray variant
  replaces the server in an outbound on each connect. The `balancer` with
  `leastping` strategy requires v2ray-core, which lacks REALITY. Mitigation:
  one best server per group + auto-failover monitor.
- **Real multi-server bonding (parallel use of 2+ servers for throughput)** —
  not possible with v2rayA+Xray. Each TCP connection is pinned to one
  outbound; MPTCP-style bonding is out of scope for Xray.

## Cleanup (previous Happ install)

If you are migrating from Happ, `happ-uninstall.sh` removes the `/opt/happ`
installation, the four systemd units, and user-level artifacts. Run it from a
sudo-capable terminal; it asks for confirmation before deleting anything.

## License

Personal configuration; no explicit license. Standard "use at your own risk"
applies. The subscription URL, server credentials, and v2rayA admin password
are **yours** — none are included in this repo.

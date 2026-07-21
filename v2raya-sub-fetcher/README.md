# v2rayA Subscription Fetcher

Tiny local HTTP proxy that lets v2rayA import a subscription from a
provider which filters by `User-Agent` (e.g. skippnet.com, which returns
HTTP 445 / a `0.0.0.0:0` placeholder node for non-browser UAs).

It listens on `127.0.0.1:8798`, fetches the upstream subscription with
`User-Agent: Hiddify`, and forwards the body. v2rayA points its
subscription at `http://127.0.0.1:8798/sub` and everything works —
including auto-refresh.

## Why

The subscription provider `sub.skippnet.com` rejects non-browser clients
(HTTP 445 with a help message). Testing UAs from their allowlist:

| UA           | Result                                         |
|--------------|------------------------------------------------|
| `v2rayN`     | 1 placeholder node (`0.0.0.0:0`, device limit) |
| `V2Box`      | 1 placeholder node                             |
| `NekoBox`    | 1 placeholder node                             |
| `Happ`       | HTTP 446 (no body)                             |
| `Hiddify`    | **141 REALITY servers** ✅                      |
| `HiddifyClash` | 141 REALITY servers                          |

`Hiddify` returns the full list without claiming a device slot.

v2rayA sends its own UA, so we proxy through this fetcher.

## Install (user systemd unit — no root)

```bash
git clone https://github.com/MaxNizamov/vpn-v2raya-cachyos
cd vpn-v2raya-cachyos/v2raya-sub-fetcher
./install.sh
```

`install.sh` rewrites the `%h/Dev/vpn-v2raya-cachyos/...` path in the unit
file to wherever you cloned the repo, then installs + enables + starts it.

## Configure v2rayA

1. Open http://127.0.0.1:2017
2. **Servers** → **Import** → **Subscription**
3. Address: `http://127.0.0.1:8798/sub`
4. Update — the 141 nodes should appear.

## Endpoints

| Endpoint         | Purpose                              |
|------------------|--------------------------------------|
| `GET /sub`       | Subscription body (proxied + cached) |
| `GET /healthz`   | Liveness probe → `ok`                |
| `GET /cache`     | Last successful body                 |
| `GET /force-refresh` | Re-fetch upstream immediately    |

Response header `X-Source` is `upstream` or `cache` so you can tell.

## Configuration

All knobs are environment variables (or CLI flags). Edit the systemd
unit (`systemctl --user edit v2raya-sub-fetcher`) and override:

```ini
[Service]
Environment=UPSTREAM_URL=https://sub.skippnet.com/sub/<your-uuid>
Environment=USER_AGENT=Hiddify
Environment=LISTEN_PORT=8798
Environment=MIN_REFRESH_INTERVAL=60
Environment=FETCH_TIMEOUT=20
Environment=LOG_LEVEL=INFO
```

Defaults baked into `sub-fetcher.py::DEFAULTS`.

## Behaviour

- **Cache:** every successful fetch is saved to
  `~/.local/share/v2raya-sub-fetcher/last_sub.txt`.
- **Failure:** if upstream is unreachable / returns 5xx, the last good
  body is served from cache so v2rayA keeps working.
- **Rate limiting:** consecutive `/sub` requests within
  `MIN_REFRESH_INTERVAL` seconds are served from cache. Use
  `/force-refresh` to bypass.

## Manage

```bash
systemctl --user status  v2raya-sub-fetcher
systemctl --user restart v2raya-sub-fetcher
journalctl --user -u v2raya-sub-fetcher -f
```

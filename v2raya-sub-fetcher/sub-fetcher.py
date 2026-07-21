#!/usr/bin/env python3
"""
v2rayA subscription fetcher.

Solves the case where a subscription provider filters by User-Agent and
returns HTTP 445 / a placeholder node for non-browser clients. We expose a
tiny local HTTP endpoint that fetches the upstream subscription with the
required User-Agent and forwards the body to v2rayA.

Run as a user systemd service (no root needed; binds to 127.0.0.1).

Endpoint:
    GET  /sub                  -> raw subscription body (cached + proxied)
    GET  /healthz              -> "ok" if the upstream is reachable
    GET  /cache                -> last successfully fetched body

Cache: every successful fetch is written to --cache-file and served on
upstream failures (5xx / network errors), so v2rayA keeps working even
when the subscription server is temporarily down.

CLI flags override environment variables of the same name (uppercased,
dashes -> underscores).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("sub-fetcher")

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": "8798",
    # Replace with your own subscription URL (set via env var UPSTREAM_URL
    # in the systemd unit, OR here if you fork the repo for personal use).
    "upstream_url": "",
    "user_agent": "Hiddify",
    "cache_file": os.path.expanduser("~/.local/share/v2raya-sub-fetcher/last_sub.txt"),
    "fetch_timeout": "20",
    "min_refresh_interval": "60",  # do not hammer upstream more often than this
}


def env_flag(name: str, default: str) -> str:
    """Read a config value from environment, with a default."""
    return os.environ.get(name.upper(), default)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="v2rayA subscription fetcher proxy")
    p.add_argument("--host", default=env_flag("listen_host", DEFAULTS["listen_host"]))
    p.add_argument("--port", type=int, default=int(env_flag("listen_port", DEFAULTS["listen_port"])))
    p.add_argument("--upstream-url", default=env_flag("upstream_url", DEFAULTS["upstream_url"]))
    p.add_argument("--user-agent", default=env_flag("user_agent", DEFAULTS["user_agent"]))
    p.add_argument("--cache-file", default=env_flag("cache_file", DEFAULTS["cache_file"]))
    p.add_argument("--fetch-timeout", type=int,
                   default=int(env_flag("fetch_timeout", DEFAULTS["fetch_timeout"])))
    p.add_argument("--min-refresh-interval", type=int,
                   default=int(env_flag("min_refresh_interval",
                                        DEFAULTS["min_refresh_interval"])))
    p.add_argument("--log-level", default=env_flag("log_level", "INFO"))
    return p.parse_args()


class Fetcher:
    """Holds upstream state + cache. Thread-safe enough for one upstream."""

    def __init__(self, opts: argparse.Namespace):
        self.upstream_url = opts.upstream_url
        self.user_agent = opts.user_agent
        self.fetch_timeout = opts.fetch_timeout
        self.min_refresh = opts.min_refresh_interval
        self.cache_file = Path(opts.cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._last_fetch_ts: float = 0.0
        self._last_body: bytes = b""
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            self._last_body = self.cache_file.read_bytes()
            self._last_fetch_ts = self.cache_file.stat().st_mtime
            LOG.info("Loaded cache: %d bytes, mtime=%s",
                     len(self._last_body), time.ctime(self._last_fetch_ts))
        except FileNotFoundError:
            LOG.info("No cache file yet at %s", self.cache_file)

    def _save_cache(self, body: bytes) -> None:
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(self.cache_file)
        self._last_body = body
        self._last_fetch_ts = time.time()

    def fetch_upstream(self) -> tuple[int, bytes]:
        """Return (status_code, body). Raises on network errors."""
        req = urllib.request.Request(
            self.upstream_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.fetch_timeout) as resp:
            return resp.status, resp.read()

    def get(self, force: bool = False) -> tuple[int, bytes, str]:
        """
        Return (status_code, body, source) where source is
        'upstream' | 'cache' | 'error'.
        Respects min_refresh_interval unless force=True.
        """
        now = time.time()
        age = now - self._last_fetch_ts
        if not force and age < self.min_refresh and self._last_body:
            return 200, self._last_body, "cache"

        try:
            status, body = self.fetch_upstream()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            LOG.warning("Upstream fetch failed: %r", e)
            if self._last_body:
                return 200, self._last_body, "cache"
            return 502, b"upstream error\n", "error"

        if status != 200 or not body:
            LOG.warning("Upstream returned status=%s, len=%d", status, len(body))
            if self._last_body:
                return 200, self._last_body, "cache"
            return status, body, "error"

        self._save_cache(body)
        LOG.info("Fetched %d bytes from upstream", len(body))
        return 200, body, "upstream"


class Handler(BaseHTTPRequestHandler):
    @property
    def fetcher(self) -> "Fetcher":
        # Injected on the server instance via `server.fetcher = ...`.
        return self.server.fetcher  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        # Route to our logger instead of stderr noise.
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/sub":
            status, body, source = self.fetcher.get()
            # explicit X-Source for debugging
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Source", source)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        elif path == "/healthz":
            self._send(200, b"ok\n")
        elif path == "/cache":
            body = self.fetcher._last_body or b"(empty)\n"
            self._send(200, body)
        elif path == "/force-refresh":
            status, body, source = self.fetcher.get(force=True)
            self._send(status, f"refreshed; source={source}; bytes={len(body)}\n".encode())
        else:
            self._send(404, b"not found\nroutes: /sub /healthz /cache /force-refresh\n")


def main() -> int:
    opts = parse_args()
    logging.basicConfig(
        level=getattr(logging, opts.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    fetcher = Fetcher(opts)

    if not opts.upstream_url:
        LOG.error("UPSTREAM_URL is not set. Configure it via the systemd unit:")
        LOG.error("  systemctl --user edit v2raya-sub-fetcher")
        LOG.error("  [Service]")
        LOG.error("  Environment=UPSTREAM_URL=https://your-provider/sub/<your-uuid>")
        return 2

    server = ThreadingHTTPServer((opts.host, opts.port), Handler)
    server.fetcher = fetcher  # type: ignore[attr-defined]
    LOG.info("Listening on http://%s:%d  (upstream=%s, UA=%s)",
             opts.host, opts.port, opts.upstream_url, opts.user_agent)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

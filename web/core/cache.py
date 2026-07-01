"""Cache backends for the web app.

praiser's data collection is expensive and option-independent, so a *shared,
durable* cache (across hosts/restarts) is worth it. Streamlit Cloud's filesystem
is ephemeral, so the default there is a **serverless Redis** (Upstash REST) when
its secrets are present; otherwise a local file cache (fine for dev / a single
long-lived instance).

Both expose praiser's ``get/set/has`` interface, so either can be injected into
``pipeline.run(config, cache=...)``. The Redis backend is **best-effort**: any
network/backend error degrades to a miss and never breaks a scan.
"""

import json
import os
from pathlib import Path

from praiser.cache import Cache  # local file backend + the shared key() helper

# Upstash's free tier caps request size; skip pushing very large blobs to the
# shared KV (the scan still works, that entry just isn't shared-cached).
_MAX_VALUE_BYTES = 400_000
_PREFIX = "praiser:"


class RedisCache:
    """Shared TTL cache over the Upstash Redis REST API.

    Matches ``praiser.cache.Cache``'s ``get/set/has`` so it drops into the
    pipeline. ``Cache.key`` (static) is still used by callers to build keys.
    """

    def __init__(self, url: str, token: str, ttl: int) -> None:
        import httpx

        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._ttl = int(ttl)
        self._client = httpx.Client(timeout=10.0)

    # -- Redis command over the REST endpoint (best-effort) -----------------
    def _command(self, args: list):
        """Run one Redis command (as a JSON array); return its result or None."""
        try:
            resp = self._client.post(self._url, headers=self._headers, json=args)
            if resp.status_code != 200:
                return None
            return resp.json().get("result")
        except Exception:
            return None

    # -- praiser cache interface --------------------------------------------
    def get(self, key: str, default=None):
        raw = self._command(["GET", _PREFIX + key])
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def set(self, key: str, value) -> None:
        try:
            blob = json.dumps(value)
        except (TypeError, ValueError):
            return
        if len(blob) > _MAX_VALUE_BYTES:
            return
        self._command(["SET", _PREFIX + key, blob, "EX", str(self._ttl)])

    def has(self, key: str) -> bool:
        return self._command(["EXISTS", _PREFIX + key]) == 1

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def make_cache(ttl: int = 86_400):
    """Shared Redis cache when Upstash secrets are set, else a local file cache."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return RedisCache(url, token, ttl)
    directory = os.environ.get("PRAISER_CACHE_DIR", "/tmp/praiser-cache")
    return Cache(Path(directory), ttl=ttl)

"""Cache backends for the web app.

Two layers, on purpose (see ``web.core.service``):

* **HTTP layer** (``local_cache``) — praiser's per-fetch cache, passed to
  ``pipeline.run(config, cache=...)``. Kept **local** (file), which is free and
  fast; being per-instance/ephemeral is fine because the result layer is what's
  shared. Caching every fetch in a shared KV would burn hundreds of commands per
  scan — the thing we're avoiding.
* **Result layer** (``make_result_cache``) — the *shared, durable* cache: one
  entry per scan (the collected ``RunResult``), so a warm user costs ~1–2 Redis
  commands total instead of hundreds. Serverless **Redis** (Upstash REST) when
  its secrets are present, else a local file cache.

All backends expose praiser's ``get/set/has`` interface. The Redis backend is
**best-effort**: any network/backend error degrades to a miss, never breaking a
scan. Values must be JSON-serialisable (the service base64-encodes the result)."""

import json
import os
from pathlib import Path

from praiser.cache import Cache  # local file backend + the shared key() helper

# Skip pushing very large blobs to the shared KV (still works, just not shared).
# Bandwidth is ample (50 GB tier); this guards a single pathological result.
_MAX_VALUE_BYTES = 2_000_000
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

    def delete(self, key: str) -> None:
        """Remove a cached entry (best-effort; no-op if absent)."""
        self._command(["DEL", _PREFIX + key])

    def clear_all(self, protect_prefix: str | None = None) -> int:
        """Delete EVERY praiser-namespaced key (SCAN + DEL). Returns the count
        deleted. Keys whose name (after the ``praiser:`` prefix) starts with
        ``protect_prefix`` are kept — e.g. ``"stats:"`` so usage metrics survive a
        wipe. Best-effort; keys are enumerable though not reversible to usernames."""
        skip = (_PREFIX + protect_prefix) if protect_prefix else None
        deleted = 0
        cursor = "0"
        while True:
            res = self._command(
                ["SCAN", cursor, "MATCH", _PREFIX + "*", "COUNT", "500"])
            if not isinstance(res, list) or len(res) != 2:
                break
            cursor, keys = res[0], res[1]
            if skip:
                keys = [k for k in keys if not str(k).startswith(skip)]
            if keys:
                self._command(["DEL", *keys])   # keys already include the prefix
                deleted += len(keys)
            if str(cursor) == "0":
                break
        return deleted

    def incr(self, key: str, ttl: int | None = None) -> int | None:
        """Atomically increment a counter (INCR) and return the new value. On the
        first increment (value becomes 1) set ``ttl`` seconds of expiry if given,
        so rolling counters (e.g. per-day scan counts) self-expire. One command
        (two on the first hit). Best-effort — None on backend error."""
        n = self._command(["INCR", _PREFIX + key])
        if ttl and n == 1:
            self._command(["EXPIRE", _PREFIX + key, str(int(ttl))])
        return n if isinstance(n, int) else None

    def key_count(self) -> int | None:
        """Number of keys in the Redis DB (DBSIZE — one command). Counts the
        whole DB; on a praiser-dedicated Upstash DB that equals the namespace."""
        n = self._command(["DBSIZE"])
        return n if isinstance(n, int) else None

    def pfadd(self, key: str, *items) -> None:
        """Add elements to a HyperLogLog (approx-distinct counter). One command,
        idempotent — re-adding a value doesn't change the estimate. Best-effort."""
        if not items:
            return
        self._command(["PFADD", _PREFIX + key, *[str(i) for i in items]])

    def pfcount(self, key: str) -> int:
        """Estimated distinct count of a HyperLogLog (~0.8% error). 0 on miss."""
        n = self._command(["PFCOUNT", _PREFIX + key])
        return n if isinstance(n, int) else 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def local_cache(ttl: int = 86_400, refresh: bool = False):
    """Local file cache for praiser's HTTP layer (free, fast, per-instance).

    ``refresh=True`` forces the (person-anchored) fetches to re-run this scan —
    the pipeline still serves speculative org-membership repos from cache to
    avoid exhausting the API rate limit (see ``pipeline._scan_forge``)."""
    directory = os.environ.get("PRAISER_CACHE_DIR", "/tmp/praiser-cache")
    return Cache(Path(directory), ttl=ttl, refresh=refresh)


# How long a collected result stays cached. A person's elevated roles change
# slowly, so a long TTL means fewer re-scans (and fewer Redis commands). Tunable
# via PRAISER_RESULT_TTL (seconds) with no code change; default 30 days.
_DEFAULT_RESULT_TTL = 30 * 86_400


def make_result_cache(ttl: int | None = None):
    """Shared result cache: Redis when Upstash secrets are set, else local file.

    One entry per scan (the collected result), so it's the cheap, durable layer
    shared across instances/sessions. ``ttl`` defaults to ``PRAISER_RESULT_TTL``
    (seconds) or 30 days.
    """
    if ttl is None:
        ttl = int(os.environ.get("PRAISER_RESULT_TTL", _DEFAULT_RESULT_TTL))
    url = os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return RedisCache(url, token, ttl)
    directory = os.environ.get("PRAISER_RESULT_CACHE_DIR",
                               os.environ.get("PRAISER_CACHE_DIR",
                                              "/tmp/praiser-cache") + "/results")
    return Cache(Path(directory), ttl=ttl)

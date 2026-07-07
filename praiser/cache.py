"""Dead-simple file-based JSON cache keyed by a request hash.

Re-runs and LLM steps reuse cached payloads instead of re-fetching. Values
must be JSON-serialisable. A ``None`` value is stored faithfully and returned
as ``None`` on hit, so callers distinguish hit-with-None from miss via
``has()`` or the ``default`` sentinel.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_MISS = object()


class Cache:
    def __init__(
        self,
        directory: Path | str,
        ttl: float | None = None,
        refresh: bool = False,
    ) -> None:
        self.dir = Path(directory)
        self.ttl = ttl          # seconds; None = never expire
        # refresh=True: ignore existing entries on read (force a re-fetch) but
        # still write, so a run repopulates the cache fresh (--refresh).
        self.refresh = refresh
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(*parts: Any) -> str:
        blob = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def has(self, key: str) -> bool:
        return self.get(key, default=_MISS) is not _MISS

    def get(self, key: str, default: Any = None) -> Any:
        if self.refresh:            # force-refresh: treat every entry as a miss
            return default
        path = self._path(key)
        if not path.exists():
            return default
        try:
            with path.open(encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return default
        if self.ttl is not None and time.time() - record.get("ts", 0) > self.ttl:
            path.unlink(missing_ok=True)   # expired -> delete (keeps the cache from growing forever)
            return default
        return record.get("value")

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        # Unique temp name per writer so concurrent writes (threads) to the same
        # key don't clobber each other's temp file; the final replace is atomic.
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "value": value}, fh)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        """Remove a cached entry (no-op if absent). Best-effort."""
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass

    def incr(self, key: str, ttl: float | None = None) -> int | None:
        """Increment an integer counter and return the new value (starts at 1).
        ``ttl`` is accepted for interface parity with the Redis backend and
        ignored here (file entries expire via the cache-wide ttl). Best-effort;
        non-atomic (single-process file cache), returns None on error."""
        try:
            n = int(self.get(key) or 0) + 1
        except (TypeError, ValueError):
            n = 1
        self.set(key, n)
        return n

    def key_count(self) -> int | None:
        """Number of entries in this cache directory (best-effort)."""
        try:
            return len(list(self.dir.glob("*.json")))
        except OSError:
            return None

    def pfadd(self, key: str, *items) -> None:
        """Approx-distinct add. Local fallback for the Redis HyperLogLog: keeps an
        exact set (fine for a single-process dev cache). No-op without items."""
        if not items:
            return
        cur = self.get(key)
        s = set(cur) if isinstance(cur, list) else set()
        s.update(items)
        self.set(key, sorted(s))

    def pfcount(self, key: str) -> int:
        """Distinct-count of what pfadd stored (exact, local fallback)."""
        cur = self.get(key)
        return len(cur) if isinstance(cur, list) else 0

    def delete_prefix(self, prefix: str) -> int:
        """Remove every entry whose key starts with ``prefix`` (e.g. ``"stats:"``);
        return the count. Best-effort."""
        n = 0
        try:
            entries = list(self.dir.glob(f"{prefix}*.json"))
        except OSError:
            return 0
        for p in entries:
            if not p.stem.startswith(prefix):
                continue
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def acquire_lock(self, key: str, ttl: int | None = None, value=None) -> bool:
        """Best-effort lease for the single-process local cache: True unless the
        key is already set. ``ttl`` is ignored (Redis provides the real TTL lease);
        ``value`` (who holds it) is stored and readable via ``get``. Release via
        ``release_lock``."""
        if self.get(key) is not None:
            return False
        self.set(key, value if value is not None else True)
        return True

    def renew_lock(self, key: str, ttl: int | None = None, value=None) -> None:
        """Refresh a held lease (local: just re-store the value)."""
        self.set(key, value if value is not None else True)

    def release_lock(self, key: str) -> None:
        self.delete(key)

    def clear(self, protect_prefixes: tuple = ()) -> int:
        """Remove every cached entry in this cache's directory; return the count.
        Entries whose key starts with any of ``protect_prefixes`` are kept (e.g.
        usage stats / seed config survive a wipe). Best-effort."""
        n = 0
        try:
            entries = list(self.dir.glob("*.json"))
        except OSError:
            return 0
        for p in entries:
            if protect_prefixes and p.stem.startswith(tuple(protect_prefixes)):
                continue
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

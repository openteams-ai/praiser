"""Dead-simple file-based JSON cache keyed by a request hash.

Re-runs and LLM steps reuse cached payloads instead of re-fetching. Values
must be JSON-serialisable. A ``None`` value is stored faithfully and returned
as ``None`` on hit, so callers distinguish hit-with-None from miss via
``has()`` or the ``default`` sentinel.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

_MISS = object()


class Cache:
    def __init__(self, directory: Path | str, ttl: float | None = None) -> None:
        self.dir = Path(directory)
        self.ttl = ttl  # seconds; None = never expire
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
        path = self._path(key)
        if not path.exists():
            return default
        try:
            with path.open(encoding="utf-8") as fh:
                record = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return default
        if self.ttl is not None and time.time() - record.get("ts", 0) > self.ttl:
            return default
        return record.get("value")

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "value": value}, fh)
        tmp.replace(path)

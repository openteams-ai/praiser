"""A size-bounded LRU cache for collected scan results.

Keeps the results of *every* scan a session does, so revisiting an earlier
user/options is instant — bounded by total **serialized size** (not a fixed
count), so scans persist as long as there's room and the oldest are evicted
only when the byte budget is exceeded. A ``RunResult`` is small (tens of KB), so
a modest budget (default 200 MB) effectively keeps a whole session's history.

Framework-agnostic (no Streamlit import) so it's unit-testable; the frontend
just holds one instance (e.g. in ``st.session_state``). Size is estimated via
``pickle`` length — a deterministic proxy for the in-memory footprint.
"""

import pickle
from collections import OrderedDict


class SizeBoundedLRU:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = int(max_bytes)
        self._d: "OrderedDict[object, tuple]" = OrderedDict()  # key -> (value, size)
        self._total = 0

    def get(self, key, default=None):
        if key in self._d:
            self._d.move_to_end(key)          # mark most-recently-used
            return self._d[key][0]
        return default

    def __contains__(self, key) -> bool:
        return key in self._d

    def __len__(self) -> int:
        return len(self._d)

    @property
    def total_bytes(self) -> int:
        return self._total

    def pop(self, key, default=None):
        """Remove ``key`` and return its value (``default`` if absent)."""
        if key in self._d:
            value, size = self._d.pop(key)
            self._total -= size
            return value
        return default

    def discard_where(self, predicate) -> int:
        """Drop every entry whose key satisfies ``predicate(key)``; return the
        count removed. Used to evict a user from a session after an admin trashes
        their shared cache, so the next Praise re-scans instead of serving the
        stale in-session copy."""
        doomed = [k for k in self._d if predicate(k)]
        for k in doomed:
            self.pop(k)
        return len(doomed)

    def clear(self) -> None:
        """Drop all entries."""
        self._d.clear()
        self._total = 0

    def put(self, key, value) -> None:
        size = len(pickle.dumps(value))
        if key in self._d:                    # replacing: drop the old size first
            self._total -= self._d.pop(key)[1]
        self._d[key] = (value, size)
        self._d.move_to_end(key)
        self._total += size
        # Evict least-recently-used until within budget, but always keep the
        # entry just added (even if it alone exceeds the budget).
        while self._total > self.max_bytes and len(self._d) > 1:
            _, (_, evicted_size) = self._d.popitem(last=False)
            self._total -= evicted_size

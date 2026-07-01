"""Tests for the size-bounded LRU used by the web UI to persist scan results."""

import pickle

from web.core.resultcache import SizeBoundedLRU


def _sized(n):
    """A value whose pickle size is ~n bytes, for deterministic budget tests."""
    return "x" * n


def _bytes(n):
    return len(pickle.dumps(_sized(n)))


def test_get_put_round_trip_and_miss_default():
    c = SizeBoundedLRU(10_000)
    assert c.get("k", "default") == "default"
    c.put("k", {"a": 1})
    assert c.get("k") == {"a": 1}
    assert "k" in c and len(c) == 1


def test_multiple_entries_persist_within_budget():
    c = SizeBoundedLRU(10_000)
    for i in range(5):
        c.put(("user", i), _sized(100))
    assert len(c) == 5                       # all kept — under budget
    assert c.get(("user", 2)) == _sized(100)


def test_user1_user2_user1_is_a_cache_hit():
    # The reported scenario: scan u1, scan u2, back to u1 -> reused, not rescanned.
    c = SizeBoundedLRU(10_000)
    c.put("u1", "result1")
    c.put("u2", "result2")
    assert c.get("u1") == "result1"          # still cached
    assert c.get("u2") == "result2"


def test_evicts_oldest_when_over_budget():
    # Budget fits ~2 entries of ~1000 bytes; a 3rd evicts the least-recently-used.
    one = _bytes(1000)
    c = SizeBoundedLRU(int(one * 2.5))
    c.put("a", _sized(1000))
    c.put("b", _sized(1000))
    c.put("c", _sized(1000))                 # over budget -> evict LRU ("a")
    assert "a" not in c
    assert "b" in c and "c" in c
    assert c.total_bytes <= c.max_bytes


def test_get_refreshes_lru_order():
    one = _bytes(1000)
    c = SizeBoundedLRU(int(one * 2.5))
    c.put("a", _sized(1000))
    c.put("b", _sized(1000))
    c.get("a")                               # 'a' now most-recently-used
    c.put("c", _sized(1000))                 # evicts LRU, now "b" (not "a")
    assert "a" in c and "c" in c and "b" not in c


def test_replacing_key_updates_size_no_duplicate():
    c = SizeBoundedLRU(10_000)
    c.put("k", _sized(100))
    first = c.total_bytes
    c.put("k", _sized(500))                  # replace, not add
    assert len(c) == 1 and c.total_bytes > first


def test_newest_kept_even_if_larger_than_budget():
    c = SizeBoundedLRU(10)                    # tiny budget
    c.put("big", _sized(5000))               # single entry exceeds budget
    assert c.get("big") == _sized(5000)      # still kept (never evict sole entry)
    assert len(c) == 1

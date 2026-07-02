"""Tests for the file cache — TTL expiry and --refresh semantics."""

import json
import time

from praiser.cache import Cache


def test_get_set_roundtrip_and_none_faithful(tmp_path):
    c = Cache(tmp_path)
    assert c.get("k", default="miss") == "miss"
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}
    c.set("n", None)                       # None stored faithfully
    assert c.get("n", default="miss") is None
    assert c.has("n") is True


def test_ttl_expires_entries(tmp_path):
    c = Cache(tmp_path, ttl=1000)
    c.set("k", "v")
    assert c.get("k") == "v"               # fresh
    # Backdate the stored timestamp beyond the TTL.
    path = c._path("k")
    rec = json.loads(path.read_text())
    rec["ts"] = time.time() - 2000
    path.write_text(json.dumps(rec))
    assert c.get("k", default="expired") == "expired"
    assert not path.exists()               # expired entry is deleted on access


def test_ttl_none_never_expires(tmp_path):
    c = Cache(tmp_path, ttl=None)
    c.set("k", "v")
    path = c._path("k")
    rec = json.loads(path.read_text())
    rec["ts"] = 0                          # ancient
    path.write_text(json.dumps(rec))
    assert c.get("k") == "v"               # still returned


def test_refresh_ignores_reads_but_repopulates(tmp_path):
    # Seed a cache, then open it in refresh mode: reads miss (force re-fetch),
    # but writes still land — so the run repopulates it fresh.
    Cache(tmp_path).set("k", "stale")
    r = Cache(tmp_path, refresh=True)
    assert r.get("k", default="miss") == "miss"     # ignored on read
    r.set("k", "fresh")                             # ...but written
    # A subsequent normal (non-refresh) cache sees the refreshed value.
    assert Cache(tmp_path).get("k") == "fresh"


def test_cache_ttl_zero_disables_reuse(tmp_path):
    # --cache-ttl 0 -> everything is immediately "older than 0s" -> always a miss.
    c = Cache(tmp_path, ttl=0)
    c.set("k", "v")
    assert c.get("k", default="miss") == "miss"

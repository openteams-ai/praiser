"""Tests for the web shared-cache backend (offline; no real Redis/network)."""

import json

from praiser.cache import Cache
from web.core.cache import RedisCache, local_cache, make_result_cache


class _FakeRedis(RedisCache):
    """RedisCache with the REST call replaced by an in-memory store."""

    def __init__(self, ttl=100):
        self._store: dict[str, str] = {}
        self._ttl = ttl
        self.commands: list[list] = []

    def _command(self, args):
        self.commands.append(args)
        op = args[0]
        if op == "GET":
            return self._store.get(args[1])
        if op == "SET":
            self._store[args[1]] = args[2]
            return "OK"
        if op == "EXISTS":
            return 1 if args[1] in self._store else 0
        return None


def test_get_set_round_trip_and_prefix():
    c = _FakeRedis()
    assert c.get("k") is None                 # miss -> default
    c.set("k", {"a": 1})
    assert c.get("k") == {"a": 1}             # hit -> parsed value
    assert c.has("k") is True
    assert list(c._store) == ["praiser:k"]    # namespaced


def test_matches_praiser_cache_semantics_for_text_and_404():
    # praiser stores raw text and a "__404__" sentinel via get_url.
    c = _FakeRedis()
    c.set("f", "file contents")
    assert c.get("f") == "file contents"
    c.set("g", "__404__")
    assert c.get("g") == "__404__"


def test_large_values_are_not_pushed():
    from web.core.cache import _MAX_VALUE_BYTES
    c = _FakeRedis()
    c.set("big", "x" * (_MAX_VALUE_BYTES + 1))   # over the size cap
    assert c.get("big") is None and c._store == {}


def test_set_applies_ttl_via_ex():
    c = _FakeRedis(ttl=42)
    c.set("k", 1)
    assert c.commands[-1] == ["SET", "praiser:k", "1", "EX", "42"]


def test_none_command_result_degrades_to_miss():
    # RedisCache._command returns None on any backend/network error; get/has
    # must treat that as a clean miss (never crash a scan).
    class _Down(RedisCache):
        def __init__(self):
            self._ttl = 100
        def _command(self, args):
            return None
    c = _Down()
    assert c.get("k", default="d") == "d"
    assert c.has("k") is False
    c.set("k", 1)  # no-op, must not raise


def test_local_cache_is_always_a_file_cache(monkeypatch, tmp_path):
    # The HTTP layer is always local (free, per-instance) — never Redis.
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    monkeypatch.setenv("PRAISER_CACHE_DIR", str(tmp_path))
    assert isinstance(local_cache(ttl=5), Cache)


def test_result_cache_falls_back_to_local_without_redis_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("PRAISER_CACHE_DIR", str(tmp_path))
    assert isinstance(make_result_cache(ttl=5), Cache)


def test_result_cache_uses_redis_when_secrets_present(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    c = make_result_cache(ttl=5)
    assert isinstance(c, RedisCache)
    c.close()

import json
import time

import pytest

from praiser.github_client import GitHubError, GitHubClient, RateLimitError

reset_in = GitHubClient._ratelimit_reset_in


class _MemCache:
    def __init__(self): self._d = {}
    def get(self, k, default=None): return self._d.get(k, default)
    def set(self, k, v): self._d[k] = v


def _client_with_graphql(status, body_obj, rate=None):
    """A bare client whose _request returns a canned GraphQL response."""
    c = GitHubClient.__new__(GitHubClient)
    c.cache = _MemCache()
    c.rate = rate or {}
    c._request = lambda *a, **k: (status, {}, json.dumps(body_obj).encode())
    return c


def _client_with_responses(responses):
    """A bare client whose _request yields the given (status, body) tuples in
    order — so we can simulate a transient error followed by a success."""
    c = GitHubClient.__new__(GitHubClient)
    c.cache = _MemCache()
    c.rate = {}
    it = iter(responses)
    c._request = lambda *a, **k: (lambda s, b: (s, {}, b.encode()))(*next(it))
    return c


def test_get_url_does_not_cache_transient_errors():
    # A WDQS 429/500 must NOT be cached as a permanent miss — the next call
    # retries and can succeed (regression for the dropped-founder bug, #102).
    for transient in (429, 500, 502, 503):
        c = _client_with_responses([(transient, ""), (200, "OK-BODY")])
        assert c.get_url("https://query.wikidata.org/x") is None   # transient fail
        assert c.get_url("https://query.wikidata.org/x") == "OK-BODY"  # retried, ok


def test_get_url_caches_genuine_404():
    # If the 404 weren't cached, the 2nd call would consume the 200 and return
    # its body; returning None proves the 404 was cached (no 2nd fetch).
    c = _client_with_responses([(404, ""), (200, "SHOULD-NOT-BE-REACHED")])
    assert c.get_url("https://example.com/missing") is None
    assert c.get_url("https://example.com/missing") is None   # served from cache


def test_get_url_caches_success():
    c = _client_with_responses([(200, "HELLO")])
    assert c.get_url("https://example.com/x") == "HELLO"
    assert c.get_url("https://example.com/x") == "HELLO"   # cached, no 2nd _request


def test_graphql_rate_limited_error_becomes_ratelimiterror():
    future = int(time.time()) + 180
    c = _client_with_graphql(
        200, {"data": None, "errors": [{"type": "RATE_LIMITED",
                                        "message": "API rate limit exceeded"}]},
        rate={"graphql": (0, 5000, future)})
    with pytest.raises(RateLimitError) as ei:
        c.graphql("query{x}", {})
    assert 170 <= ei.value.reset_in <= 181       # reset time surfaced


def test_graphql_partial_data_with_errors_is_returned():
    # A non-rate-limit error (e.g. a missing field) with usable data -> no raise.
    c = _client_with_graphql(
        200, {"data": {"user": {"login": "x"}},
              "errors": [{"type": "NOT_FOUND", "message": "no such field"}]})
    assert c.graphql("q", {}) == {"user": {"login": "x"}}


def test_graphql_error_without_data_raises_generic():
    c = _client_with_graphql(
        200, {"data": None, "errors": [{"type": "NOT_FOUND", "message": "boom"}]})
    with pytest.raises(GitHubError):
        c.graphql("q", {})


def test_bucket_reset_in():
    c = GitHubClient.__new__(GitHubClient)
    c.rate = {"graphql": (0, 5000, int(time.time()) + 90)}
    assert 80 <= c._bucket_reset_in("graphql") <= 91
    assert c._bucket_reset_in("core") is None    # untouched bucket


def test_non_rate_limit_statuses_return_none():
    assert reset_in(404, {}, b"") is None
    assert reset_in(200, {"x-ratelimit-remaining": "0"}, b"") is None


def test_forbidden_but_not_rate_limited_returns_none():
    # A 403 that is not about rate limiting (e.g. a private/forbidden path).
    assert reset_in(403, {"x-ratelimit-remaining": "42"}, b"Not Found") is None


def test_remaining_zero_uses_reset_header():
    future = int(time.time()) + 120
    got = reset_in(403, {"x-ratelimit-remaining": "0",
                         "x-ratelimit-reset": str(future)}, b"")
    assert 110 <= got <= 121


def test_body_message_triggers_detection_even_without_headers():
    got = reset_in(403, {}, b'{"message":"API rate limit exceeded for 1.2.3.4"}')
    assert got == 60  # no reset header -> conservative default


def test_retry_after_takes_precedence():
    got = reset_in(429, {"retry-after": "7", "x-ratelimit-remaining": "0"}, b"")
    assert got == 7


def _bare_client():
    return GitHubClient.__new__(GitHubClient)  # no network/cache needed


def test_track_rate_and_summary_per_bucket():
    c = _bare_client()
    c.rate = {}
    c._track_rate({"x-ratelimit-resource": "core",
                   "x-ratelimit-remaining": "4200", "x-ratelimit-limit": "5000",
                   "x-ratelimit-reset": "0"})
    c._track_rate({"x-ratelimit-resource": "graphql",
                   "x-ratelimit-remaining": "4990", "x-ratelimit-limit": "5000",
                   "x-ratelimit-reset": "0"})
    assert c.rate["core"][0] == 4200
    summary = c.rate_summary()
    assert "REST 4200/5000" in summary
    assert "GraphQL 4990/5000" in summary


def test_track_rate_ignores_headerless_responses():
    c = _bare_client()
    c.rate = {}
    c._track_rate({})  # no rate headers (e.g. a cached path or odd response)
    assert c.rate_summary() == ""

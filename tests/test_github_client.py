import time

from praiser.github_client import GitHubClient

reset_in = GitHubClient._ratelimit_reset_in


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

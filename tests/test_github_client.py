import time

from ghrecord.github_client import GitHubClient

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

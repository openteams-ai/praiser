"""Tests for the one-request total-contributor count (Link-header trick)."""

from praiser.cache import Cache
from praiser.github_client import GitHubClient, _link_last_page

_LINK = ('<https://api.github.com/repositories/1/contributors'
         '?per_page=1&anon=true&page=2>; rel="next", '
         '<https://api.github.com/repositories/1/contributors'
         '?per_page=1&anon=true&page=6683>; rel="last"')


def test_link_last_page_parsing():
    assert _link_last_page(_LINK) == 6683
    assert _link_last_page("") is None
    assert _link_last_page(None) is None
    # single page (no rel="last") -> not derivable from the header
    assert _link_last_page('<...&page=2>; rel="next"') is None


def test_repo_contributor_count_uses_link_and_caches(tmp_path):
    c = GitHubClient(None, Cache(tmp_path))
    calls = []

    def fake_request(method, url, accept, **k):
        calls.append(url)
        return 200, {"link": _LINK}, b"[{}]"
    c._request = fake_request

    assert c.repo_contributor_count("o", "r", anon=True) == 6683
    assert "anon=true" in calls[0] and "per_page=1" in calls[0]
    # second call is served from the cache — no extra request
    assert c.repo_contributor_count("o", "r", anon=True) == 6683
    assert len(calls) == 1


def test_repo_contributor_count_no_link_counts_body(tmp_path):
    c = GitHubClient(None, Cache(tmp_path))
    c._request = lambda *a, **k: (200, {}, b'[{"login": "x"}]')
    assert c.repo_contributor_count("o", "r") == 1           # single contributor

    c0 = GitHubClient(None, Cache(tmp_path / "empty"))
    c0._request = lambda *a, **k: (200, {}, b"[]")
    assert c0.repo_contributor_count("o", "r") == 0          # no contributors


def test_repo_contributor_count_none_on_error(tmp_path):
    from praiser.github_client import GitHubError

    c = GitHubClient(None, Cache(tmp_path))
    def boom(*a, **k):
        raise GitHubError("nope")
    c._request = boom
    assert c.repo_contributor_count("o", "r") is None        # lenient on failure


def test_repo_contributor_count_propagates_rate_limit(tmp_path):
    import pytest

    from praiser.github_client import RateLimitError

    c = GitHubClient(None, Cache(tmp_path))
    def limited(*a, **k):
        raise RateLimitError("limit", reset_in=60)
    c._request = limited
    with pytest.raises(RateLimitError):
        c.repo_contributor_count("o", "r")

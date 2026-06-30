from ghrecord.cache import Cache
from ghrecord.github_client import GitHubClient


def _client(tmp_path, token="tok"):
    return GitHubClient(token, Cache(tmp_path / "c"))


def test_graphql_blobs_parsing(tmp_path):
    c = _client(tmp_path)
    c.graphql = lambda q, v: {  # type: ignore[method-assign]
        "repository": {
            "f0": {"text": "hello", "isTruncated": False},
            "f1": None,                                   # missing path
            "f2": {"text": None, "isTruncated": True},    # too large
        }
    }
    texts, truncated = c._graphql_blobs("o", "r", ["a", "b", "big"], None)
    assert texts == {"a": "hello"}
    assert truncated == {"big"}


def test_get_files_uses_graphql_and_caches(tmp_path):
    c = _client(tmp_path)
    calls = {"n": 0}

    def fake_graphql(q, v):
        calls["n"] += 1
        return {"repository": {"f0": {"text": "OWNERS!", "isTruncated": False},
                               "f1": None}}
    c.graphql = fake_graphql  # type: ignore[method-assign]

    got = c.get_files("o", "r", ["CODEOWNERS", "missing"])
    assert got == {"CODEOWNERS": "OWNERS!", "missing": None}
    assert calls["n"] == 1

    # Second call is served entirely from cache: no further GraphQL.
    again = c.get_files("o", "r", ["CODEOWNERS", "missing"])
    assert again == {"CODEOWNERS": "OWNERS!", "missing": None}
    assert calls["n"] == 1


def test_truncated_falls_back_to_rest(tmp_path):
    c = _client(tmp_path)
    c.graphql = lambda q, v: {  # type: ignore[method-assign]
        "repository": {"f0": {"text": None, "isTruncated": True}}
    }
    c.get_file = lambda o, r, p, ref=None: "FROM-REST"  # type: ignore[method-assign]
    assert c.get_files("o", "r", ["big.rst"]) == {"big.rst": "FROM-REST"}


def test_no_token_falls_back_to_rest(tmp_path):
    c = _client(tmp_path, token=None)
    seen = []

    def fake_get_file(o, r, p, ref=None):
        seen.append(p)
        return f"text-of-{p}"
    c.get_file = fake_get_file  # type: ignore[method-assign]

    got = c.get_files("o", "r", ["A", "B"])
    assert got == {"A": "text-of-A", "B": "text-of-B"}
    assert seen == ["A", "B"]

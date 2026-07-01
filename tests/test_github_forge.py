"""Offline tests for the GitHub Forge implementation.

A fake transport client routes ``graphql`` by query content and stubs the REST
helpers, so we verify the adapters (GitHub JSON -> neutral dataclasses) without
any network.
"""

import tempfile

from praiser.cache import Cache
from praiser.forge import (
    ContributorCount,
    DirEntry,
    FileHit,
    GitHubForge,
    RepoMeta,
    UserRef,
)


def _node(nwo, stars=0, fork=False):
    return {"nameWithOwner": nwo, "stargazerCount": stars, "forkCount": 0,
            "isFork": fork, "isPrivate": False, "pushedAt": "2024-01-01T00:00:00Z"}


class _FakeClient:
    def __init__(self):
        self.search_queries = []

    def graphql(self, query, variables):
        if "organizations" in query:                      # DISCOVERY_QUERY
            return {"user": {
                "login": "pearu", "name": "Pearu Peterson",
                "organizations": {"nodes": [{"login": "numpy"}, {"login": None}]},
                "repositories": {"nodes": [_node("pearu/pylibtiff", 140)]},
                "repositoriesContributedTo": {"nodes": [_node("numpy/numpy", 32000)]},
            }}
        if "createdAt" in query:
            return {"user": {"createdAt": "2014-06-01T00:00:00Z"}}
        if "contributionsCollection" in query:            # HISTORY_QUERY
            return {"user": {"contributionsCollection": {
                "commitContributionsByRepository": [
                    {"repository": _node("numpy/numpy", 32000)},
                    {"repository": _node("scipy/scipy", 12000)},
                ]}}}
        if query.lstrip().startswith("query{r"):           # batch metadata
            return {"r0": _node("a/b", 50), "r1": None}     # r1 inaccessible
        if "organization(login" in query:                  # ORG_REPOS_QUERY
            return {"organization": {"repositories": {"nodes": [_node("numpy/numpy", 32000)]}}}
        return {"user": {"login": "pearu", "name": "Pearu Peterson"}}  # USER_QUERY

    def rest_json(self, path, params=None):
        return {"full_name": "numpy/numpy", "stargazers_count": 32000,
                "forks_count": 11000, "fork": False, "private": False,
                "pushed_at": "2024-02-02T00:00:00Z"}

    def list_dir(self, owner, repo, path):
        return [{"name": "f.py", "type": "file", "path": "x/f.py"},
                {"name": "sub", "type": "dir", "path": "x/sub"},
                {"type": "file"}]  # nameless entry is skipped

    def search_code(self, query, per_page=30):
        self.search_queries.append(query)
        return [{"repository": {"full_name": "a/b"}, "path": "CODEOWNERS"},
                {"repository": {}}]  # no full_name -> skipped

    def search_commits(self, query, per_page=100):
        return [{"repository": {"full_name": "a/b"}},
                {"repository": {"full_name": "a/b"}}]  # dup collapses

    def repo_contributors(self, owner, repo, max_pages=2):
        return [{"login": "pearu", "contributions": 500}, {"contributions": 1}]


def _forge(fake=None):
    f = GitHubForge(None, Cache(tempfile.mkdtemp()))
    f._client = fake or _FakeClient()
    return f


def test_web_url():
    assert _forge().web_url("numpy/numpy") == "https://github.com/numpy/numpy"


def test_resolve_user():
    assert _forge().resolve_user("pearu") == UserRef(login="pearu", name="Pearu Peterson")


def test_user_repositories_and_contributed_and_orgs():
    f = _forge()
    assert f.user_repositories("pearu") == [RepoMeta("pearu/pylibtiff", stars=140,
                                                     pushed_at="2024-01-01T00:00:00Z")]
    assert f.user_contributed_repositories("pearu")[0].name_with_owner == "numpy/numpy"
    assert f.user_organizations("pearu") == ["numpy"]  # None login dropped


def test_organization_repositories():
    assert _forge().organization_repositories("numpy")[0].name_with_owner == "numpy/numpy"


def test_repository_rest_adapter():
    meta = _forge().repository("numpy", "numpy")
    assert meta == RepoMeta("numpy/numpy", stars=32000, forks=11000,
                            pushed_at="2024-02-02T00:00:00Z")


def test_repositories_metadata_stamps_names_and_omits_missing():
    out = _forge().repositories_metadata(["a/b", "ghost/gone"])
    assert set(out) == {"a/b"}             # r1 (None) omitted
    assert out["a/b"].name_with_owner == "a/b" and out["a/b"].stars == 50


def test_user_commit_history_dedupes_to_repometa():
    hist = _forge().user_commit_history("pearu")
    names = {m.name_with_owner for m in hist}
    assert names == {"numpy/numpy", "scipy/scipy"}
    assert all(isinstance(m, RepoMeta) for m in hist)


def test_list_dir_adapter():
    entries = _forge().list_dir("a", "b", "x")
    assert entries == [DirEntry("f.py", "x/f.py", False), DirEntry("sub", "x/sub", True)]


def test_search_file_mentions_quotes_multiword_and_parses_hits():
    fake = _FakeClient()
    f = _forge(fake)
    assert f.search_file_mentions("pearu", "CODEOWNERS") == [FileHit("a/b", "CODEOWNERS")]
    f.search_file_mentions("Pearu Peterson", "AUTHORS")
    assert fake.search_queries == ['pearu filename:CODEOWNERS',
                                   '"Pearu Peterson" filename:AUTHORS']


def test_search_commits_by_author_dedupes():
    assert _forge().search_commits_by_author("pearu") == ["a/b"]


def test_repo_contributors_adapter_and_none_passthrough():
    f = _forge()
    assert f.repo_contributors("a", "b") == [ContributorCount("pearu", 500)]

    class _NoneClient(_FakeClient):
        def repo_contributors(self, owner, repo, max_pages=2):
            return None
    assert _forge(_NoneClient()).repo_contributors("a", "b") is None

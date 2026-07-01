"""Offline tests for the Gitea/Forgejo (Codeberg) forge.

A fake transport returns canned API payloads keyed by request path, so the
adapters (Gitea JSON -> neutral dataclasses) are verified without network.
"""

import tempfile

from praiser.cache import Cache
from praiser.forge import DirEntry, GiteaForge, RepoMeta, UserRef


def _repo(full_name, stars=0, fork=False):
    return {"full_name": full_name, "stars_count": stars, "forks_count": 0,
            "fork": fork, "private": False, "updated_at": "2024-01-01T00:00:00Z"}


class _FakeHttp:
    def __init__(self, json_routes=None, raw=None, external=None):
        self.json_routes = json_routes or {}
        self.raw = raw or {}
        self.external = external or {}

    def get_json(self, path, params=None):
        # repo listings are paginated; serve page 1, empty afterwards
        if params and params.get("page", 1) > 1:
            return []
        return self.json_routes.get(path)

    def get_raw(self, owner, repo, path, ref):
        return self.raw.get((owner, repo, path))

    def get_external(self, url, accept):
        return self.external.get(url)

    def close(self):
        pass


def _forge(http):
    f = GiteaForge(None, Cache(tempfile.mkdtemp()))
    f._http = http
    return f


def test_name_and_web_url_default_to_codeberg():
    f = _forge(_FakeHttp())
    assert f.name == "codeberg"
    assert f.web_url("a/b") == "https://codeberg.org/a/b"


def test_custom_instance_base_and_name():
    f = GiteaForge(None, Cache(tempfile.mkdtemp()),
                   base_url="https://git.example.org/", name="example")
    assert f.name == "example"
    assert f.web_url("a/b") == "https://git.example.org/a/b"


def test_resolve_user_uses_full_name():
    f = _forge(_FakeHttp({"users/earl": {"login": "earl", "full_name": "Earl Warren"}}))
    assert f.resolve_user("earl") == UserRef("earl", "Earl Warren")


def test_resolve_user_missing_is_none():
    assert _forge(_FakeHttp()).resolve_user("ghost") is None


def test_repository_adapter():
    f = _forge(_FakeHttp({"repos/forgejo/forgejo": _repo("forgejo/forgejo", 5000)}))
    meta = f.repository("forgejo", "forgejo")
    assert meta == RepoMeta("forgejo/forgejo", stars=5000,
                            pushed_at="2024-01-01T00:00:00Z")


def test_user_repositories_paginates_and_adapts():
    f = _forge(_FakeHttp({"users/earl/repos": [_repo("earl/a"), _repo("earl/b")]}))
    repos = f.user_repositories("earl")
    assert [r.name_with_owner for r in repos] == ["earl/a", "earl/b"]


def test_user_organizations():
    f = _forge(_FakeHttp({"users/earl/orgs": [{"username": "forgejo"}, {"name": "x"}]}))
    assert f.user_organizations("earl") == ["forgejo"]  # entry without username dropped


def test_organization_repositories():
    f = _forge(_FakeHttp({"orgs/forgejo/repos": [_repo("forgejo/forgejo")]}))
    assert f.organization_repositories("forgejo")[0].name_with_owner == "forgejo/forgejo"


def test_list_dir_adapter_and_non_dir():
    routes = {"repos/a/b/contents/x": [
        {"name": "f.py", "path": "x/f.py", "type": "file"},
        {"name": "sub", "path": "x/sub", "type": "dir"},
        {"type": "file"}]}  # nameless skipped
    f = _forge(_FakeHttp(routes))
    assert f.list_dir("a", "b", "x") == [DirEntry("f.py", "x/f.py", False),
                                         DirEntry("sub", "x/sub", True)]
    # a file path returns a dict, not a list -> empty
    assert _forge(_FakeHttp({"repos/a/b/contents/f": {"name": "f"}})).list_dir("a", "b", "f") == []


def test_get_file_reads_raw():
    f = _forge(_FakeHttp(raw={("a", "b", "README.md"): "hello"}))
    assert f.get_file("a", "b", "README.md") == "hello"
    assert f.get_file("a", "b", "missing") is None


def test_get_url_uses_external_no_auth():
    f = _forge(_FakeHttp(external={"http://x/team": "<html>team</html>"}))
    assert f.get_url("http://x/team") == "<html>team</html>"


def test_unsupported_capabilities_degrade_gracefully():
    f = _forge(_FakeHttp())
    # Gitea has no cheap endpoint for these -> safe interface defaults.
    assert f.repo_contributors("a", "b") is None
    assert f.user_commit_history("earl") == []
    assert f.search_file_mentions("earl", "CODEOWNERS") == []
    assert f.merged_pr_count("a", "b", "earl") == 0

"""Offline tests for the Bitbucket forge."""

import tempfile

from praiser.cache import Cache
from praiser.forge import BitbucketForge, DirEntry, RepoMeta, UserRef


class _FakeHttp:
    def __init__(self, json_routes=None, src=None):
        self.json_routes = json_routes or {}
        self.src = src or {}
        self.src_calls = []

    def get_json(self, path, params=None):
        if params and params.get("page", 1) > 1:
            return {"values": []}  # only page 1 has data
        return self.json_routes.get(path)

    def get_src(self, w, r, ref, path):
        self.src_calls.append((w, r, ref, path))
        return self.src.get((w, r, ref, path))

    def get_external(self, url, accept):
        return None

    def close(self):
        pass


def _forge(json_routes=None, src=None):
    f = BitbucketForge(None, Cache(tempfile.mkdtemp()))
    f._http = _FakeHttp(json_routes, src)
    return f


def test_name_web_url_and_no_stars():
    f = _forge()
    assert f.name == "bitbucket" and f.has_stars is False
    assert f.web_url("acme/widget") == "https://bitbucket.org/acme/widget"


def test_repository_uses_watchers_as_popularity_and_detects_fork():
    routes = {
        "repositories/acme/widget": {
            "full_name": "acme/widget", "is_private": False,
            "mainbranch": {"name": "main"}, "parent": {"full_name": "up/widget"},
            "updated_on": "2024-05-05T00:00:00+00:00"},
        "repositories/acme/widget/watchers": {"size": 7},
    }
    meta = _forge(routes).repository("acme", "widget")
    assert meta == RepoMeta("acme/widget", stars=0, forks=7, is_fork=True,
                            pushed_at="2024-05-05T00:00:00+00:00")


def test_resolve_user_via_workspace_then_echo_fallback():
    f = _forge({"workspaces/jane": {"slug": "jane", "name": "Jane R"}})
    assert f.resolve_user("jane") == UserRef("jane", "Jane R")
    # unknown workspace -> echo the login (never fail resolution)
    assert _forge().resolve_user("ghost") == UserRef("ghost", None)


def test_get_file_resolves_default_branch_then_fetches_src():
    routes = {"repositories/a/b": {"full_name": "a/b", "mainbranch": {"name": "trunk"}}}
    f = _forge(routes, src={("a", "b", "trunk", "README.md"): "hi"})
    assert f.get_file("a", "b", "README.md") == "hi"
    assert f._http.src_calls[-1] == ("a", "b", "trunk", "README.md")


def test_get_file_with_explicit_ref_skips_branch_lookup():
    f = _forge(src={("a", "b", "dev", "F"): "x"})
    assert f.get_file("a", "b", "F", ref="dev") == "x"
    assert f._http.src_calls == [("a", "b", "dev", "F")]


def test_list_dir_adapter():
    routes = {
        "repositories/a/b": {"mainbranch": {"name": "main"}},
        "repositories/a/b/src/main/": {"values": [
            {"type": "commit_file", "path": ".gitignore"},
            {"type": "commit_directory", "path": "docs"}]},
    }
    assert _forge(routes).list_dir("a", "b", "") == [
        DirEntry(".gitignore", ".gitignore", False),
        DirEntry("docs", "docs", True)]


def test_list_dir_nested_path_gets_trailing_slash():
    routes = {
        "repositories/a/b": {"mainbranch": {"name": "main"}},
        "repositories/a/b/src/main/docs/": {"values": [
            {"type": "commit_file", "path": "docs/guide.md"}]},
    }
    entries = _forge(routes).list_dir("a", "b", "docs")
    assert entries == [DirEntry("guide.md", "docs/guide.md", False)]


def test_user_repositories_lists_workspace_repos():
    # Bitbucket list items ALWAYS include `parent` (null for non-forks), so the
    # fork test must check the value, not the key — else every repo looks forked.
    routes = {"repositories/jane": {"values": [
        {"full_name": "jane/x", "is_private": False, "parent": None,
         "updated_on": "2024-01-01T00:00:00+00:00"},
        {"full_name": "jane/y", "parent": {"full_name": "up/y"}}]}}
    repos = _forge(routes).user_repositories("jane")
    assert [r.name_with_owner for r in repos] == ["jane/x", "jane/y"]
    assert repos[0].is_fork is False  # parent=null -> NOT a fork
    assert repos[1].is_fork is True   # parent set -> fork

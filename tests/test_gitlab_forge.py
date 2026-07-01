"""Offline tests for the GitLab forge.

A fake transport returns canned API payloads keyed by request path, verifying
the adapters (GitLab JSON -> neutral dataclasses) and GitLab's quirks: nested
project paths, username->id lookup, and its field names.
"""

import tempfile

from praiser.cache import Cache
from praiser.forge import DirEntry, GitLabForge, RepoMeta, UserRef


def _project(path, stars=0, fork=False, visibility="public"):
    return {"path_with_namespace": path, "star_count": stars, "forks_count": 0,
            "forked_from_project": ({"id": 1} if fork else None),
            "visibility": visibility, "last_activity_at": "2024-01-01T00:00:00Z"}


class _FakeHttp:
    def __init__(self, json_routes=None, raw=None, external=None):
        self.json_routes = json_routes or {}
        self.raw = raw or {}
        self.external = external or {}
        self.raw_calls = []

    def get_json(self, path, params=None):
        if params and params.get("page", 1) > 1:
            return []
        # user lookup carries the username in params
        if path == "users" and params:
            return self.json_routes.get(f"users?username={params['username']}")
        return self.json_routes.get(path)

    def get_raw(self, project, path, ref):
        self.raw_calls.append((project, path, ref))
        return self.raw.get((project, path))

    def get_external(self, url, accept):
        return self.external.get(url)

    def close(self):
        pass


def _forge(http):
    f = GitLabForge(None, Cache(tempfile.mkdtemp()))
    f._http = http
    return f


def test_name_and_web_url():
    f = _forge(_FakeHttp())
    assert f.name == "gitlab"
    assert f.web_url("group/sub/proj") == "https://gitlab.com/group/sub/proj"


def test_self_hosted_instance_sets_web_base_and_name():
    f = GitLabForge(None, Cache(tempfile.mkdtemp()),
                    base_url="https://gitlab.gnome.org", name="gnome")
    assert f.name == "gnome"
    assert f.web_base == "https://gitlab.gnome.org"
    assert f.web_url("gnome/mutter") == "https://gitlab.gnome.org/gnome/mutter"


def test_resolve_user_and_id_reuse():
    http = _FakeHttp({"users?username=jane": [{"id": 7, "username": "jane", "name": "Jane R"}]})
    f = _forge(http)
    assert f.resolve_user("jane") == UserRef("jane", "Jane R")
    assert f._user_ids["jane"] == 7  # cached from the resolve call


def test_repository_adapter_fork_and_visibility():
    http = _FakeHttp({"projects/a%2Fb": _project("a/b", stars=99, fork=True,
                                                 visibility="private")})
    meta = _forge(http).repository("a", "b")
    assert meta == RepoMeta("a/b", stars=99, is_fork=True, is_private=True,
                            pushed_at="2024-01-01T00:00:00Z")


def test_nested_project_path_is_preserved():
    # Candidate.repo keeps everything after the first slash, so a subgroup path
    # round-trips through get_file as group/subgroup/project.
    http = _FakeHttp(raw={("g/sub/proj", "README.md"): "hi"})
    f = _forge(http)
    assert f.get_file("g", "sub/proj", "README.md") == "hi"
    assert http.raw_calls == [("g/sub/proj", "README.md", None)]


def test_user_repositories_via_id_lookup():
    http = _FakeHttp({
        "users?username=jane": [{"id": 7, "username": "jane"}],
        "users/7/projects": [_project("jane/x", 5), _project("jane/y")],
    })
    repos = _forge(http).user_repositories("jane")
    assert [r.name_with_owner for r in repos] == ["jane/x", "jane/y"]


def test_user_repositories_unknown_user_is_empty():
    assert _forge(_FakeHttp()).user_repositories("ghost") == []


def test_list_dir_tree_adapter():
    http = _FakeHttp({"projects/a%2Fb/repository/tree": [
        {"name": "f.py", "path": "f.py", "type": "blob"},
        {"name": "sub", "path": "sub", "type": "tree"}]})
    assert _forge(http).list_dir("a", "b", "") == [
        DirEntry("f.py", "f.py", False), DirEntry("sub", "sub", True)]


def test_unsupported_capabilities_degrade_gracefully():
    f = _forge(_FakeHttp())
    assert f.repo_contributors("a", "b") is None
    assert f.user_organizations("jane") == []
    assert f.user_commit_history("jane") == []
    assert f.search_file_mentions("jane", "CODEOWNERS") == []

"""Offline tests for the Gitee forge (GitHub-shaped REST v5)."""

import base64
import tempfile

from praiser.cache import Cache
from praiser.forge import DirEntry, GiteeForge, RepoMeta, UserRef


def _repo(full_name, stars=0, fork=False):
    return {"full_name": full_name, "stargazers_count": stars, "forks_count": 0,
            "fork": fork, "private": False, "pushed_at": "2024-01-01T00:00:00Z"}


def _b64(text):
    return {"encoding": "base64",
            "content": base64.b64encode(text.encode()).decode()}


class _FakeHttp:
    def __init__(self, json_routes=None):
        self.json_routes = json_routes or {}

    def get_json(self, path, params=None):
        if params and params.get("page", 1) > 1:
            return []
        return self.json_routes.get(path)

    def get_external(self, url, accept):
        return None

    def close(self):
        pass


def _forge(routes):
    f = GiteeForge(None, Cache(tempfile.mkdtemp()))
    f._http = _FakeHttp(routes)
    return f


def test_name_web_url_and_has_stars():
    f = _forge({})
    assert f.name == "gitee"
    assert f.has_stars is True  # Gitee has stars -> ranking uses them
    assert f.web_url("a/b") == "https://gitee.com/a/b"


def test_repository_adapter():
    meta = _forge({"repos/mindspore/mindspore": _repo("mindspore/mindspore", 9000)}
                  ).repository("mindspore", "mindspore")
    assert meta == RepoMeta("mindspore/mindspore", stars=9000,
                            pushed_at="2024-01-01T00:00:00Z")


def test_get_file_base64_decode():
    routes = {"repos/a/b/contents/README.md": _b64("hello gitee")}
    assert _forge(routes).get_file("a", "b", "README.md") == "hello gitee"


def test_get_file_on_directory_returns_none():
    # contents of a dir is a list, not a base64 blob
    routes = {"repos/a/b/contents/src": [{"name": "x", "path": "src/x", "type": "file"}]}
    assert _forge(routes).get_file("a", "b", "src") is None


def test_list_dir_adapter():
    routes = {"repos/a/b/contents/": [
        {"name": "f.py", "path": "f.py", "type": "file"},
        {"name": "sub", "path": "sub", "type": "dir"}]}
    assert _forge(routes).list_dir("a", "b", "") == [
        DirEntry("f.py", "f.py", False), DirEntry("sub", "sub", True)]


def test_resolve_user_and_repos():
    routes = {
        "users/jane": {"login": "jane", "name": "Jane R"},
        "users/jane/repos": [_repo("jane/x", 5), _repo("jane/y")],
    }
    f = _forge(routes)
    assert f.resolve_user("jane") == UserRef("jane", "Jane R")
    assert [r.name_with_owner for r in f.user_repositories("jane")] == ["jane/x", "jane/y"]


def test_user_organizations():
    routes = {"users/jane/orgs": [{"login": "acme"}, {"name": "nologin"}]}
    assert _forge(routes).user_organizations("jane") == ["acme"]

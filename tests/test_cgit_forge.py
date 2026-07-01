"""Offline tests for the generic cgit backend."""

import tempfile

from praiser.cache import Cache
from praiser.forge import CgitForge


class _FakeSession:
    """Records requested URLs and serves canned bodies (None = 404)."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.urls = []

    def get(self, url, headers=None):
        self.urls.append(url)
        body = self.bodies.get(url)

        class _R:
            status_code = 200 if body is not None else 404
            content = (body or "").encode()
        return _R()


def _forge(bodies, base="https://git.savannah.gnu.org", name="savannah"):
    f = CgitForge(None, Cache(tempfile.mkdtemp()), base_url=base, name=name)
    f._session = _FakeSession(bodies)
    return f


def test_name_web_base_and_no_stars():
    f = _forge({})
    assert f.name == "savannah"
    assert f.has_stars is False  # ranking falls back to forks
    assert f.web_url("cgit/gnulib.git") == "https://git.savannah.gnu.org/cgit/gnulib.git"


def test_get_file_builds_cgit_plain_url_and_rejoins_path():
    # owner/repo rejoin to the full cgit-relative path (repo keeps the rest).
    url = "https://git.savannah.gnu.org/cgit/gnulib.git/plain/README"
    f = _forge({url: "hello gnulib"})
    assert f.get_file("cgit", "gnulib.git", "README") == "hello gnulib"
    assert f._session.urls[-1] == url


def test_get_file_with_ref_adds_h_param():
    f = _forge({})
    f.get_file("cgit", "x.git", "F", ref="stable")
    assert f._session.urls[-1].endswith("/plain/F?h=stable")


def test_kernel_org_nested_path():
    base = "https://git.kernel.org"
    url = f"{base}/pub/scm/git/git.git/plain/README.md"
    f = _forge({url: "# Git"}, base=base, name="kernel")
    # Candidate would split "pub/scm/git/git.git" into owner="pub", repo=rest.
    assert f.get_file("pub", "scm/git/git.git", "README.md") == "# Git"


def test_repository_confirms_existence_via_summary_page():
    base = "https://git.savannah.gnu.org"
    f = _forge({f"{base}/cgit/gnulib.git/": "<html>cgit summary</html>"})
    assert f.repository("cgit", "gnulib.git").name_with_owner == "cgit/gnulib.git"
    # a mistyped repo (no summary page) is dropped
    assert f.repository("cgit", "nope.git") is None


def test_list_dir_is_deferred_empty():
    assert _forge({}).list_dir("cgit", "x.git", "") == []

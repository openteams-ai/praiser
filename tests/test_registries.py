import json

from praiser.models import Identity, PackageRef
from praiser.registries import (
    crates_packages,
    crates_refs,
    discover_packages,
    github_nwo,
    index_by_repo,
    npm_packages,
    npm_refs,
    pypi_name_guesses,
    pypi_ref,
    pypi_ref_for_repo,
)


def _identity(login="pearu", names=(), emails=()):
    return Identity(primary_login=login, logins={login},
                    names=set(names), emails=set(emails))


def _fetcher(pages: dict[str, object]):
    """fetch(url)->text; dict/list values are JSON-encoded, str passed through."""
    def fetch(url):
        if url not in pages:
            return None
        val = pages[url]
        return val if isinstance(val, str) else json.dumps(val)
    return fetch


# --- github_nwo -----------------------------------------------------------
def test_github_nwo_variants():
    assert github_nwo("https://github.com/numpy/numpy") == "numpy/numpy"
    assert github_nwo("https://github.com/numpy/numpy.git") == "numpy/numpy"
    assert github_nwo("git+https://github.com/a/b.git#egg=x") == "a/b"
    assert github_nwo("git@github.com:a/b.git") == "a/b"
    assert github_nwo("https://github.com/a/b/tree/main/sub") == "a/b"


def test_github_nwo_rejects_non_repo_and_other_forges():
    assert github_nwo("https://github.com/sponsors/pearu") is None
    assert github_nwo("https://gitlab.com/a/b") is None
    assert github_nwo("https://example.com") is None
    assert github_nwo(None) is None


# --- index_by_repo --------------------------------------------------------
def test_index_by_repo_groups_and_lowercases_and_skips_repoless():
    refs = [
        PackageRef("pypi", "x", "u1", repo="A/B"),
        PackageRef("npm", "y", "u2", repo="a/b"),
        PackageRef("crates", "z", "u3", repo=None),
    ]
    idx = index_by_repo(refs)
    assert set(idx) == {"a/b"}
    assert len(idx["a/b"]) == 2


# --- PyPI (reverse probe) -------------------------------------------------
def test_pypi_name_guesses_normalizes_separators():
    assert pypi_name_guesses("scikit_learn") == ["scikit_learn", "scikit-learn"]
    assert pypi_name_guesses("NumPy") == ["NumPy", "numpy"]


def test_pypi_ref_matches_author_or_maintainer_fields():
    base = {"name": "p", "project_urls": {"Source": "https://github.com/a/p"}}
    me = _identity(names=["Pearu Peterson"])
    assert pypi_ref({**base, "author": "Pearu Peterson"}, me).author_match is True
    assert pypi_ref({**base, "maintainer": "Pearu Peterson"}, me).author_match is True
    assert pypi_ref({**base, "author": "Someone Else"}, me).author_match is False


def test_pypi_ref_for_repo_credits_authored_package():
    pages = {"https://pypi.org/pypi/mypkg/json": {"info": {
        "name": "mypkg", "author": "Pearu Peterson",
        "project_urls": {"Source": "https://github.com/pearu/mypkg"}}}}
    ref = pypi_ref_for_repo(_fetcher(pages), "pearu/mypkg",
                            _identity(names=["Pearu Peterson"]))
    assert ref is not None and ref.author_match and ref.repo == "pearu/mypkg"


def test_pypi_ref_for_repo_rejects_non_author_even_if_name_matches():
    # numpy exists and its source is the repo, but the user isn't its author.
    pages = {"https://pypi.org/pypi/numpy/json": {"info": {
        "name": "numpy", "author": "Travis Oliphant et al.",
        "project_urls": {"Source": "https://github.com/numpy/numpy"}}}}
    assert pypi_ref_for_repo(_fetcher(pages), "numpy/numpy",
                             _identity(names=["Pearu Peterson"])) is None


def test_pypi_ref_for_repo_rejects_different_source_repo():
    # A same-named package authored by the user but shipping from another repo.
    pages = {"https://pypi.org/pypi/mypkg/json": {"info": {
        "name": "mypkg", "author": "Pearu Peterson",
        "project_urls": {"Source": "https://github.com/someoneelse/mypkg"}}}}
    assert pypi_ref_for_repo(_fetcher(pages), "pearu/mypkg",
                             _identity(names=["Pearu Peterson"])) is None


def test_pypi_ref_for_repo_anchors_when_source_absent():
    # No source URL, but user-authored and name guessed from the repo -> credit.
    pages = {"https://pypi.org/pypi/mypkg/json": {"info": {
        "name": "mypkg", "author": "Pearu Peterson"}}}
    ref = pypi_ref_for_repo(_fetcher(pages), "pearu/mypkg",
                            _identity(names=["Pearu Peterson"]))
    assert ref is not None and ref.repo == "pearu/mypkg"


# --- npm -------------------------------------------------------------------
def test_npm_refs_handles_links_and_string_repository():
    data = {"objects": [
        {"package": {"name": "left-pad",
                     "links": {"repository": "https://github.com/stevemao/left-pad"}}},
        {"package": {"name": "p2",
                     "repository": "git+https://github.com/o/r.git",
                     "author": {"name": "Pearu Peterson"}}},
    ]}
    refs = npm_refs(data, _identity(names=["Pearu Peterson"]))
    assert refs[0].repo == "stevemao/left-pad"
    assert refs[1].repo == "o/r" and refs[1].author_match is True


def test_npm_packages_uses_maintainer_query():
    url = ("https://registry.npmjs.org/-/v1/search"
           "?text=maintainer:pearu&size=100")
    refs = npm_packages(_fetcher({url: {"objects": [
        {"package": {"name": "pkg",
                     "links": {"repository": "https://github.com/a/b"}}}]}}),
        _identity())
    assert len(refs) == 1 and refs[0].repo == "a/b"


# --- crates.io -------------------------------------------------------------
def test_crates_refs():
    data = {"crates": [{"name": "serde", "repository": "https://github.com/serde-rs/serde"}]}
    refs = crates_refs(data)
    assert refs[0].repo == "serde-rs/serde"
    assert refs[0].url == "https://crates.io/crates/serde"


def test_crates_packages_resolves_user_then_crates():
    pages = {
        "https://crates.io/api/v1/users/pearu": {"user": {"id": 42, "login": "pearu"}},
        "https://crates.io/api/v1/crates?user_id=42&per_page=100": {
            "crates": [{"name": "c", "repository": "https://github.com/a/c"}]},
    }
    refs = crates_packages(_fetcher(pages), _identity())
    assert len(refs) == 1 and refs[0].repo == "a/c"


def test_crates_packages_unknown_user_returns_empty():
    assert crates_packages(_fetcher({}), _identity()) == []


# --- orchestration --------------------------------------------------------
def test_discover_packages_merges_forward_registries():
    pages = {
        "https://registry.npmjs.org/-/v1/search?text=maintainer:pearu&size=100": {
            "objects": [{"package": {
                "name": "n", "links": {"repository": "https://github.com/a/n"}}}]},
        "https://crates.io/api/v1/users/pearu": {"user": {"id": 1}},
        "https://crates.io/api/v1/crates?user_id=1&per_page=100": {
            "crates": [{"name": "c", "repository": "https://github.com/a/c"}]},
    }
    refs = discover_packages(_fetcher(pages), _identity())
    assert {r.registry for r in refs} == {"npm", "crates"}
    # PyPI is a reverse probe, never part of forward discovery.
    assert all(r.registry != "pypi" for r in refs)


def test_discover_packages_swallows_collector_exceptions():
    def boom(url):
        raise RuntimeError("network on fire")
    # Must not raise; every collector fails -> empty list.
    assert discover_packages(boom, _identity()) == []

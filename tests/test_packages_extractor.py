import json

from praiser.extractors.base import ExtractContext
from praiser.extractors.packages import PackagesExtractor
from praiser.models import AUTHOR, MAINTAINER, Candidate, Identity, PackageRef
from praiser.registry import KnownProjects


class _Client:
    """Fake GitHubClient exposing only get_url, backed by a url->payload dict."""

    def __init__(self, pages):
        self._pages = pages

    def get_url(self, url, accept=None):
        val = self._pages.get(url)
        return val if val is None or isinstance(val, str) else json.dumps(val)


def _ctx(index, forge=None, names=()):
    return ExtractContext(
        identity=Identity(primary_login="pearu", names=set(names)),
        forge=forge,
        registry=KnownProjects(projects={}),
        package_index=index,
    )


def test_no_index_means_not_applicable():
    ext = PackagesExtractor()
    assert ext.applicable(Candidate("a/b"), _ctx({})) is False


def test_emits_maintainer_evidence_for_indexed_repo():
    index = {"numpy/numpy": [PackageRef("pypi", "numpy",
                                        "https://pypi.org/project/numpy/",
                                        repo="numpy/numpy")]}
    ev = PackagesExtractor().extract(Candidate("numpy/numpy"), _ctx(index))
    assert len(ev) == 1
    assert ev[0].role == MAINTAINER
    assert ev[0].source == "pypi"
    assert ev[0].url == "https://pypi.org/project/numpy/"
    assert "numpy" in ev[0].detail


def test_author_match_promotes_role_and_confidence():
    index = {"a/b": [PackageRef("pypi", "p", "u", repo="a/b", author_match=True)]}
    ev = PackagesExtractor().extract(Candidate("a/b"), _ctx(index))
    assert ev[0].role == AUTHOR
    assert ev[0].confidence > 0.8


def test_repo_not_in_index_yields_nothing():
    index = {"a/b": [PackageRef("npm", "p", "u", repo="a/b")]}
    assert PackagesExtractor().extract(Candidate("c/d"), _ctx(index)) == []


def test_match_is_case_insensitive():
    index = {"a/b": [PackageRef("crates", "c", "u", repo="A/B")]}
    ev = PackagesExtractor().extract(Candidate("A/B"), _ctx(index))
    assert len(ev) == 1 and ev[0].source == "crates"


def test_pypi_reverse_probe_emits_author_evidence():
    client = _Client({"https://pypi.org/pypi/mypkg/json": {"info": {
        "name": "mypkg", "author": "Pearu Peterson",
        "project_urls": {"Source": "https://github.com/pearu/mypkg"}}}})
    ctx = _ctx({}, forge=client, names=["Pearu Peterson"])
    ev = PackagesExtractor().extract(Candidate("pearu/mypkg"), ctx)
    assert len(ev) == 1
    assert ev[0].source == "pypi" and ev[0].role == AUTHOR


def test_pypi_probe_skipped_without_a_known_name():
    client = _Client({"https://pypi.org/pypi/mypkg/json": {"info": {
        "name": "mypkg", "author": "Pearu Peterson"}}})
    ctx = _ctx({}, forge=client, names=())  # no identity name -> no probe
    assert PackagesExtractor().applicable(Candidate("pearu/mypkg"), ctx) is False

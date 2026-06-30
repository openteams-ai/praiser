from praiser.extractors.base import ExtractContext
from praiser.extractors.web_roles import (
    WebRolesExtractor,
    handles_on_page,
    matches,
    page_text,
)
from praiser.models import Candidate, Identity
from praiser.registry import KnownProject, KnownProjects, RoleSource

PAGE = """
<html><body>
<h2>Maintainers</h2>
<ul>
  <li><a href="https://github.com/pearu">Pearu Peterson</a></li>
  <li><a href="https://github.com/someone">Some One</a></li>
</ul>
</body></html>
"""


def test_page_text_strips_markup_and_entities():
    txt = page_text("<p>Hello&amp;world <b>x</b></p>")
    assert "hello&world" in txt
    assert "<" not in txt


def test_handles_on_page():
    assert handles_on_page(PAGE) == {"pearu", "someone"}


def test_matches_handle_then_name_then_none():
    assert matches(PAGE, {"pearu"}, set()) is True            # handle link
    assert matches("<p>Pearu Peterson</p>", set(), {"pearu peterson"}) is False
    assert matches(PAGE, {"ghost"}, {"nobody here"}) is None


class _Client:
    def __init__(self, page):
        self._page = page

    def get_url(self, url):
        return self._page


def _ctx(page, role="maintainer"):
    reg = KnownProjects({"a/b": KnownProject(
        "a/b", role_sources=[RoleSource("http://x", role, "Team page")])})
    return ExtractContext(
        identity=Identity(primary_login="pearu", names={"Pearu Peterson"}),
        forge=_Client(page), registry=reg,
    )


def test_extractor_assigns_registry_role_from_page():
    ev = WebRolesExtractor().extract(Candidate("a/b"), _ctx(PAGE, "steering_council"))
    assert len(ev) == 1
    assert ev[0].role == "steering_council"
    assert ev[0].confidence == 0.9  # handle match


def test_extractor_no_match_returns_nothing():
    ev = WebRolesExtractor().extract(
        Candidate("a/b"), _ctx("<p>nobody relevant</p>"))
    assert ev == []


NAME_ONLY = "<h2>About</h2><p>Founded with help from Pearu Peterson.</p>"


def test_steering_council_requires_handle_not_name():
    # name-only on a steering-council source -> rejected
    ev = WebRolesExtractor().extract(
        Candidate("a/b"), _ctx(NAME_ONLY, "steering_council"))
    assert ev == []


def test_maintainer_allows_name_match():
    ev = WebRolesExtractor().extract(
        Candidate("a/b"), _ctx(NAME_ONLY, "maintainer"))
    assert len(ev) == 1
    assert ev[0].role == "maintainer"
    assert ev[0].confidence == 0.75  # name match


def test_steering_council_handle_match_is_kept():
    ev = WebRolesExtractor().extract(
        Candidate("a/b"), _ctx(PAGE, "steering_council"))
    assert len(ev) == 1 and ev[0].role == "steering_council"

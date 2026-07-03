"""Offline tests for the release-manager extractor."""

from praiser.extractors.base import ExtractContext
from praiser.extractors.releases import (
    ReleaseManagerExtractor,
    classify,
    release_standing,
)
from praiser.models import RELEASE_MANAGER, Candidate, Identity
from praiser.registry import KnownProjects


def test_release_standing_filters_bots():
    authors = ["charris"] * 8 + ["github-actions[bot]"] * 5 + ["someoneelse"] * 2
    mine, total = release_standing(authors, {"charris"})
    assert (mine, total) == (8, 10)          # 5 bot releases excluded from both


def test_release_standing_none_when_all_bots():
    assert release_standing(["github-actions[bot]", "dependabot[bot]"], {"x"}) is None


def test_classify_share_and_floor():
    assert classify(100, 100) == 0.8         # dominant
    assert classify(8, 10) == 0.8            # >= 50%
    assert classify(4, 12) == 0.65           # 33%, above the 25% floor + >=3
    assert classify(2, 4) is None            # only 2 releases (< MIN_RELEASES)
    assert classify(3, 20) is None           # 15% share, below the floor


class _ReleaseForge:
    def __init__(self, authors):
        self.authors = authors
        self.calls = 0

    def repo_release_authors(self, owner, repo, max_releases=100):
        self.calls += 1
        return self.authors


def _ctx(forge, login="charris", floor=1000):
    return ExtractContext(
        identity=Identity(primary_login=login),
        forge=forge, registry=KnownProjects(projects={}),
        role_discovery_floor=floor)


def _extract(ctx, cand):
    ext = ReleaseManagerExtractor()
    return ext.extract(cand, ctx) if ext.applicable(cand, ctx) else []


def test_dominant_release_author_is_credited():
    forge = _ReleaseForge(["charris"] * 100)
    ev = _extract(_ctx(forge), Candidate("numpy/numpy", stars=32000))
    assert len(ev) == 1
    assert ev[0].role == RELEASE_MANAGER and ev[0].confidence == 0.8
    assert "100 of the last 100 releases" in ev[0].detail
    assert ev[0].url.endswith("/releases")


def test_minor_release_author_not_credited():
    # one release out of many -> not a release manager
    forge = _ReleaseForge(["release-lead"] * 99 + ["charris"])
    assert _extract(_ctx(forge), Candidate("numpy/numpy", stars=32000)) == []


def test_bot_only_project_yields_no_signal():
    forge = _ReleaseForge(["github-actions[bot]"] * 100)
    assert _extract(_ctx(forge), Candidate("auto/repo", stars=5000)) == []


def test_gated_off_below_popularity_floor():
    forge = _ReleaseForge(["charris"] * 100)
    assert _extract(_ctx(forge, floor=1000), Candidate("small/repo", stars=50)) == []
    assert forge.calls == 0                  # not even a network call


def test_no_releases_is_clean_miss():
    assert _extract(_ctx(_ReleaseForge([])), Candidate("norel/repo", stars=5000)) == []
    assert _extract(_ctx(_ReleaseForge(None)), Candidate("norel/repo", stars=5000)) == []

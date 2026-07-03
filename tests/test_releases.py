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


def test_classify_scales_with_share_no_dominance_gate():
    assert classify(100, 100) == 0.9         # all releases → capped high
    assert classify(88, 100) == round(0.55 + 0.35 * 0.88, 2)
    assert classify(6, 100) == round(0.55 + 0.35 * 0.06, 2)   # minor share still credited
    assert classify(2, 100) == round(0.55 + 0.35 * 0.02, 2)   # 2 releases → credited (low conf)
    assert classify(1, 100) is None          # a single release is filtered (incidental/CD)


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
    assert ev[0].role == RELEASE_MANAGER and ev[0].confidence == 0.9
    assert "100 of the last 100 releases" in ev[0].detail
    assert (ev[0].releases_authored, ev[0].releases_total) == (100, 100)
    assert ev[0].url.endswith("/releases")


def test_occasional_release_manager_is_credited_with_count():
    # #79: cutting a few releases is worth crediting; magnitude is reported, not gated.
    forge = _ReleaseForge(["tylerjereddy"] * 88 + ["rgommers"] * 6
                          + ["ev-br"] * 4 + ["pv"] * 2)
    ev = _extract(_ctx(forge, login="rgommers"), Candidate("scipy/scipy", stars=15000))
    assert len(ev) == 1 and ev[0].role == RELEASE_MANAGER
    assert (ev[0].releases_authored, ev[0].releases_total) == (6, 100)
    # pv, with 2 releases, is still credited (above the single-release floor)
    ev_pv = _extract(_ctx(forge, login="pv"), Candidate("scipy/scipy", stars=15000))
    assert ev_pv and ev_pv[0].releases_authored == 2


def test_single_release_is_filtered():
    # one release out of many -> incidental / release-per-merge noise, not credited
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

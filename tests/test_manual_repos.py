from ghrecord.discovery import keep_candidate
from ghrecord.extractors.base import ExtractContext
from ghrecord.extractors.contributors import ContributorsExtractor
from ghrecord.models import CODE_OWNER, Candidate, Evidence, Identity, ProjectRecord
from ghrecord.popularity import filter_records
from ghrecord.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def test_manual_candidate_kept_even_if_fork_or_private():
    c = Candidate("heavyai/rbc", is_fork=True, is_private=True)
    c.sources.add("manual")
    assert keep_candidate(c, EMPTY, include_private=False)


def test_manual_repo_is_trusted_and_contributor_checked():
    class C:
        def repo_contributors(self, o, r, max_pages=2):
            return [{"login": "x", "contributions": 9}] * 3 + \
                   [{"login": "pearu", "contributions": 50}]
    ctx = ExtractContext(
        identity=Identity(primary_login="pearu"), client=C(), registry=EMPTY,
        manual_repos={"heavyai/rbc"}, popularity_floor=50,
    )
    cand = Candidate("heavyai/rbc", stars=29, forks=11)  # below normal gates
    assert ctx.trust_role_file(cand)
    assert ContributorsExtractor().applicable(cand, ctx)
    ev = ContributorsExtractor().extract(cand, ctx)
    assert ev and ev[0].role == "core_contributor"


def test_manual_repo_records_plain_contribution():
    # apache/arrow case: 8 commits, rank ~165 — below the elevated bar, but the
    # user vouched via --add-repo, so it's recorded at low confidence.
    class C:
        def repo_contributors(self, o, r, max_pages=2):
            return [{"login": f"u{i}", "contributions": 100} for i in range(164)] + \
                   [{"login": "pearu", "contributions": 8}]
        def merged_pr_count(self, o, r, login):
            return 8  # modest -> still below the bar
    ctx = ExtractContext(
        identity=Identity(primary_login="pearu"), client=C(), registry=EMPTY,
        manual_repos={"apache/arrow"}, popularity_floor=50,
    )
    ev = ContributorsExtractor().extract(Candidate("apache/arrow", stars=16000), ctx)
    assert ev and ev[0].role == "core_contributor"
    assert ev[0].confidence == 0.4
    assert "8 commits" in ev[0].detail


def test_non_manual_plain_contribution_excluded():
    class C:
        def repo_contributors(self, o, r, max_pages=2):
            return [{"login": f"u{i}", "contributions": 100} for i in range(164)] + \
                   [{"login": "pearu", "contributions": 8}]
        def merged_pr_count(self, o, r, login):
            return 8
    ctx = ExtractContext(
        identity=Identity(primary_login="pearu"), client=C(), registry=EMPTY,
        popularity_floor=50, canonical_stars=1000,
    )
    # popular repo, but pearu is a plain contributor -> excluded
    assert ContributorsExtractor().extract(Candidate("apache/arrow", stars=16000), ctx) == []


def test_manual_repo_forced_into_primary():
    rec = ProjectRecord(
        name_with_owner="heavyai/rbc", url="u", stars=29, forks=11,
        evidence=[Evidence("contributors", "core_contributor", "u", 0.8, "")],
    )
    primary, secondary = filter_records(
        [rec], min_stars=50, registry=EMPTY, force_primary={"heavyai/rbc"})
    assert [r.name_with_owner for r in primary] == ["heavyai/rbc"]
    assert not secondary

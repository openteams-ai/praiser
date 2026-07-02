from praiser.extractors.authors import find_credit
from praiser.extractors.base import ExtractContext
from praiser.extractors.contributors import classify
from praiser.forge import ContributorCount
from praiser.models import Candidate, Identity
from praiser.registry import KnownProjects


class _RecordingForge:
    def __init__(self):
        self.calls = []

    def repo_contributors(self, owner, repo, max_pages=2):
        self.calls.append(max_pages)
        return [ContributorCount("pearu", 10)]


class _ContribForge:
    def repo_contributors(self, owner, repo, max_pages=2):
        return [ContributorCount("pearu", 200)]


def _contrib_ctx():
    from praiser.extractors.contributors import ContributorsExtractor  # noqa
    return ExtractContext(
        identity=Identity(primary_login="pearu"),
        forge=_ContribForge(),
        registry=KnownProjects(projects={}),
    )


def test_contributor_signal_rejected_on_vendored_copy():
    from praiser.extractors.contributors import ContributorsExtractor
    ext = ContributorsExtractor()
    # EasyFHE: vendored pytorch history makes pearu a "contributor", but the
    # repo is small and unaffiliated -> not trustworthy -> no role.
    ev = ext.extract(Candidate("jizhuoran/EasyFHE", stars=53, forks=1), _contrib_ctx())
    assert ev == []


def test_rank1_rescue_trusts_top_contributor_of_widely_forked_nonfork_repo():
    # djhoese/pylibtiff case: unaffiliated + below canonical popularity, so
    # trust_role_file is False — but the user is the #1 contributor to a
    # widely-forked, non-fork repo, so the signal is trusted (rescued).
    from praiser.extractors.contributors import ContributorsExtractor

    class TopContrib:
        def repo_contributors(self, o, r, max_pages=2):
            return [ContributorCount("pearu", 107), ContributorCount("other", 40)]
    ctx = ExtractContext(identity=Identity(primary_login="pearu"), forge=TopContrib(),
                         registry=KnownProjects(projects={}))
    cand = Candidate("someoneelse/pylibtiff", stars=140, forks=57)  # 57 >= WIDELY_USED_FORKS
    cand.is_fork = False
    ev = ContributorsExtractor().extract(cand, ctx)
    assert ev and ev[0].role == "core_contributor" and "#1" in ev[0].detail


def test_rank1_rescue_does_not_trust_non_top_contributor():
    # Same modest, unaffiliated repo but the user is NOT #1 -> not trusted
    # (guards against vendored-copy false positives).
    from praiser.extractors.contributors import ContributorsExtractor

    class NotTop:
        def repo_contributors(self, o, r, max_pages=2):
            return [ContributorCount("lead", 300), ContributorCount("pearu", 40)]
    ctx = ExtractContext(identity=Identity(primary_login="pearu"), forge=NotTop(),
                         registry=KnownProjects(projects={}))
    cand = Candidate("someoneelse/proj", stars=140, forks=57)
    cand.is_fork = False
    assert ContributorsExtractor().extract(cand, ctx) == []


def test_contributor_signal_kept_on_canonical_repo():
    from praiser.extractors.contributors import ContributorsExtractor
    ev = ContributorsExtractor().extract(
        Candidate("numpy/numpy", stars=30000, forks=10000), _contrib_ctx())
    assert ev and ev[0].role == "core_contributor"


def test_merged_pr_rescue_elevates_undercounted_contributor():
    # Commit count says plain contributor (5, rank ~50), but the user has many
    # merged PRs (squash/ghstack, or unlinked email) -> elevated via PR count.
    from praiser.extractors.contributors import ContributorsExtractor

    class C:
        def repo_contributors(self, o, r, max_pages=2):
            return [ContributorCount(f"u{i}", 9) for i in range(50)] + \
                   [ContributorCount("pearu", 5)]
        def merged_pr_count(self, o, r, login):
            return 150
    ctx = ExtractContext(
        identity=Identity(primary_login="pearu"), forge=C(),
        registry=KnownProjects(projects={}), popularity_floor=50,
    )
    ev = ContributorsExtractor().extract(Candidate("big/repo", stars=20000), ctx)
    assert ev and ev[0].role == "core_contributor"
    assert "merged PRs" in ev[0].detail


def test_contributor_pages_cap_is_passed_through():
    forge = _RecordingForge()
    ctx = ExtractContext(
        identity=Identity(primary_login="pearu"),
        forge=forge,
        registry=KnownProjects(projects={}),
        contributor_pages=2,
    )
    ctx.contributors(Candidate("a/b"))
    assert forge.calls == [2]
    # cached: no second fetch
    ctx.contributors(Candidate("a/b"))
    assert forge.calls == [2]

THANKS = """\
SciPy Developers
================

Founding authors:
  Pearu Peterson - f2py, core architecture
  Travis Oliphant - project lead

Many thanks to all contributors.
"""


def test_classify_elevation_tiers():
    assert classify(500, 3) == 0.8      # huge volume
    assert classify(5, 7) == 0.8        # very high rank
    assert classify(30, 25) == 0.6      # solid contributor
    assert classify(3, 200) is None     # a few commits -> not elevated


def test_find_credit_name_match():
    hit = find_credit(THANKS, names={"pearu peterson"}, logins=set())
    assert hit is not None
    line, strong = hit
    assert "Pearu Peterson" in line
    assert strong is False


def test_find_credit_handle_match_is_strong():
    text = "Maintainers:\n- @pearu\n- @someone\n"
    hit = find_credit(text, names=set(), logins={"pearu"})
    assert hit is not None
    _, strong = hit
    assert strong is True


def test_find_credit_short_name_ignored():
    # Very short names are skipped to avoid false positives.
    assert find_credit("contains bob somewhere", names={"bob"}, logins=set()) is None


def test_find_credit_absent():
    assert find_credit(THANKS, names={"nobody here"}, logins={"ghost"}) is None

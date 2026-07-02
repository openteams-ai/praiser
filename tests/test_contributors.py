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


def test_genuine_contribution_source_is_trusted_even_low_fork():
    # draive/KaQuMiQ: #1 of a modest non-fork repo (few forks, <1000 stars, no
    # affiliation) is trusted BECAUSE it was discovered via a genuine live-
    # contribution signal (contributed/history) — copy-resistant, so no fork/
    # star threshold needed. (#62)
    from praiser.extractors.contributors import ContributorsExtractor
    cand = Candidate("miquido/draive", stars=110, forks=12)  # below all thresholds
    cand.is_fork = False
    cand.sources = {"contributed", "history"}
    ev = ContributorsExtractor().extract(cand, _contrib_ctx())
    assert ev and ev[0].role == "core_contributor"


def test_low_repo_without_genuine_source_still_rejected():
    # Same modest, unaffiliated repo but NO genuine-contribution source (e.g.
    # discovered by name-search only / a vendored copy) -> still gated out.
    # Guards that #62 didn't weaken the copy-resistance (EasyFHE-style).
    from praiser.extractors.contributors import ContributorsExtractor
    cand = Candidate("jizhuoran/EasyFHE", stars=53, forks=1)
    cand.is_fork = False
    cand.sources = {"commit-search"}       # name/search, not a contribution signal
    assert ContributorsExtractor().extract(cand, _contrib_ctx()) == []


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


class _CappedForge:
    """200 contributors fetched (== the 2-page cap) → the list is truncated."""
    def __init__(self, real_count):
        self.real_count = real_count
        self.count_calls = 0
    def repo_contributors(self, o, r, max_pages=2):
        return ([ContributorCount("pearu", 500)]
                + [ContributorCount(f"u{i}", 50) for i in range(199)])
    def repo_contributor_count(self, o, r, anon=True):
        self.count_calls += 1
        assert anon is True         # the uncapped identity count
        return self.real_count


def _capped_ctx(forge, **kw):
    return ExtractContext(
        identity=Identity(primary_login="pearu"), forge=forge,
        registry=kw.pop("registry", KnownProjects(projects={})),
        contributor_pages=2, popularity_floor=50, **kw)


def test_capped_contributor_list_gets_real_total_from_forge():
    from praiser.extractors.contributors import ContributorsExtractor
    forge = _CappedForge(6683)
    ev = ContributorsExtractor().extract(
        Candidate("big/repo", stars=9000), _capped_ctx(forge))
    assert ev[0].n_contributors == 6683 and ev[0].contributors_capped is False
    assert ev[0].contributors_approx is True        # resolved total is approximate
    assert forge.count_calls == 1


def test_capped_falls_back_to_lower_bound_when_forge_cant_answer():
    from praiser.extractors.contributors import ContributorsExtractor
    forge = _CappedForge(None)          # forge can't determine the total
    ev = ContributorsExtractor().extract(
        Candidate("big/repo", stars=9000), _capped_ctx(forge))
    assert ev[0].n_contributors == 200 and ev[0].contributors_capped is True
    assert ev[0].contributors_approx is False       # lower bound, not an estimate


def test_registry_snapshot_wins_over_live_count():
    from praiser.extractors.contributors import ContributorsExtractor
    from praiser.registry import KnownProject
    reg = KnownProjects(projects={"big/repo": KnownProject(
        "big/repo", popularity={"contributors": 16432})})
    forge = _CappedForge(6683)
    ev = ContributorsExtractor().extract(
        Candidate("big/repo", stars=9000), _capped_ctx(forge, registry=reg))
    assert ev[0].n_contributors == 16432 and ev[0].contributors_capped is False
    assert ev[0].contributors_approx is True        # a snapshot is approximate
    assert forge.count_calls == 0       # snapshot short-circuits the live call


def test_uncapped_list_is_exact_and_makes_no_extra_call():
    from praiser.extractors.contributors import ContributorsExtractor

    class Small:
        def repo_contributors(self, o, r, max_pages=2):
            return [ContributorCount("pearu", 500), ContributorCount("x", 5)]
        def repo_contributor_count(self, o, r, anon=True):
            raise AssertionError("must not query the total for a complete list")
    ev = ContributorsExtractor().extract(
        Candidate("small/repo", stars=9000), _capped_ctx(Small()))
    assert ev[0].n_contributors == 2 and ev[0].contributors_capped is False
    assert ev[0].contributors_approx is False       # full list -> exact


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
    assert classify(30, 25) == 0.6      # solid volume (rank irrelevant)
    assert classify(15, 7) == 0.8       # genuine top-10 + real work
    assert classify(3, 200) is None     # a few commits -> not elevated


def test_classify_rank_needs_real_work_not_a_few_prs():
    # The ngoldbaum false-positive class: 1-8 commits ranking top-10 on a
    # small-team repo must NOT read as core (a few PRs != core contributor).
    assert classify(2, 2) is None       # openai/tiktoken: 2 commits, rank #2
    assert classify(8, 5) is None       # pyca/bcrypt: 8 commits, rank #5
    assert classify(1, 7) is None       # httptools: 1 commit, rank #7


def test_classify_regular_contributor_not_core():
    # The ev-br/tunix class: double-digit commits at a middling rank is a
    # regular contributor, not core — the loose top-30 tier is gone.
    assert classify(11, 17) is None     # google/tunix: 11 commits, rank #17
    assert classify(15, 20) is None     # regular contributor
    assert classify(24, 12) is None     # just under the volume bar, not top-10
    assert classify(25, 500) == 0.6     # but real volume still counts, any rank


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

"""Tests for the org-repo reverse-index seeder (#65)."""

from praiser.cache import Cache
from praiser.contribindex import ContributorIndex
from praiser.forge import ContributorCount, RepoMeta
from praiser.seed import seed_org
from web.seed import parse_seed_target


def test_parse_seed_target_org_repo_and_forge():
    # bare / forge-prefixed org
    assert parse_seed_target("numpy") == ("github", "org", "numpy")
    assert parse_seed_target("github/numpy") == ("github", "org", "numpy")
    assert parse_seed_target("gitlab/foo") == ("gitlab", "org", "foo")
    # single repo: forge/owner/repo, or bare owner/repo (leading seg not a forge)
    assert parse_seed_target("github/pytorch/pytorch") == ("github", "repo", "pytorch/pytorch")
    assert parse_seed_target("pytorch/pytorch") == ("github", "repo", "pytorch/pytorch")
    # stray slashes / empty
    assert parse_seed_target("/numpy/") == ("github", "org", "numpy")
    assert parse_seed_target("", "gitea") == ("gitea", "org", "")


def test_seed_one_repo_indexes_just_that_repo(tmp_path):
    from praiser.seed import seed_one
    cache = Cache(tmp_path)
    idx = ContributorIndex(cache)
    f = FakeForge()
    res = seed_one("acme/big", forge=f, index=idx, cache=cache)
    assert res["seeded"] == 1 and res["contributors_indexed"] == 2
    assert f.fetches == ["acme/big"]                       # only that repo
    assert set(idx.repos_for("jek")) == {"acme/big"}
    # re-seed within TTL is skipped
    assert seed_one("acme/big", forge=f, index=idx, cache=cache)["seeded"] == 0


class FakeForge:
    """Org with 3 repos; records how many contributor fetches happened."""
    def __init__(self):
        self.fetches = []
        self._rosters = {
            "acme/big":  [ContributorCount("alice", 900), ContributorCount("jek", 500)],
            "acme/mid":  [ContributorCount("bob", 200), ContributorCount("jek", 60)],
            "acme/small": [ContributorCount("carol", 40)],
        }
    def organization_repositories(self, org):
        return [RepoMeta(n) for n in self._rosters]
    def repo_contributors(self, owner, repo, max_pages=2):
        self.fetches.append(f"{owner}/{repo}")
        return self._rosters[f"{owner}/{repo}"]
    def rate_summary(self):
        return "REST 5000/5000"


def test_seed_populates_reverse_index(tmp_path):
    cache = Cache(tmp_path)
    idx = ContributorIndex(cache)
    f = FakeForge()
    res = seed_org("acme", forge=f, index=idx, cache=cache, budget=50)
    assert res["seeded"] == 3
    # jek is a contributor to two acme repos -> both indexed from the org seed,
    # with no scan of jek at all (the whole point).
    assert set(idx.repos_for("jek")) == {"acme/big", "acme/mid"}
    assert idx.repos_for("alice") == ["acme/big"]


def test_seed_budget_limits_repos(tmp_path):
    cache = Cache(tmp_path)
    f = FakeForge()
    res = seed_org("acme", forge=f, index=ContributorIndex(cache), cache=cache, budget=2)
    assert res["seeded"] == 2
    assert len(f.fetches) == 2                 # only 2 repos fetched
    assert "budget" in res["stopped"]


def test_seed_is_resumable_skips_already_seeded(tmp_path):
    cache = Cache(tmp_path)
    idx = ContributorIndex(cache)
    f = FakeForge()
    seed_org("acme", forge=f, index=idx, cache=cache, budget=2)   # seeds 2
    first = list(f.fetches)
    seed_org("acme", forge=f, index=idx, cache=cache, budget=50)  # resume
    # the 2 already-seeded repos are skipped; only the remaining one is fetched
    assert set(f.fetches) - set(first) == {"acme/small"}
    assert len(f.fetches) == 3                 # 2 + 1, no re-fetch


def test_seed_stops_on_low_quota(tmp_path):
    cache = Cache(tmp_path)
    class LowQuota(FakeForge):
        def rate_summary(self): return "REST 100/5000"   # below MIN_REST
    f = LowQuota()
    res = seed_org("acme", forge=f, index=ContributorIndex(cache), cache=cache, budget=50)
    assert res["seeded"] == 0 and "low REST" in res["stopped"]

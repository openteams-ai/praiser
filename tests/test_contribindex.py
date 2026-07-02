"""Contributor reverse-index (#59): unit + two-scan collaborative discovery."""

from praiser.cache import Cache
from praiser.contribindex import ContributorIndex, MIN_COMMITS
from praiser.discovery import discover
from praiser.forge import Forge, RepoMeta
from praiser.models import Identity
from praiser.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def test_index_records_and_looks_up_substantial_contributors(tmp_path):
    idx = ContributorIndex(Cache(tmp_path))
    idx.record_rosters({"sqlalchemy/sqlalchemy": {"zzzeek": 9000, "jek": 515},
                        "a/b": {"jek": 3}})            # 3 < MIN_COMMITS -> skipped
    assert idx.repos_for("jek") == ["sqlalchemy/sqlalchemy"]
    assert "sqlalchemy/sqlalchemy" in idx.repos_for("zzzeek")
    assert idx.repos_for("nobody") == []


def test_index_is_case_insensitive_and_merges(tmp_path):
    idx = ContributorIndex(Cache(tmp_path))
    idx.record_rosters({"o/r1": {"Jek": 100}})
    idx.record_rosters({"o/r2": {"jek": 100}})          # merge, not overwrite
    assert set(idx.repos_for("JEK")) == {"o/r1", "o/r2"}


def test_min_commits_floor(tmp_path):
    idx = ContributorIndex(Cache(tmp_path))
    idx.record_rosters({"o/r": {"drive_by": MIN_COMMITS - 1}})
    assert idx.repos_for("drive_by") == []


class _Forge(Forge):
    """Minimal forge: no person-side signal reaches the target repo (the jek
    situation), so only the reverse-index can surface it."""
    name = "github"
    web_base = "https://github.com"
    def web_url(self, nwo): return f"https://github.com/{nwo}"
    def get_file(self, o, r, p, ref=None): return None
    def list_dir(self, o, r, p): return []
    def repository(self, o, r): return None
    def get_url(self, url, accept="text/html"): return None
    def repositories_metadata(self, names):
        return {n: RepoMeta(n, stars=12000, is_fork=False) for n in names}


def test_two_scan_collaborative_discovery(tmp_path):
    # Scan #1 fetched sqlalchemy's roster (recorded here). Scan #2 of jek — whose
    # account exposes NO signal to sqlalchemy — must still discover it via the
    # reverse-index.
    idx = ContributorIndex(Cache(tmp_path))
    idx.record_rosters({"sqlalchemy/sqlalchemy": {"zzzeek": 9000, "jek": 515}})

    cands = {c.name_with_owner: c for c in discover(
        _Forge(), Identity(primary_login="jek"), EMPTY,
        include_org_repos=False, use_code_search=False,
        index_repos=idx.repos_for("jek"))}
    assert "sqlalchemy/sqlalchemy" in cands
    assert "reverse-index" in cands["sqlalchemy/sqlalchemy"].sources


def test_unindexed_user_gets_no_reverse_candidates(tmp_path):
    idx = ContributorIndex(Cache(tmp_path))
    cands = {c.name_with_owner: c for c in discover(
        _Forge(), Identity(primary_login="stranger"), EMPTY,
        include_org_repos=False, use_code_search=False,
        index_repos=idx.repos_for("stranger"))}
    assert cands == {}

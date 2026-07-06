"""Seed the contributor reverse-index from an organization's repos (#65).

Pre-populates the reverse-index (#59) so that scanning anyone who contributes to
an org's repos immediately surfaces those repos — the collaborative-discovery
network effect, triggered ahead of time. We index the org's **repos** (not its
members): fetching each repo's contributor roster captures every contributor,
including hidden ones and external non-members, with no dependence on (often
private) org membership.

Budgeted + resumable so a periodic run stays under the API rate limit: each run
seeds up to ``budget`` not-recently-seeded repos and stops near the quota; a
per-repo marker (TTL-bounded, in the cache) makes re-runs skip fresh repos and
naturally re-seed once they expire.
"""

import argparse
import re
import sys

from .cache import Cache
from .config import default_cache_dir, resolve_token
from .contribindex import MIN_COMMITS, ContributorIndex
from .forge import GitHubForge
from .github_client import RateLimitError

SEED_PAGES = 5          # contributors pages (100 each) — GitHub caps ~500 anyway
MIN_REST = 400          # stop if REST quota dips below this
SEED_TTL = 2_592_000    # 30 days: re-seed a repo only after this (matches index TTL)
# How many of an org's repos (most-starred first) seeding will consider. Well
# above the UI budget so the budget is the real limit, and high enough that
# resumed runs reach repos beyond the first budget's worth.
SEED_ORG_LIST = 500


def _rest_remaining(forge) -> int | None:
    m = re.search(r"(\d+)\s*/\s*\d+", forge.rate_summary() or "")
    return int(m.group(1)) if m else None


def _load_coverage(cache, target: str) -> tuple[set, set]:
    """Per-target cumulative coverage (distinct repos + distinct indexed logins),
    unioned across seed runs — the source of truth for the coverage report. Stored
    under its own key so the (potentially large) login set never bloats the catalog
    blob; a target's set is bounded (~500 contributors/repo × its repos)."""
    data = cache.get(Cache.key("seed-coverage", target))
    if not isinstance(data, dict):
        return set(), set()
    return set(data.get("repos", [])), set(data.get("logins", []))


def _save_coverage(cache, target: str, repos: set, logins: set) -> None:
    cache.set(Cache.key("seed-coverage", target),
              {"repos": sorted(repos), "logins": sorted(logins)})


def seed_repo(name_with_owner, *, forge, index, cache, force=False) -> set | None:
    """Seed one repo's contributor roster into the index. Returns the set of
    **indexed** contributor logins (those with >= MIN_COMMITS commits — i.e. the
    ones actually made discoverable, possibly empty), or None if skipped (already
    seeded within SEED_TTL). Raises RateLimitError to let the caller stop."""
    marker = Cache.key("roster-seeded", name_with_owner)
    if not force and cache.has(marker):
        return None  # seeded within SEED_TTL — skip (re-seeds once it expires)
    owner, _, repo = name_with_owner.partition("/")
    contribs = forge.repo_contributors(owner, repo, max_pages=SEED_PAGES)
    indexed: set = set()
    if contribs:
        roster = {c.login.lower(): c.contributions for c in contribs if c.login}
        index.record_rosters({name_with_owner: roster})
        # Mirror record_rosters' threshold so the count reflects who's discoverable.
        indexed = {login for login, commits in roster.items() if commits >= MIN_COMMITS}
    cache.set(marker, True)
    return indexed


def seed_one(name_with_owner, *, forge, index, cache, log=lambda m: None) -> dict:
    """Seed a single specified repo (for when only one repo is of interest)."""
    cov_repos, cov_logins = _load_coverage(cache, name_with_owner)
    try:
        logins = seed_repo(name_with_owner, forge=forge, index=index, cache=cache)
    except RateLimitError as exc:
        return _seed_summary("repo", name_with_owner, 0, 0, 1,
                             cov_repos, cov_logins, f"rate limit ({exc.reset_in}s)")
    if logins is None:
        return _seed_summary("repo", name_with_owner, 0, 0, 1,
                             cov_repos, cov_logins, "already seeded (within 30 days)")
    cov_repos.add(name_with_owner)
    cov_logins |= logins
    _save_coverage(cache, name_with_owner, cov_repos, cov_logins)
    log(f"seeded {name_with_owner} ({len(logins)} contributors)")
    return _seed_summary("repo", name_with_owner, 1, len(logins), 1,
                         cov_repos, cov_logins, "all repos seeded")


def seed_org(org, *, forge, index, cache, budget=50, log=lambda m: None) -> dict:
    """Seed the reverse-index from ``org``'s repos. Returns a small summary.

    Skips repos seeded within SEED_TTL (resumable across periodic runs); stops at
    ``budget`` newly-seeded repos or when the REST quota runs low.
    """
    cov_repos, cov_logins = _load_coverage(cache, org)
    try:
        repos = forge.organization_repositories(org, limit=SEED_ORG_LIST)
    except RateLimitError as exc:
        return _seed_summary("org", org, 0, 0, 0,
                             cov_repos, cov_logins, f"rate limit ({exc.reset_in}s)")

    seeded = entries = 0
    stopped = None
    for meta in repos:
        if seeded >= budget:
            stopped = f"budget ({budget} repos)"
            break
        rem = _rest_remaining(forge)
        if rem is not None and rem < MIN_REST:
            stopped = f"low REST quota ({rem})"
            break
        try:
            logins = seed_repo(meta.name_with_owner, forge=forge, index=index, cache=cache)
        except RateLimitError as exc:
            stopped = f"rate limit ({exc.reset_in}s)"
            break
        if logins is None:
            continue  # already seeded within SEED_TTL
        entries += len(logins)
        seeded += 1
        cov_repos.add(meta.name_with_owner)
        cov_logins |= logins
        log(f"seeded {meta.name_with_owner} ({len(logins)} contributors)")
    if seeded:
        _save_coverage(cache, org, cov_repos, cov_logins)
    return _seed_summary("org", org, seeded, entries, len(repos),
                         cov_repos, cov_logins, stopped or "all repos seeded")


def _seed_summary(kind, target, seeded, entries, repos_available,
                  cov_repos, cov_logins, stopped) -> dict:
    """Build the seed result dict. ``seeded``/``contributors_indexed`` describe
    THIS run (repos freshly seeded, sum of their indexed rosters); ``*_distinct``
    are the cumulative distinct coverage for the target (from the coverage set), so
    a no-op re-run still reports the true totals."""
    return {kind: target, "seeded": seeded, "contributors_indexed": entries,
            "repos_available": repos_available,
            "repos_distinct": len(cov_repos),
            "contributors_distinct": len(cov_logins),
            "stopped": stopped}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="praiser-seed",
        description="Seed praiser's contributor reverse-index from an org's "
                    "repos, so later scans of its contributors surface them. "
                    "Budgeted + resumable for periodic, rate-limit-safe runs.")
    p.add_argument("org", help="GitHub organization login (e.g. pytest-dev)")
    p.add_argument("--budget", type=int, default=50, metavar="N",
                   help="max repos to seed this run (default: 50); a periodic "
                        "run resumes where it left off (re-seeds after 30 days)")
    p.add_argument("--token", default=None,
                   help="GitHub token (or GITHUB_TOKEN / GH_TOKEN); a dedicated "
                        "bot token is recommended so seeding doesn't spend your "
                        "personal API quota")
    p.add_argument("--cache-dir", default=None,
                   help="cache directory (default: ~/.cache/praiser)")
    args = p.parse_args(argv)

    token, _ = resolve_token(args.token)
    cache = Cache(args.cache_dir or default_cache_dir(), ttl=SEED_TTL)
    forge = GitHubForge(token, cache)
    index = ContributorIndex(cache)
    try:
        result = seed_org(args.org, forge=forge, index=index, cache=cache,
                          budget=args.budget, log=lambda m: print(f"[seed] {m}"))
    finally:
        forge.close()
    print(f"[seed] {result['org']}: seeded {result['seeded']} repo(s) this run "
          f"(of {result['repos_available']} available) — coverage: "
          f"{result['repos_distinct']} repo(s), {result['contributors_distinct']} "
          f"distinct contributors — {result['stopped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

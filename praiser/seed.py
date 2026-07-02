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
from .contribindex import ContributorIndex
from .forge import GitHubForge
from .github_client import RateLimitError

SEED_PAGES = 5          # contributors pages (100 each) — GitHub caps ~500 anyway
MIN_REST = 400          # stop if REST quota dips below this
SEED_TTL = 2_592_000    # 30 days: re-seed a repo only after this (matches index TTL)


def _rest_remaining(forge) -> int | None:
    m = re.search(r"(\d+)\s*/\s*\d+", forge.rate_summary() or "")
    return int(m.group(1)) if m else None


def seed_repo(name_with_owner, *, forge, index, cache, force=False) -> int | None:
    """Seed one repo's contributor roster into the index. Returns the number of
    contributors indexed, or None if skipped (already seeded within SEED_TTL).
    Raises RateLimitError to let the caller stop."""
    marker = Cache.key("roster-seeded", name_with_owner)
    if not force and cache.has(marker):
        return None  # seeded within SEED_TTL — skip (re-seeds once it expires)
    owner, _, repo = name_with_owner.partition("/")
    contribs = forge.repo_contributors(owner, repo, max_pages=SEED_PAGES)
    n = 0
    if contribs:
        roster = {c.login.lower(): c.contributions for c in contribs if c.login}
        index.record_rosters({name_with_owner: roster})
        n = len(roster)
    cache.set(marker, True)
    return n


def seed_one(name_with_owner, *, forge, index, cache, log=lambda m: None) -> dict:
    """Seed a single specified repo (for when only one repo is of interest)."""
    try:
        n = seed_repo(name_with_owner, forge=forge, index=index, cache=cache)
    except RateLimitError as exc:
        return {"repo": name_with_owner, "seeded": 0, "contributors_indexed": 0,
                "repos_available": 1, "stopped": f"rate limit ({exc.reset_in}s)"}
    if n is None:
        return {"repo": name_with_owner, "seeded": 0, "contributors_indexed": 0,
                "repos_available": 1, "stopped": "already seeded (within 30 days)"}
    log(f"seeded {name_with_owner} ({n} contributors)")
    return {"repo": name_with_owner, "seeded": 1, "contributors_indexed": n,
            "repos_available": 1, "stopped": "all repos seeded"}


def seed_org(org, *, forge, index, cache, budget=50, log=lambda m: None) -> dict:
    """Seed the reverse-index from ``org``'s repos. Returns a small summary.

    Skips repos seeded within SEED_TTL (resumable across periodic runs); stops at
    ``budget`` newly-seeded repos or when the REST quota runs low.
    """
    try:
        repos = forge.organization_repositories(org)
    except RateLimitError as exc:
        return {"org": org, "seeded": 0, "stopped": f"rate limit ({exc.reset_in}s)"}

    seeded = indexed_contributors = 0
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
            n = seed_repo(meta.name_with_owner, forge=forge, index=index, cache=cache)
        except RateLimitError as exc:
            stopped = f"rate limit ({exc.reset_in}s)"
            break
        if n is None:
            continue  # already seeded within SEED_TTL
        indexed_contributors += n
        seeded += 1
        log(f"seeded {meta.name_with_owner} ({n} contributors)")
    return {"org": org, "seeded": seeded,
            "contributors_indexed": indexed_contributors,
            "repos_available": len(repos),
            "stopped": stopped or "all repos seeded"}


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
    print(f"[seed] {result['org']}: seeded {result['seeded']} repo(s), "
          f"{result['contributors_indexed']} contributor entries "
          f"(of {result['repos_available']} available) — {result['stopped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

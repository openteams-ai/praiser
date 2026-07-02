"""Contributor reverse-index — collaborative discovery (issue #59).

GitHub exposes *repo → contributors* but no complete *user → contributed-repos*
lookup, so a historical direct-committer (old commits, no owned fork) has no
person-side signal to the repo. But praiser already fetches a repo's full
top-roster whenever it attributes that repo for anyone. Retaining that as a
reverse map — ``login -> {repos where they're a substantial contributor}`` —
reconstructs the missing lookup: the more people scanned, the more repos get
indexed, so such users become discoverable once *anyone* leading to their repo
has been scanned.

Backed by the shared/local ``Cache`` (inherits TTL + the web app's Redis
backend). One entry per login. Purely additive: it only proposes discovery
candidates; attribution re-verifies via the contributor count, so no false
positive can leak.
"""

from .cache import Cache

# Only index a contributor with at least this many commits — keeps the index to
# plausibly-elevated people and bounds its size (drive-by committers excluded).
MIN_COMMITS = 15
_PREFIX = "contrib-index"


class ContributorIndex:
    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    def repos_for(self, login: str) -> list[str]:
        """Repos where ``login`` was recorded as a substantial contributor."""
        val = self._cache.get(Cache.key(_PREFIX, login.lower()))
        return list(val) if isinstance(val, list) else []

    def record_rosters(self, rosters: dict[str, dict[str, int]]) -> None:
        """Fold ``{repo: {login: commits}}`` into the reverse index.

        Called once after attribution (single-threaded), so no locking needed.
        Merges into any existing per-login entry, capped and deduped.
        """
        # Invert to login -> set(repos) for the rosters seen this run.
        fresh: dict[str, set[str]] = {}
        for repo, roster in rosters.items():
            for login, commits in (roster or {}).items():
                if commits >= MIN_COMMITS:
                    fresh.setdefault(login.lower(), set()).add(repo)
        for login, repos in fresh.items():
            key = Cache.key(_PREFIX, login)
            existing = self._cache.get(key)
            merged = list(dict.fromkeys(
                (existing if isinstance(existing, list) else []) + sorted(repos)
            ))
            self._cache.set(key, merged)

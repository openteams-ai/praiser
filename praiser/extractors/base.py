"""Extractor interface + shared context.

Each extractor maps one role-recording convention (CODEOWNERS, MAINTAINERS,
package manifests, enhancement-proposal series, governance prose) to a list of
``Evidence``. The *parsing* logic of every extractor is kept in a module-level
pure function so it can be unit-tested offline with no network.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..forge import Forge
from ..models import Evidence, Identity, PackageRef
from ..registry import KnownProject, KnownProjects


@dataclass
class ExtractContext:
    """Everything an extractor needs at run time."""

    identity: Identity
    forge: Forge  # the code host to read from (GitHub, GitLab, …)
    registry: KnownProjects
    llm: object | None = None  # praiser.llm.LLM or None when disabled
    org_logins: set[str] = field(default_factory=set)  # orgs the user belongs to
    popularity_floor: int = 0  # gate expensive per-repo checks on stars
    canonical_stars: int = 1000  # at/above this a repo is plausibly the original
    canonical_forks: int = 100   # ...or widely forked enough to be the original
    contributor_pages: int = 2  # contributors API pages (100 each) to fetch
    auto_discover_roles: bool = False  # find role pages via LLM + web search
    use_wikidata: bool = False  # derive creator/developer roles from Wikidata
    role_discovery_floor: int = 1000   # floor for external role lookup (LLM/Wikidata)
    # Shared/durable cache for repo-level, time-independent lookups (founder/
    # creator resolution from Wikidata/Wikipedia). On the web it's the shared
    # Redis; on the CLI the on-disk cache. Keyed per REPO (not per user), so a
    # repo's founders are resolved once and reused by every scan — decoupling the
    # founder roles from live WDQS reachability, which throttles cloud IPs (#108).
    # Uses the cache's normal (finite) TTL so the keyspace stays bounded.
    founder_cache: object | None = None
    manual_repos: set[str] = field(default_factory=set)  # user-vouched repos
    # user-vouched subcomponents: repo -> [paths]
    manual_subcomponents: dict[str, list[str]] = field(default_factory=dict)
    # owner/repo (lowercased) -> packages it ships (registry maintainer signal)
    package_index: dict[str, list[PackageRef]] = field(default_factory=dict)
    # repo -> {login: commit_count} | None (None = could not fetch)
    _contrib_cache: dict[str, dict[str, int] | None] = field(default_factory=dict)
    # repo -> [role-source dicts] discovered this run (for --save-registry)
    _discovered: dict[str, list[dict]] = field(default_factory=dict)
    _discovered_lock: object = field(default_factory=threading.Lock)

    def known(self, name_with_owner: str) -> KnownProject | None:
        return self.registry.get(name_with_owner)

    def note_discovered(self, name_with_owner: str, sources: list[dict]) -> None:
        """Record web-discovered role sources for a repo (thread-safe)."""
        if not sources:
            return
        with self._discovered_lock:
            self._discovered.setdefault(name_with_owner, list(sources))

    def discovered_sources(self) -> dict[str, list[dict]]:
        with self._discovered_lock:
            return {k: list(v) for k, v in self._discovered.items()}

    def fetched_rosters(self) -> dict[str, dict[str, int]]:
        """Contributor rosters actually fetched this run: {repo: {login: commits}}.
        Feeds the contributor reverse-index (#59); skips repos that couldn't be
        fetched (None)."""
        return {k: v for k, v in self._contrib_cache.items() if v}

    def contributors(self, candidate) -> dict[str, int] | None:
        """Cached {login: commits} for a repo, or None if it couldn't be fetched."""
        key = candidate.name_with_owner
        if key not in self._contrib_cache:
            raw = self.forge.repo_contributors(
                candidate.owner, candidate.repo, max_pages=self.contributor_pages
            )
            if raw is None:
                self._contrib_cache[key] = None
            else:
                self._contrib_cache[key] = {
                    c.login.lower(): c.contributions for c in raw if c.login
                }
        return self._contrib_cache[key]

    def contributor_total(self, candidate, fetched: int) -> tuple[int, bool, bool]:
        """``(total_contributors, capped, approx)`` for the ``#R/N`` display.

        Priority: (1) a curated/cached **registry snapshot** — stable, offline,
        and authoritative for big projects, but *approximate* (frozen); (2) if
        the fetched contributor list did NOT hit our page cap, the fetched length
        is *exact*; (3) otherwise the list was truncated, so ask the forge for the
        true total in one request (uncapped identity count) — *approximate* (it
        drifts daily). Only if that's unavailable do we return the fetched length
        as a lower bound (``capped=True`` → rendered ``N+``).

        ``approx=True`` (snapshot or resolved total) renders rounded, e.g. ``~6800``.
        """
        snap = self.registry.contributor_count(candidate.name_with_owner)
        if snap:
            return snap, False, True                 # snapshot: approximate
        if fetched < max(1, self.contributor_pages) * 100:
            return fetched, False, False             # fetched everyone: exact
        total = self.forge.repo_contributor_count(candidate.owner, candidate.repo)
        if total and total >= fetched:
            return total, False, True                # real total: approximate
        return fetched, True, False                  # lower bound -> "N+"

    def trust_role_file(self, candidate) -> bool:
        """Whether a CODEOWNERS/AUTHORS match here is trustworthy vs inherited.

        Must use COPY-RESISTANT signals only: a vendored repo carries the
        upstream's full git history *and* its role files, so "is the user a
        contributor" is useless (their copied commits show up everywhere). What
        a copy cannot fake is affiliation (the repo being under the user's own
        account or org) or being the popular, canonical project itself.
        """
        if candidate.name_with_owner in self.manual_repos:
            return True  # the user explicitly vouched for this repo
        if self.identity.matches_handle(candidate.owner):
            return True  # the repo is under the user's own account
        if candidate.owner.lower() in self.org_logins:
            return True  # belongs to an org the user is in
        # The canonical project is far more popular than any vendored copy.
        return (
            candidate.stars >= self.canonical_stars
            or candidate.forks >= self.canonical_forks
        )


class Extractor(ABC):
    name: str = "base"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        """Cheap pre-check; default True (extract decides definitively)."""
        return True

    @abstractmethod
    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        """Return evidence that ctx.identity holds a role in ``candidate``."""
        raise NotImplementedError

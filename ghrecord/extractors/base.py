"""Extractor interface + shared context.

Each extractor maps one role-recording convention (CODEOWNERS, MAINTAINERS,
package manifests, enhancement-proposal series, governance prose) to a list of
``Evidence``. The *parsing* logic of every extractor is kept in a module-level
pure function so it can be unit-tested offline with no network.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..github_client import GitHubClient
from ..models import Evidence, Identity
from ..registry import KnownProject, KnownProjects


@dataclass
class ExtractContext:
    """Everything an extractor needs at run time."""

    identity: Identity
    client: GitHubClient
    registry: KnownProjects
    llm: object | None = None  # ghrecord.llm.LLM or None when disabled
    org_logins: set[str] = field(default_factory=set)  # orgs the user belongs to
    popularity_floor: int = 0  # gate expensive per-repo checks on stars
    canonical_stars: int = 1000  # at/above this a repo is plausibly the original
    canonical_forks: int = 100   # ...or widely forked enough to be the original
    contributor_pages: int = 2  # contributors API pages (100 each) to fetch
    auto_discover_roles: bool = False  # find role pages via LLM + web search
    role_discovery_floor: int = 1000   # only auto-discover for repos this popular
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

    def contributors(self, candidate) -> dict[str, int] | None:
        """Cached {login: commits} for a repo, or None if it couldn't be fetched."""
        key = candidate.name_with_owner
        if key not in self._contrib_cache:
            raw = self.client.repo_contributors(
                candidate.owner, candidate.repo, max_pages=self.contributor_pages
            )
            if raw is None:
                self._contrib_cache[key] = None
            else:
                self._contrib_cache[key] = {
                    c["login"].lower(): c.get("contributions", 0)
                    for c in raw if c.get("login")
                }
        return self._contrib_cache[key]

    def trust_role_file(self, candidate) -> bool:
        """Whether a CODEOWNERS/AUTHORS match here is trustworthy vs inherited.

        Must use COPY-RESISTANT signals only: a vendored repo carries the
        upstream's full git history *and* its role files, so "is the user a
        contributor" is useless (their copied commits show up everywhere). What
        a copy cannot fake is affiliation (the repo being under the user's own
        account or org) or being the popular, canonical project itself.
        """
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

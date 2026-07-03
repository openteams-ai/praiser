"""Phase 1 — candidate discovery (wide net).

Sources, strongest first:
* owned repos + org repos the user belongs to,
* repos the user has contributed to (over-collects on purpose; Phase 2 filters),
* full commit history (recovers repos older than the rolling contribution graph),
* self-reported repos linked from the profile README,
* code/commit search for the handle, name search in credit files,
* packages the user maintains on a registry, and registry seeds.

Returns deduped ``Candidate`` objects; forks are dropped unless they are also a
registry seed. All forge access goes through the :class:`~praiser.forge.Forge`
interface — this module is platform-agnostic.
"""

import re

from .forge import Forge, RepoMeta
from .models import Candidate, Identity, PackageRef
from .registry import KnownProjects

_REPO_LINK_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
_NON_REPO_OWNERS = {"sponsors", "orgs", "users", "topics", "features", "about",
                    "settings", "marketplace", "apps"}

# Role-bearing files to look for the handle in via code search.
SEARCH_FILES = ["CODEOWNERS", "MAINTAINERS", "OWNERS", "GOVERNANCE"]
# Credit files to look for the user's *name* in (catches founders/authors whose
# only recorded involvement is an AUTHORS/THANKS credit, e.g. SciPy).
SEARCH_AUTHOR_FILES = ["AUTHORS", "THANKS", "CONTRIBUTORS"]


def org_logins(forge: Forge, login: str) -> set[str]:
    """Lowercased org/group logins the user belongs to."""
    return {o.lower() for o in forge.user_organizations(login) if o}


def keep_candidate(
    c: Candidate, registry: KnownProjects, *, include_private: bool
) -> bool:
    """Whether a discovered repo is worth scanning.

    Registry/known projects are always kept. Otherwise forks are dropped (they
    inherit upstream role files) and private repos are dropped by default (a
    public 'popular projects' record shouldn't surface, or leak, private repos).
    """
    if "manual" in c.sources:
        return True  # the user explicitly asked for this repo
    if c.name_with_owner in registry:
        return True
    if c.is_fork:
        return False
    if c.is_private and not include_private:
        return False
    return True


def discover(
    forge: Forge,
    identity: Identity,
    registry: KnownProjects,
    *,
    include_org_repos: bool = True,
    use_code_search: bool = True,
    include_private: bool = False,
    extra_repos: list[str] | None = None,
    package_refs: list[PackageRef] | None = None,
    index_repos: list[str] | None = None,
) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    # Names whose fork/star metadata is authoritative (came from the forge with
    # full metadata). Anything else (search/registry) is resolved before the
    # fork filter, since forks inherit upstream role files (false positives).
    has_meta: set[str] = set()
    # Subset of has_meta whose star/fork counts were real (not a null from a
    # partial/degraded response) — the rest get their metrics re-fetched (#120).
    has_real_metrics: set[str] = set()

    web_host = getattr(forge, "web_base", None) or None

    def add_meta(meta: RepoMeta, source: str) -> None:
        c = candidates.get(meta.name_with_owner)
        if c is None:
            c = Candidate(name_with_owner=meta.name_with_owner,
                          forge=forge.name, web_host=web_host)
            candidates[meta.name_with_owner] = c
        c.sources.add(source)
        c.stars = max(c.stars, meta.stars)
        c.forks = max(c.forks, meta.forks)
        c.is_fork = meta.is_fork
        c.is_private = meta.is_private
        c.pushed_at = meta.pushed_at or c.pushed_at
        has_meta.add(meta.name_with_owner)
        if meta.metrics_known:                 # got a trustworthy star/fork count
            has_real_metrics.add(meta.name_with_owner)

    def add_name(name_with_owner: str, source: str) -> None:
        c = candidates.get(name_with_owner)
        if c is None:
            c = Candidate(name_with_owner=name_with_owner,
                          forge=forge.name, web_host=web_host)
            candidates[name_with_owner] = c
        c.sources.add(source)

    login = identity.primary_login
    for meta in forge.user_repositories(login):
        add_meta(meta, "owned")
        # A personal fork of a canonical repo is the bridge to a project whose
        # (often old/graph-invisible) contributions no person-side signal
        # surfaces — add its upstream parent as a candidate (#58). The fork
        # itself is dropped by the fork filter; attribution still gates whether
        # the parent earns a role, so a no-real-contribution fork adds nothing.
        if meta.is_fork and meta.parent:
            add_name(meta.parent, "fork-parent")
    for meta in forge.user_contributed_repositories(login):
        add_meta(meta, "contributed")
    if include_org_repos:
        for org in forge.user_organizations(login):
            for meta in forge.organization_repositories(org):
                add_meta(meta, "org-repo")
    for meta in forge.user_commit_history(login):
        add_meta(meta, "history")

    _profile_links(forge, identity, add_name)
    if use_code_search:
        _code_search(forge, identity, add_name)
        _name_search(forge, identity, add_name)
        _commit_search(forge, identity, add_name)

    # Registry seeds are GitHub-keyed curated projects (python/peps, numpy/…);
    # they only exist on GitHub, so skip them on other forges — otherwise every
    # seed becomes a candidate that costs a sequential 404 to enrich (forges
    # without batch metadata would crawl).
    if forge.name == "github":
        for seed in registry.seeds():
            add_name(seed.name_with_owner, "registry")

    # Repos the user ships packages from on PyPI/npm/crates — catches projects
    # where their role is "package maintainer" rather than "top committer".
    for ref in package_refs or []:
        if ref.repo:
            add_name(ref.repo, f"pkg:{ref.registry}")

    # Repos where the contributor reverse-index (#59) recorded this user as a
    # substantial contributor — recovers direct committers with no person-side
    # signal (e.g. old contributions). NOT trusted like "manual": they pass the
    # normal fork filter and attribution gate.
    for repo in index_repos or []:
        if "/" in repo:
            add_name(repo, "reverse-index")

    # User-supplied repos the tool didn't find on its own.
    for repo in extra_repos or []:
        if "/" in repo:
            add_name(repo, "manual")

    # Resolve fork/star metadata for search & registry candidates so the fork
    # filter below is accurate. This matters a lot: forks inherit the upstream
    # CODEOWNERS/MAINTAINERS, so a match on a fork is a false positive.
    unknown = [n for n in candidates if n not in has_meta]
    metas = forge.repositories_metadata(unknown)
    for nwo in unknown:
        c = candidates[nwo]
        meta = metas.get(nwo)
        if meta is None:  # deleted/inaccessible/renamed: treat as a fork to drop it
            c.is_fork = True
            continue
        c.is_fork = meta.is_fork
        c.is_private = meta.is_private
        c.stars = max(c.stars, meta.stars)
        c.forks = max(c.forks, meta.forks)
        c.pushed_at = meta.pushed_at or c.pushed_at

    # Re-fetch metrics for KNOWN-real (add_meta) candidates whose star/fork counts
    # came back null from a partial/degraded discovery response (#120) — the
    # discovery query and repositories_metadata are separate calls, so the retry
    # can succeed. Metrics-only: never drop these (they are real repos), unlike
    # the unresolved-search-hit handling above.
    stale_metrics = [n for n in has_meta if n not in has_real_metrics]
    if stale_metrics:
        remetas = forge.repositories_metadata(stale_metrics)
        for nwo in stale_metrics:
            meta = remetas.get(nwo)
            if meta is not None and meta.metrics_known:
                c = candidates[nwo]
                c.stars = max(c.stars, meta.stars)
                c.forks = max(c.forks, meta.forks)

    # Drop forks and (by default) private repos unless they are registry seeds.
    return [
        c for c in candidates.values()
        if keep_candidate(c, registry, include_private=include_private)
    ]


def _code_search(forge: Forge, identity: Identity, add_name) -> None:
    handle = identity.primary_login
    for fname in SEARCH_FILES:
        try:
            hits = forge.search_file_mentions(handle, fname)
        except Exception:
            hits = []
        for hit in hits:
            add_name(hit.name_with_owner, f"search:{fname}")


def _name_search(forge: Forge, identity: Identity, add_name) -> None:
    """Find repos crediting the user's name in AUTHORS/THANKS/CONTRIBUTORS."""
    for name in identity.names:
        if len(name) < 5:
            continue
        for fname in SEARCH_AUTHOR_FILES:
            try:
                hits = forge.search_file_mentions(name, fname)
            except Exception:
                hits = []
            for hit in hits:
                add_name(hit.name_with_owner, f"namesearch:{fname}")


def _commit_search(forge: Forge, identity: Identity, add_name) -> None:
    """Find repos the user has authored commits in (any age, recent first).

    Searches by author *name* (catches commits authored under emails not linked
    to the account — see #22), plus by login for forges that support it. Phase 2
    filters name-ambiguity false positives (non-contributors get no role).
    """
    repos: list[str] = []
    try:
        repos += forge.search_commits_by_author(identity.primary_login)
    except Exception:
        pass
    for name in identity.names:
        if len(name) < 5:  # short names are too ambiguous
            continue
        try:
            repos += forge.search_commits_by_name(name)
        except Exception:
            pass
    for nwo in dict.fromkeys(repos):
        add_name(nwo, "commit-search")


def _profile_links(forge: Forge, identity: Identity, add_name) -> None:
    """Add owner/repo links from the user's profile README (self-reported work)."""
    login = identity.primary_login
    text = forge.get_file(login, login, "README.md")
    if not text:
        return
    for match in _REPO_LINK_RE.findall(text):
        repo = match.rstrip("./")
        owner = repo.split("/", 1)[0]
        if repo.count("/") == 1 and owner.lower() not in _NON_REPO_OWNERS:
            add_name(repo, "profile")

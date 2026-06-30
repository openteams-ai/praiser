"""Phase 1 — candidate discovery (wide net).

Sources, strongest first:
* owned repos + org repos the user belongs to,
* repos the user has contributed to (over-collects on purpose; Phase 2 filters),
* code search for the handle in CODEOWNERS/MAINTAINERS/OWNERS/GOVERNANCE,
* registry seeds (standards repos etc. — always checked).

Returns deduped ``Candidate`` objects; forks are dropped unless they are also a
registry seed.
"""

import json

from .github_client import GitHubClient
from .models import Candidate, Identity
from .registry import KnownProjects

DISCOVERY_QUERY = """
query($login:String!) {
  user(login:$login) {
    login name company
    organizations(first:100){ nodes{ login } }
    repositories(first:100, ownerAffiliations:[OWNER],
        orderBy:{field:STARGAZERS, direction:DESC}){
      nodes{ nameWithOwner stargazerCount forkCount isFork isPrivate pushedAt }
    }
    repositoriesContributedTo(first:100,
        contributionTypes:[COMMIT, PULL_REQUEST],
        orderBy:{field:STARGAZERS, direction:DESC}){
      nodes{ nameWithOwner stargazerCount forkCount isFork isPrivate pushedAt }
    }
  }
}
"""

ORG_REPOS_QUERY = """
query($org:String!) {
  organization(login:$org) {
    repositories(first:30, orderBy:{field:STARGAZERS, direction:DESC}){
      nodes{ nameWithOwner stargazerCount forkCount isFork isPrivate pushedAt }
    }
  }
}
"""

# Role-bearing files to look for the handle in via code search.
SEARCH_FILES = ["CODEOWNERS", "MAINTAINERS", "OWNERS", "GOVERNANCE"]
# Credit files to look for the user's *name* in (catches founders/authors whose
# only recorded involvement is an AUTHORS/THANKS credit, e.g. SciPy).
SEARCH_AUTHOR_FILES = ["AUTHORS", "THANKS", "CONTRIBUTORS"]


def org_logins(client: GitHubClient, login: str) -> set[str]:
    """Lowercased org logins the user belongs to (reuses the cached query)."""
    data = client.graphql(DISCOVERY_QUERY, {"login": login})
    user = (data or {}).get("user") or {}
    nodes = (user.get("organizations") or {}).get("nodes", []) or []
    return {o["login"].lower() for o in nodes if o.get("login")}


def keep_candidate(
    c: Candidate, registry: KnownProjects, *, include_private: bool
) -> bool:
    """Whether a discovered repo is worth scanning.

    Registry/known projects are always kept. Otherwise forks are dropped (they
    inherit upstream role files) and private repos are dropped by default (a
    public 'popular projects' record shouldn't surface, or leak, private repos).
    """
    if c.name_with_owner in registry:
        return True
    if c.is_fork:
        return False
    if c.is_private and not include_private:
        return False
    return True


def discover(
    client: GitHubClient,
    identity: Identity,
    registry: KnownProjects,
    *,
    include_org_repos: bool = True,
    use_code_search: bool = True,
    include_private: bool = False,
) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    # Names whose fork/star metadata is authoritative (came from a GraphQL node).
    # Anything else (code search / registry) is resolved before fork filtering.
    has_meta_names: set[str] = set()

    def add(node: dict, source: str) -> None:
        nwo = node.get("nameWithOwner")
        if not nwo:
            return
        has_meta = "stargazerCount" in node  # GraphQL node carries fork/star info
        existing = candidates.get(nwo)
        if existing is None:
            existing = Candidate(
                name_with_owner=nwo,
                stars=node.get("stargazerCount", 0) or 0,
                forks=node.get("forkCount", 0) or 0,
                is_fork=bool(node.get("isFork")),
                is_private=bool(node.get("isPrivate")),
                pushed_at=node.get("pushedAt"),
            )
            candidates[nwo] = existing
        existing.sources.add(source)
        existing.stars = max(existing.stars, node.get("stargazerCount", 0) or 0)
        if has_meta:
            has_meta_names.add(nwo)
            existing.is_private = bool(node.get("isPrivate"))
            existing.pushed_at = node.get("pushedAt") or existing.pushed_at

    data = client.graphql(DISCOVERY_QUERY, {"login": identity.primary_login})
    user = (data or {}).get("user") or {}

    for node in (user.get("repositories") or {}).get("nodes", []) or []:
        add(node, "owned")
    for node in (user.get("repositoriesContributedTo") or {}).get("nodes", []) or []:
        add(node, "contributed")

    orgs = [o["login"] for o in (user.get("organizations") or {}).get("nodes", [])]
    if include_org_repos:
        for org in orgs:
            odata = client.graphql(ORG_REPOS_QUERY, {"org": org})
            org_node = (odata or {}).get("organization") or {}
            for node in (org_node.get("repositories") or {}).get("nodes", []) or []:
                add(node, "org-repo")

    if use_code_search:
        _code_search(client, identity, add)
        _name_search(client, identity, add)
        _commit_search(client, identity, add)

    # Always check registry seeds (popularity filled in Phase 3).
    for seed in registry.seeds():
        add({"nameWithOwner": seed.name_with_owner}, "registry")

    # Resolve fork/star metadata for code-search & registry candidates so the
    # fork filter below is accurate. This matters a lot: forks inherit the
    # upstream CODEOWNERS/MAINTAINERS, so a match on a fork is a false positive.
    unknown = [
        candidates[n] for n in candidates if n not in has_meta_names
    ]
    _enrich_metadata(client, unknown)

    # Drop forks and (by default) private repos unless they are registry seeds.
    return [
        c for c in candidates.values()
        if keep_candidate(c, registry, include_private=include_private)
    ]


def _enrich_metadata(client: GitHubClient, cands: list[Candidate]) -> None:
    """Batch-fetch isFork/stars/forks via aliased GraphQL (≤50 repos/query)."""
    for i in range(0, len(cands), 50):
        batch = cands[i : i + 50]
        parts = []
        for j, c in enumerate(batch):
            parts.append(
                f"r{j}: repository(owner:{json.dumps(c.owner)}, "
                f"name:{json.dumps(c.repo)}) "
                "{ isFork isPrivate stargazerCount forkCount pushedAt }"
            )
        query = "query{" + " ".join(parts) + "}"
        try:
            data = client.graphql(query, {})
        except Exception:
            continue
        for j, c in enumerate(batch):
            repo = (data or {}).get(f"r{j}")
            if not repo:  # deleted/inaccessible/renamed: treat as a fork to drop it
                c.is_fork = True
                continue
            c.is_fork = bool(repo.get("isFork"))
            c.is_private = bool(repo.get("isPrivate"))
            c.stars = max(c.stars, repo.get("stargazerCount", 0) or 0)
            c.forks = max(c.forks, repo.get("forkCount", 0) or 0)
            c.pushed_at = repo.get("pushedAt") or c.pushed_at


def _code_search(client: GitHubClient, identity: Identity, add) -> None:
    handle = identity.primary_login
    for fname in SEARCH_FILES:
        try:
            items = client.search_code(f"{handle} filename:{fname}")
        except Exception:
            items = []
        for item in items:
            repo = (item.get("repository") or {}).get("full_name")
            if repo:
                add({"nameWithOwner": repo}, f"search:{fname}")


def _name_search(client: GitHubClient, identity: Identity, add) -> None:
    """Find repos crediting the user's name in AUTHORS/THANKS/CONTRIBUTORS."""
    for name in identity.names:
        if len(name) < 5:
            continue
        for fname in SEARCH_AUTHOR_FILES:
            try:
                items = client.search_code(f'"{name}" filename:{fname}')
            except Exception:
                items = []
            for item in items:
                repo = (item.get("repository") or {}).get("full_name")
                if repo:
                    add({"nameWithOwner": repo}, f"namesearch:{fname}")


def _commit_search(client: GitHubClient, identity: Identity, add) -> None:
    """Find repos the user has authored commits in (any age, recent first)."""
    try:
        items = client.search_commits(f"author:{identity.primary_login}")
    except Exception:
        items = []
    for item in items:
        repo = (item.get("repository") or {}).get("full_name")
        if repo:
            add({"nameWithOwner": repo}, "commit-search")

"""GitHub implementation of the Forge interface.

Wraps a transport-level :class:`~praiser.github_client.GitHubClient` (GraphQL +
REST) and turns its raw JSON into the neutral types in :mod:`praiser.forge.base`.
This is where every GitHub-specific detail is sealed off — the GraphQL queries,
the ``contributionsCollection`` history walk, GitHub's search syntax, its field
names — so the rest of praiser can stay forge-agnostic.
"""

import datetime
import json
from typing import Any

from ..cache import Cache
from ..github_client import GitHubClient
from ._http import extract_urls
from .base import ContributorCount, DirEntry, FileHit, Forge, RepoMeta, UserRef

# Repo fields we ask for everywhere a GraphQL node is returned.
_REPO_FIELDS = ("nameWithOwner stargazerCount forkCount isFork isPrivate pushedAt "
                "parent{ nameWithOwner }")

_USER_QUERY = """
query($login:String!) { user(login:$login) { login name } }
"""

# Owned repos + contributed-to repos + org memberships, in one round-trip.
_DISCOVERY_QUERY = f"""
query($login:String!) {{
  user(login:$login) {{
    login name
    organizations(first:100){{ nodes{{ login }} }}
    repositories(first:100, ownerAffiliations:[OWNER],
        orderBy:{{field:STARGAZERS, direction:DESC}}){{
      nodes{{ {_REPO_FIELDS} }}
    }}
    repositoriesContributedTo(first:100,
        contributionTypes:[COMMIT, PULL_REQUEST],
        orderBy:{{field:STARGAZERS, direction:DESC}}){{
      nodes{{ {_REPO_FIELDS} }}
    }}
  }}
}}
"""

_ORG_REPOS_QUERY = f"""
query($org:String!) {{
  organization(login:$org) {{
    repositories(first:30, orderBy:{{field:STARGAZERS, direction:DESC}}){{
      nodes{{ {_REPO_FIELDS} }}
    }}
  }}
}}
"""

# Per-year commit contributions — recovers repos older than the rolling ~1-year
# contribution graph (e.g. a former employer's repo).
_HISTORY_QUERY = f"""
query($login:String!, $from:DateTime!, $to:DateTime!) {{
  user(login:$login) {{
    contributionsCollection(from:$from, to:$to) {{
      commitContributionsByRepository(maxRepositories:100) {{
        repository {{ {_REPO_FIELDS} }}
      }}
    }}
  }}
}}
"""


def _meta_from_node(node: dict | None) -> RepoMeta | None:
    """Adapt a GraphQL repository node into a neutral RepoMeta."""
    if not node or not node.get("nameWithOwner"):
        return None
    return RepoMeta(
        name_with_owner=node["nameWithOwner"],
        stars=node.get("stargazerCount", 0) or 0,
        forks=node.get("forkCount", 0) or 0,
        is_fork=bool(node.get("isFork")),
        is_private=bool(node.get("isPrivate")),
        pushed_at=node.get("pushedAt"),
        parent=(node.get("parent") or {}).get("nameWithOwner"),
    )


def _metas_from_nodes(nodes: list[dict] | None) -> list[RepoMeta]:
    return [m for n in (nodes or []) if (m := _meta_from_node(n)) is not None]


class GitHubForge(Forge):
    name = "github"
    WEB_HOST = "https://github.com"
    web_base = WEB_HOST  # instance web host (constant for github.com)

    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        max_retries: int = 3,
        verbose: bool = False,
    ) -> None:
        self._client = GitHubClient(
            token, cache, max_retries=max_retries, verbose=verbose
        )
        # DISCOVERY_QUERY answers three methods; memoise so they share one call
        # (the client also caches, but this avoids re-parsing per login).
        self._discovery: dict[str, dict] = {}

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self.WEB_HOST}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self._client.get_file(owner, repo, path, ref)

    def get_files(
        self, owner: str, repo: str, paths: list[str], ref: str | None = None
    ) -> dict[str, str | None]:
        # GitHub batches up to 50 blobs per GraphQL query — far better than the
        # one-call-per-file default.
        return self._client.get_files(owner, repo, paths, ref)

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        return [
            DirEntry(
                name=e.get("name", ""),
                path=e.get("path", ""),
                is_dir=e.get("type") == "dir",
            )
            for e in self._client.list_dir(owner, repo, path)
            if e.get("name")
        ]

    # -- repository metadata ------------------------------------------------
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        data = self._client.rest_json(f"/repos/{owner}/{repo}")
        if not isinstance(data, dict):
            return None
        return RepoMeta(
            name_with_owner=data.get("full_name") or f"{owner}/{repo}",
            stars=data.get("stargazers_count", 0) or 0,
            forks=data.get("forks_count", 0) or 0,
            is_fork=bool(data.get("fork")),
            is_private=bool(data.get("private")),
            pushed_at=data.get("pushed_at"),
        )

    def repositories_metadata(
        self, names_with_owner: list[str]
    ) -> dict[str, RepoMeta]:
        """Batch metadata via aliased GraphQL (≤50 repos/query).

        Repos that are deleted/renamed/inaccessible are simply absent from the
        result — callers decide what that means (discovery drops them).
        """
        out: dict[str, RepoMeta] = {}
        for i in range(0, len(names_with_owner), 50):
            batch = names_with_owner[i : i + 50]
            parts = []
            for j, nwo in enumerate(batch):
                owner, _, repo = nwo.partition("/")
                parts.append(
                    f"r{j}: repository(owner:{json.dumps(owner)}, "
                    f"name:{json.dumps(repo)}) {{ {_REPO_FIELDS} }}"
                )
            try:
                data = self._client.graphql("query{" + " ".join(parts) + "}", {})
            except Exception:
                continue
            for j, nwo in enumerate(batch):
                node = (data or {}).get(f"r{j}")
                # The alias query omits nameWithOwner-less nodes; stamp the key
                # we asked for so the result is addressable by the input slug.
                meta = _meta_from_node(node)
                if meta is not None:
                    meta.name_with_owner = nwo
                    out[nwo] = meta
        return out

    # -- people & projects --------------------------------------------------
    def _discovery_data(self, login: str) -> dict:
        if login not in self._discovery:
            data = self._client.graphql(_DISCOVERY_QUERY, {"login": login})
            self._discovery[login] = ((data or {}).get("user") or {})
        return self._discovery[login]

    def resolve_user(self, login: str) -> UserRef | None:
        data = self._client.graphql(_USER_QUERY, {"login": login})
        user = (data or {}).get("user") or {}
        if not user.get("login"):
            return None
        return UserRef(login=user["login"], name=user.get("name"))

    def user_repositories(self, login: str) -> list[RepoMeta]:
        user = self._discovery_data(login)
        return _metas_from_nodes((user.get("repositories") or {}).get("nodes"))

    def user_contributed_repositories(self, login: str) -> list[RepoMeta]:
        user = self._discovery_data(login)
        return _metas_from_nodes(
            (user.get("repositoriesContributedTo") or {}).get("nodes")
        )

    def user_organizations(self, login: str) -> list[str]:
        user = self._discovery_data(login)
        nodes = (user.get("organizations") or {}).get("nodes", []) or []
        return [o["login"] for o in nodes if o.get("login")]

    def organization_repositories(self, org: str) -> list[RepoMeta]:
        data = self._client.graphql(_ORG_REPOS_QUERY, {"org": org})
        org_node = (data or {}).get("organization") or {}
        return _metas_from_nodes((org_node.get("repositories") or {}).get("nodes"))

    def user_commit_history(self, login: str) -> list[RepoMeta]:
        prof = self._client.graphql(
            "query($l:String!){user(login:$l){createdAt}}", {"l": login}
        )
        created = ((prof or {}).get("user") or {}).get("createdAt")
        if not created:
            return []
        start_year = int(created[:4])
        end_year = datetime.datetime.now(datetime.timezone.utc).year
        by_name: dict[str, RepoMeta] = {}  # dedupe, keep first (richest) seen
        for year in range(start_year, end_year + 1):
            data = self._client.graphql(_HISTORY_QUERY, {
                "login": login,
                "from": f"{year}-01-01T00:00:00Z",
                "to": f"{year}-12-31T23:59:59Z",
            })
            coll = (((data or {}).get("user") or {})
                    .get("contributionsCollection") or {})
            for item in coll.get("commitContributionsByRepository", []) or []:
                meta = _meta_from_node(item.get("repository"))
                if meta is not None:
                    by_name.setdefault(meta.name_with_owner, meta)
        return list(by_name.values())

    def team_members(self, org: str, team: str) -> list[str]:
        return self._client.team_members(org, team)

    def profile_links(self, login: str) -> list[str]:
        data = self._client.graphql(
            "query($l:String!){user(login:$l){websiteUrl bio}}", {"l": login}
        )
        user = (data or {}).get("user") or {}
        urls: list[str] = []
        if user.get("websiteUrl"):
            urls.append(user["websiteUrl"])
        urls += extract_urls(user.get("bio"))
        urls += extract_urls(self._client.get_file(login, login, "README.md"))
        return urls

    # -- search & analytics -------------------------------------------------
    def search_file_mentions(self, text: str, filename: str) -> list[FileHit]:
        # Multi-word text (a full name) must be quoted; a bare handle isn't.
        term = f'"{text}"' if " " in text.strip() else text
        hits: list[FileHit] = []
        for item in self._client.search_code(f"{term} filename:{filename}"):
            nwo = (item.get("repository") or {}).get("full_name")
            if nwo:
                hits.append(FileHit(name_with_owner=nwo, path=item.get("path", "")))
        return hits

    # Note: GitHub rejects a login-qualifier-only commit search ("author:x")
    # with 422, so search_commits_by_author inherits the empty default. Commit
    # discovery goes through name search instead (issue #22).
    def search_commits_by_name(self, name: str) -> list[str]:
        # A quoted author-name is a valid (text-bearing) commit search, and it
        # finds commits authored under emails unlinked to the GitHub account —
        # which contributionsCollection/repositoriesContributedTo omit.
        repos: list[str] = []
        for item in self._client.search_commits(f'author-name:"{name}"'):
            nwo = (item.get("repository") or {}).get("full_name")
            if nwo:
                repos.append(nwo)
        return list(dict.fromkeys(repos))

    def merged_pr_count(self, owner: str, repo: str, login: str) -> int:
        return self._client.merged_pr_count(owner, repo, login)

    def path_commit_count(
        self, owner: str, repo: str, path: str, login: str, max_pages: int = 5
    ) -> int:
        return self._client.path_commit_count(owner, repo, path, login, max_pages)

    def repo_contributors(
        self, owner: str, repo: str, max_pages: int = 2
    ) -> list[ContributorCount] | None:
        raw = self._client.repo_contributors(owner, repo, max_pages=max_pages)
        if raw is None:
            return None
        return [
            ContributorCount(login=c["login"], contributions=c.get("contributions", 0))
            for c in raw if c.get("login")
        ]

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._client.get_url(url, accept=accept)

    def rate_summary(self) -> str:
        return self._client.rate_summary()

    def close(self) -> None:
        self._client.close()

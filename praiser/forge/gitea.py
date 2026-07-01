"""Gitea / Forgejo implementation of the Forge interface (Codeberg by default).

Codeberg runs Forgejo, a Gitea fork, so this speaks the Gitea REST API v1 — a
clean, GraphQL-free surface. It implements the portable core (files, repo
metadata, user/org repos) and leans on the interface's safe defaults for the
capabilities Gitea lacks a cheap endpoint for (code/commit search, full commit
history, aggregate contributor counts). That's the "graceful degradation" the
Forge interface is built around: on Codeberg, discovery leans on owned/org
repos + registry seeds + ``--add-repo``, and the file-based extractors
(ownership, manifests, codeowners, maintainers, authors, governance, proposals)
do the attribution.

All HTTP goes through a small cached transport (``_GiteaHttp``) so the whole
class is unit-testable offline by injecting a fake.
"""

import json
import urllib.parse
from typing import Any

from ..cache import Cache
from ._http import USER_AGENT, extract_urls, fetch_text, make_session
from .base import DirEntry, Forge, RepoMeta, UserRef

_REPO_PAGE_LIMIT = 50      # Gitea max page size for repo listings
_MAX_REPO_PAGES = 4        # cap pagination (≈200 repos) to bound cold runs


def _repo_meta(d: dict) -> RepoMeta | None:
    if not d or not d.get("full_name"):
        return None
    return RepoMeta(
        name_with_owner=d["full_name"],
        stars=d.get("stars_count", 0) or 0,
        forks=d.get("forks_count", 0) or 0,
        is_fork=bool(d.get("fork")),
        is_private=bool(d.get("private")),
        pushed_at=d.get("updated_at"),
    )


class _GiteaHttp:
    """Cached HTTP for a Gitea instance, over the shared REST helper."""

    def __init__(self, api_base: str, token: str | None, cache: Cache,
                 *, max_retries: int = 3) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.cache = cache
        self.max_retries = max_retries
        self._session = make_session()

    def _fetch_text(self, url: str, *, accept: str, auth: bool) -> str | None:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if auth and self.token:
            headers["Authorization"] = f"token {self.token}"
        return fetch_text(
            self._session, url, headers=headers, cache=self.cache,
            cache_key=Cache.key("gitea", url, accept, auth),
            max_retries=self.max_retries,
        )

    def get_json(self, path: str, params: dict | None = None) -> Any | None:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        text = self._fetch_text(url, accept="application/json", auth=True)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def get_raw(self, owner: str, repo: str, path: str, ref: str | None) -> str | None:
        url = f"{self.api_base}/repos/{owner}/{repo}/raw/{urllib.parse.quote(path)}"
        if ref:
            url += "?" + urllib.parse.urlencode({"ref": ref})
        return self._fetch_text(url, accept="*/*", auth=True)

    def get_external(self, url: str, accept: str) -> str | None:
        # No auth: external pages (team/governance sites, package registries).
        return self._fetch_text(url, accept=accept, auth=False)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class GiteaForge(Forge):
    name = "codeberg"

    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        base_url: str = "https://codeberg.org",
        name: str | None = None,
        verbose: bool = False,
    ) -> None:
        self._web = base_url.rstrip("/")
        self.web_base = self._web  # instance web host (for record URLs)
        if name is not None:
            self.name = name
        self._http = _GiteaHttp(f"{self._web}/api/v1", token, cache)

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self._web}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self._http.get_raw(owner, repo, path, ref)

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        data = self._http.get_json(f"repos/{owner}/{repo}/contents/{path}")
        if not isinstance(data, list):
            return []  # a file (dict) or missing -> not a directory
        return [
            DirEntry(name=e.get("name", ""), path=e.get("path", ""),
                     is_dir=e.get("type") == "dir")
            for e in data if e.get("name")
        ]

    # -- repository metadata ------------------------------------------------
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        data = self._http.get_json(f"repos/{owner}/{repo}")
        return _repo_meta(data) if isinstance(data, dict) else None

    # -- people & projects --------------------------------------------------
    def _paged_repos(self, path: str) -> list[RepoMeta]:
        out: list[RepoMeta] = []
        for page in range(1, _MAX_REPO_PAGES + 1):
            data = self._http.get_json(path, {"page": page, "limit": _REPO_PAGE_LIMIT})
            if not isinstance(data, list) or not data:
                break
            out.extend(m for d in data if (m := _repo_meta(d)) is not None)
            if len(data) < _REPO_PAGE_LIMIT:
                break
        return out

    def resolve_user(self, login: str) -> UserRef | None:
        data = self._http.get_json(f"users/{login}")
        if not isinstance(data, dict) or not data.get("login"):
            return None
        return UserRef(login=data["login"], name=data.get("full_name") or None)

    def user_repositories(self, login: str) -> list[RepoMeta]:
        return self._paged_repos(f"users/{login}/repos")

    def profile_links(self, login: str) -> list[str]:
        data = self._http.get_json(f"users/{login}")
        user = data if isinstance(data, dict) else {}
        urls: list[str] = []
        if user.get("website"):
            urls.append(user["website"])
        urls += extract_urls(user.get("description"))
        return urls

    def user_organizations(self, login: str) -> list[str]:
        data = self._http.get_json(f"users/{login}/orgs")
        if not isinstance(data, list):
            return []
        return [o["username"] for o in data if o.get("username")]

    def organization_repositories(self, org: str) -> list[RepoMeta]:
        return self._paged_repos(f"orgs/{org}/repos")

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._http.get_external(url, accept)

    def close(self) -> None:
        self._http.close()

"""Gitee implementation of the Forge interface (gitee.com).

Gitee is the largest code host in China; its REST API v5 closely mirrors
GitHub's shape (same field names: `stargazers_count`, `full_name`, …), so this
is the closest fit to `GitHubForge` of the non-GitHub backends — and it exposes
**stars**, so ranking works without the forks fallback.

HTTP goes through the shared cached helper; auth is Gitee's `access_token` query
parameter (public data works without one).
"""

import base64
import json
import urllib.parse
from typing import Any

from ..cache import Cache
from ._http import USER_AGENT, extract_urls, fetch_text, make_session
from .base import DirEntry, Forge, RepoMeta, UserRef

_REPO_PAGE_LIMIT = 100
_MAX_REPO_PAGES = 3


def _repo_meta(d: dict) -> RepoMeta | None:
    if not d or not d.get("full_name"):
        return None
    return RepoMeta(
        name_with_owner=d["full_name"],
        stars=d.get("stargazers_count", 0) or 0,
        forks=d.get("forks_count", 0) or 0,
        is_fork=bool(d.get("fork")),
        is_private=bool(d.get("private")),
        pushed_at=d.get("pushed_at"),
    )


class _GiteeHttp:
    """Cached HTTP for gitee.com; token (if any) rides the access_token param."""

    def __init__(self, api_base: str, token: str | None, cache: Cache,
                 *, max_retries: int = 3) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.cache = cache
        self.max_retries = max_retries
        self._session = make_session()

    def _fetch_text(self, url: str, *, accept: str, auth: bool) -> str | None:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        return fetch_text(
            self._session, url, headers=headers, cache=self.cache,
            cache_key=Cache.key("gitee", url, accept), max_retries=self.max_retries,
        )

    def get_json(self, path: str, params: dict | None = None) -> Any | None:
        params = dict(params or {})
        if self.token:
            params["access_token"] = self.token
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

    def get_external(self, url: str, accept: str) -> str | None:
        return self._fetch_text(url, accept=accept, auth=False)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class GiteeForge(Forge):
    name = "gitee"
    web_base = "https://gitee.com"

    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        verbose: bool = False,
    ) -> None:
        self._http = _GiteeHttp("https://gitee.com/api/v5", token, cache)

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self.web_base}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def _contents(self, owner: str, repo: str, path: str, ref: str | None) -> Any:
        params = {"ref": ref} if ref else None
        return self._http.get_json(
            f"repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", params
        )

    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        data = self._contents(owner, repo, path, ref)
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        data = self._contents(owner, repo, path, None)
        if not isinstance(data, list):
            return []
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
            data = self._http.get_json(path, {"page": page, "per_page": _REPO_PAGE_LIMIT})
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
        return UserRef(login=data["login"], name=data.get("name") or None)

    def user_repositories(self, login: str) -> list[RepoMeta]:
        return self._paged_repos(f"users/{login}/repos")

    def profile_links(self, login: str) -> list[str]:
        data = self._http.get_json(f"users/{login}")
        user = data if isinstance(data, dict) else {}
        urls: list[str] = []
        if user.get("blog"):
            urls.append(user["blog"])
        urls += extract_urls(user.get("bio"))
        return urls

    def user_organizations(self, login: str) -> list[str]:
        data = self._http.get_json(f"users/{login}/orgs")
        if not isinstance(data, list):
            return []
        return [o["login"] for o in data if o.get("login")]

    def organization_repositories(self, org: str, limit: int = 30) -> list[RepoMeta]:
        return self._paged_repos(f"orgs/{org}/repos")[:limit]

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._http.get_external(url, accept)

    def close(self) -> None:
        self._http.close()

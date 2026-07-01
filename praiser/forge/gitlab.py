"""GitLab implementation of the Forge interface (gitlab.com by default).

GitLab's REST API v4 differs from GitHub/Gitea in ways this class absorbs so the
rest of praiser doesn't have to:

* projects are addressed by a URL-encoded *path* (``group%2Fsubgroup%2Fproject``),
  not ``owner/repo`` — and paths can be nested (subgroups). ``Candidate.owner``
  keeps only the first segment while ``.repo`` keeps the rest, so joining them
  reconstructs the full path.
* auth is the ``PRIVATE-TOKEN`` header; fields are ``star_count`` /
  ``forks_count`` / ``visibility`` / ``forked_from_project`` / ``last_activity_at``.
* listing a user's projects needs a username→id lookup first.

Group memberships, merge-request counts, and code search are not exposed cheaply
for arbitrary users without elevated scopes, so those inherit the interface's
safe defaults (like the Gitea forge): discovery leans on the user's own projects
+ registry seeds + ``--add-repo``, and the file-based extractors attribute roles.
"""

import json
import urllib.parse
from typing import Any

from ..cache import Cache
from ._http import USER_AGENT, extract_urls, fetch_text, make_session
from .base import DirEntry, Forge, RepoMeta, UserRef

_PROJECT_PAGE_LIMIT = 100
_MAX_PROJECT_PAGES = 3


def _repo_meta(d: dict) -> RepoMeta | None:
    if not d or not d.get("path_with_namespace"):
        return None
    return RepoMeta(
        name_with_owner=d["path_with_namespace"],
        stars=d.get("star_count", 0) or 0,
        forks=d.get("forks_count", 0) or 0,
        is_fork=bool(d.get("forked_from_project")),
        is_private=d.get("visibility", "public") != "public",
        pushed_at=d.get("last_activity_at"),
    )


class _GitLabHttp:
    """Cached HTTP for a GitLab instance, over the shared REST helper."""

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
            headers["PRIVATE-TOKEN"] = self.token
        return fetch_text(
            self._session, url, headers=headers, cache=self.cache,
            cache_key=Cache.key("gitlab", url, accept, auth),
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

    def get_raw(self, project: str, path: str, ref: str | None) -> str | None:
        # /projects/:id/repository/files/:file_path/raw — both id and file_path
        # are URL-encoded (slashes -> %2F).
        pid = urllib.parse.quote(project, safe="")
        fpath = urllib.parse.quote(path, safe="")
        url = f"{self.api_base}/projects/{pid}/repository/files/{fpath}/raw"
        if ref:
            url += "?" + urllib.parse.urlencode({"ref": ref})
        return self._fetch_text(url, accept="*/*", auth=True)

    def get_external(self, url: str, accept: str) -> str | None:
        return self._fetch_text(url, accept=accept, auth=False)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class GitLabForge(Forge):
    name = "gitlab"

    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        base_url: str = "https://gitlab.com",
        name: str | None = None,
        verbose: bool = False,
    ) -> None:
        self._web = base_url.rstrip("/")
        self.web_base = self._web  # instance web host (for record URLs)
        if name is not None:
            self.name = name
        self._http = _GitLabHttp(f"{self._web}/api/v4", token, cache)
        self._user_ids: dict[str, int | None] = {}  # login -> id (memoised)

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self._web}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self._http.get_raw(f"{owner}/{repo}", path, ref)

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        pid = urllib.parse.quote(f"{owner}/{repo}", safe="")
        data = self._http.get_json(
            f"projects/{pid}/repository/tree",
            {"path": path, "per_page": 100},
        )
        if not isinstance(data, list):
            return []
        return [
            DirEntry(name=e.get("name", ""), path=e.get("path", ""),
                     is_dir=e.get("type") == "tree")
            for e in data if e.get("name")
        ]

    # -- repository metadata ------------------------------------------------
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        pid = urllib.parse.quote(f"{owner}/{repo}", safe="")
        data = self._http.get_json(f"projects/{pid}")
        return _repo_meta(data) if isinstance(data, dict) else None

    # -- people & projects --------------------------------------------------
    def _user_id(self, login: str) -> int | None:
        if login not in self._user_ids:
            data = self._http.get_json("users", {"username": login})
            uid = data[0].get("id") if isinstance(data, list) and data else None
            self._user_ids[login] = uid
        return self._user_ids[login]

    def resolve_user(self, login: str) -> UserRef | None:
        data = self._http.get_json("users", {"username": login})
        if not isinstance(data, list) or not data:
            return None
        user = data[0]
        self._user_ids[login] = user.get("id")  # reuse the lookup
        if not user.get("username"):
            return None
        return UserRef(login=user["username"], name=user.get("name") or None)

    def profile_links(self, login: str) -> list[str]:
        data = self._http.get_json("users", {"username": login})
        user = data[0] if isinstance(data, list) and data else {}
        urls: list[str] = []
        if user.get("website_url"):
            urls.append(user["website_url"])
        urls += extract_urls(user.get("bio"))
        return urls

    def user_repositories(self, login: str) -> list[RepoMeta]:
        uid = self._user_id(login)
        if uid is None:
            return []
        out: list[RepoMeta] = []
        for page in range(1, _MAX_PROJECT_PAGES + 1):
            data = self._http.get_json(
                f"users/{uid}/projects",
                {"page": page, "per_page": _PROJECT_PAGE_LIMIT,
                 "order_by": "star_count"},
            )
            if not isinstance(data, list) or not data:
                break
            out.extend(m for d in data if (m := _repo_meta(d)) is not None)
            if len(data) < _PROJECT_PAGE_LIMIT:
                break
        return out

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._http.get_external(url, accept)

    def close(self) -> None:
        self._http.close()

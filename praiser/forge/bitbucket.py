"""Bitbucket implementation of the Forge interface (bitbucket.org).

Bitbucket Cloud's REST API 2.0 is a clean fit — git-only, `workspace/repo`
maps straight to `owner/repo`. Two quirks this class absorbs:

* **No star metric.** `has_stars` is False; the closest popularity signal is the
  *watcher* count (`/watchers?size`), which we carry in `RepoMeta.forks` — the
  field #4 uses as the star-less popularity proxy.
* **`src` needs a ref.** File/tree access is `/src/{ref}/{path}`, so we resolve
  (and cache) each repo's default branch from `mainbranch.name`.

User lookup is by workspace slug (`/workspaces/{login}`); Bitbucket's account
API is UUID/privacy-restricted, so orgs/search aren't exposed and inherit the
interface defaults (discovery = the user's own workspace repos + --add-repo).
"""

import json
import urllib.parse
from typing import Any

from ..cache import Cache
from ._http import USER_AGENT, fetch_text, make_session
from .base import DirEntry, Forge, RepoMeta, UserRef

_PAGELEN = 100
_MAX_PAGES = 3


class _BitbucketHttp:
    def __init__(self, token: str | None, cache: Cache, *, max_retries: int = 3) -> None:
        self.api = "https://api.bitbucket.org/2.0"
        self.token = token
        self.cache = cache
        self.max_retries = max_retries
        self._session = make_session()

    def _text(self, url: str, *, accept: str, auth: bool) -> str | None:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return fetch_text(self._session, url, headers=headers, cache=self.cache,
                          cache_key=Cache.key("bitbucket", url, accept),
                          max_retries=self.max_retries)

    def get_json(self, path: str, params: dict | None = None) -> Any | None:
        url = f"{self.api}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        text = self._text(url, accept="application/json", auth=True)
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def get_src(self, w: str, r: str, ref: str, path: str) -> str | None:
        url = f"{self.api}/repositories/{w}/{r}/src/{ref}/{urllib.parse.quote(path)}"
        return self._text(url, accept="*/*", auth=True)

    def get_external(self, url: str, accept: str) -> str | None:
        return self._text(url, accept=accept, auth=False)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class BitbucketForge(Forge):
    name = "bitbucket"
    has_stars = False  # no stars; watcher count stands in as the popularity proxy
    web_base = "https://bitbucket.org"

    def __init__(self, token: str | None, cache: Cache, *, verbose: bool = False) -> None:
        self._http = _BitbucketHttp(token, cache)
        self._branch: dict[tuple[str, str], str] = {}  # (w,r) -> default branch

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self.web_base}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def _default_branch(self, owner: str, repo: str) -> str:
        key = (owner, repo)
        if key not in self._branch:
            obj = self._http.get_json(f"repositories/{owner}/{repo}")
            self._branch[key] = (
                ((obj or {}).get("mainbranch") or {}).get("name") or "master"
            )
        return self._branch[key]

    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        return self._http.get_src(owner, repo, ref or self._default_branch(owner, repo), path)

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        ref = self._default_branch(owner, repo)
        prefix = f"{path}/" if path and not path.endswith("/") else path
        data = self._http.get_json(
            f"repositories/{owner}/{repo}/src/{ref}/{prefix}", {"pagelen": _PAGELEN}
        )
        if not isinstance(data, dict):
            return []
        out: list[DirEntry] = []
        for v in data.get("values", []) or []:
            full = v.get("path")
            if not full:
                continue
            out.append(DirEntry(name=full.rsplit("/", 1)[-1], path=full,
                                is_dir=v.get("type") == "commit_directory"))
        return out

    # -- repository metadata ------------------------------------------------
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        obj = self._http.get_json(f"repositories/{owner}/{repo}")
        if not isinstance(obj, dict) or not obj.get("full_name"):
            return None
        self._branch[(owner, repo)] = (obj.get("mainbranch") or {}).get("name") or "master"
        watchers = self._http.get_json(
            f"repositories/{owner}/{repo}/watchers", {"pagelen": 0}
        )
        watcher_count = watchers.get("size", 0) if isinstance(watchers, dict) else 0
        return RepoMeta(
            name_with_owner=obj["full_name"],
            stars=0,
            forks=watcher_count or 0,  # watchers = star-less popularity proxy (#4)
            # ``parent`` is always present but null for non-forks, so test the
            # value, not the key.
            is_fork=obj.get("parent") is not None,
            is_private=bool(obj.get("is_private")),
            pushed_at=obj.get("updated_on"),
        )

    # -- people & projects --------------------------------------------------
    def resolve_user(self, login: str) -> UserRef | None:
        ws = self._http.get_json(f"workspaces/{login}")
        if isinstance(ws, dict) and ws.get("slug"):
            return UserRef(login=ws["slug"], name=ws.get("name") or None)
        return UserRef(login=login)  # workspace lookup restricted -> echo

    def user_repositories(self, login: str) -> list[RepoMeta]:
        out: list[RepoMeta] = []
        for page in range(1, _MAX_PAGES + 1):
            data = self._http.get_json(
                f"repositories/{login}", {"pagelen": _PAGELEN, "page": page}
            )
            values = data.get("values") if isinstance(data, dict) else None
            if not values:
                break
            for v in values:
                if v.get("full_name"):
                    # counts aren't in the list; enrich_stars fills watchers later
                    out.append(RepoMeta(
                        name_with_owner=v["full_name"], stars=0, forks=0,
                        is_fork=v.get("parent") is not None,
                        is_private=bool(v.get("is_private")),
                        pushed_at=v.get("updated_on"),
                    ))
            if not data.get("next"):
                break
        return out

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._http.get_external(url, accept)

    def close(self) -> None:
        self._http.close()

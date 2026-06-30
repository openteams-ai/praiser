"""GitHub access: GraphQL (discovery) + REST (file contents, search, teams).

Prefers ``httpx`` when installed; falls back to stdlib ``urllib`` so the core
runs with zero third-party dependencies. All GETs/queries go through the cache.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .cache import Cache

try:  # optional accelerator
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - exercised only without httpx
    httpx = None

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"
USER_AGENT = "gh-record/0.1 (+https://github.com)"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        max_retries: int = 3,
        verbose: bool = False,
    ) -> None:
        self.token = token
        self.cache = cache
        self.max_retries = max_retries
        self.verbose = verbose
        self._client = httpx.Client(timeout=30.0) if httpx is not None else None

    # -- low-level HTTP -----------------------------------------------------
    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Return (status, headers, raw body). Retries transient failures."""
        headers = self._headers(accept)
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if self._client is not None:
                    resp = self._client.request(
                        method, url, headers=headers, content=body
                    )
                    status, rheaders, data = (
                        resp.status_code,
                        dict(resp.headers),
                        resp.content,
                    )
                else:
                    req = urllib.request.Request(
                        url, data=body, headers=headers, method=method
                    )
                    try:
                        with urllib.request.urlopen(req) as r:
                            status, rheaders, data = (
                                r.status,
                                dict(r.headers),
                                r.read(),
                            )
                    except urllib.error.HTTPError as e:
                        status, rheaders, data = e.code, dict(e.headers), e.read()
            except Exception as exc:  # network blip
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            if self._should_retry(status, rheaders):
                self._sleep_for_ratelimit(rheaders, attempt)
                continue
            return status, rheaders, data

        raise GitHubError(f"request failed after retries: {url} ({last_exc})")

    @staticmethod
    def _should_retry(status: int, headers: dict[str, str]) -> bool:
        if status in (502, 503, 504):
            return True
        if status in (403, 429) and headers.get("X-RateLimit-Remaining") == "0":
            return True
        return False

    @staticmethod
    def _sleep_for_ratelimit(headers: dict[str, str], attempt: int) -> None:
        reset = headers.get("X-RateLimit-Reset")
        retry_after = headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            time.sleep(min(60, int(retry_after)))
        elif reset and reset.isdigit():
            wait = max(0, int(reset) - int(time.time())) + 1
            time.sleep(min(90, wait))
        else:
            time.sleep(2.0 * (attempt + 1))

    # -- GraphQL ------------------------------------------------------------
    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        ck = Cache.key("graphql", query, variables)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached
        payload = json.dumps({"query": query, "variables": variables}).encode()
        status, _, data = self._request(
            "POST", GRAPHQL_URL, accept="application/json", body=payload
        )
        if status != 200:
            raise GitHubError(f"GraphQL HTTP {status}: {data[:300]!r}")
        parsed = json.loads(data)
        if parsed.get("errors"):
            # Partial data is common (e.g. a missing user field); surface but
            # do not crash if there is usable data.
            if not parsed.get("data"):
                raise GitHubError(f"GraphQL errors: {parsed['errors']}")
        result = parsed.get("data") or {}
        self.cache.set(ck, result)
        return result

    # -- REST ---------------------------------------------------------------
    def rest_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{REST_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        ck = Cache.key("rest", url)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached
        status, _, data = self._request(
            "GET", url, accept="application/vnd.github+json"
        )
        if status == 404:
            self.cache.set(ck, {"__status__": 404})
            return None
        if status != 200:
            raise GitHubError(f"REST HTTP {status} for {path}: {data[:200]!r}")
        result = json.loads(data) if data else None
        self.cache.set(ck, result)
        return result

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        """Raw text of a file, or None if it does not exist."""
        api = f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
        if ref:
            api += "?" + urllib.parse.urlencode({"ref": ref})
        url = f"{REST_BASE}{api}"
        ck = Cache.key("rawfile", url)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return None if cached == "__404__" else cached
        status, _, data = self._request(
            "GET", url, accept="application/vnd.github.raw"
        )
        if status == 404:
            self.cache.set(ck, "__404__")
            return None
        if status != 200:
            raise GitHubError(f"raw file HTTP {status} for {owner}/{repo}/{path}")
        text = data.decode("utf-8", errors="replace")
        self.cache.set(ck, text)
        return text

    def list_dir(self, owner: str, repo: str, path: str) -> list[dict[str, Any]]:
        """Directory listing entries (name/type/path), or [] if missing."""
        result = self.rest_json(f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}")
        if isinstance(result, list):
            return result
        return []

    def team_members(self, org: str, team_slug: str) -> list[str]:
        """Logins of a team's members (requires read:org scope)."""
        members = self.rest_json(
            f"/orgs/{org}/teams/{team_slug}/members", params={"per_page": 100}
        )
        if not isinstance(members, list):
            return []
        return [m["login"] for m in members if "login" in m]

    def search_code(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        """Code search items (repository.full_name, path). Needs auth."""
        result = self.rest_json(
            "/search/code", params={"q": query, "per_page": per_page}
        )
        if isinstance(result, dict):
            return result.get("items", []) or []
        return []

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

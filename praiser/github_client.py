"""GitHub access: GraphQL (discovery) + REST (file contents, search, teams).

Prefers ``httpx`` when installed; falls back to stdlib ``urllib`` so the core
runs with zero third-party dependencies. All GETs/queries go through the cache.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .cache import Cache

# The pagination `Link` header's rel="last" page number. With per_page=1 that
# number equals the total item count — see `repo_contributor_count`.
_LINK_LAST_RE = re.compile(r'[?&]page=(\d+)>;\s*rel="last"')


def _link_last_page(link_header: str) -> int | None:
    m = _LINK_LAST_RE.search(link_header or "")
    return int(m.group(1)) if m else None

try:  # optional accelerator
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - exercised only without httpx
    httpx = None

GRAPHQL_URL = "https://api.github.com/graphql"
REST_BASE = "https://api.github.com"
USER_AGENT = "praiser/0.1 (+https://github.com)"


class GitHubError(RuntimeError):
    pass


class RateLimitError(GitHubError):
    """Raised when the GitHub rate limit is exhausted and won't reset soon."""

    def __init__(self, message: str, reset_in: int | None = None) -> None:
        super().__init__(message)
        self.reset_in = reset_in


# Secondary/abuse limits reset in seconds; primary limits can be ~an hour away.
# We wait out short windows but fail fast (so the caller can stop and report a
# partial result) on anything longer.
SHORT_RETRY_WINDOW = 30

# How many file blobs to request per GraphQL query. GraphQL is a separate rate
# bucket from REST and lets us fetch many files in one request.
GQL_FILE_BATCH = 50

_FILE_404 = "__404__"


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
        # Latest rate-limit state per bucket: resource -> (remaining, limit, reset).
        self.rate: dict[str, tuple[int, int, int]] = {}

    # -- low-level HTTP -----------------------------------------------------
    def _headers(self, accept: str, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        accept: str,
        body: bytes | None = None,
        auth: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        """Return (status, headers, raw body). Retries transient failures."""
        headers = self._headers(accept, auth=auth)
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                if self._client is not None:
                    resp = self._client.request(
                        method, url, headers=headers, content=body
                    )
                    status, raw_headers, data = (
                        resp.status_code,
                        resp.headers,
                        resp.content,
                    )
                else:
                    req = urllib.request.Request(
                        url, data=body, headers=headers, method=method
                    )
                    try:
                        with urllib.request.urlopen(req) as r:
                            status, raw_headers, data = r.status, r.headers, r.read()
                    except urllib.error.HTTPError as e:
                        status, raw_headers, data = e.code, e.headers, e.read()
            except Exception as exc:  # network blip
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue

            # Normalise header keys to lowercase: httpx and urllib disagree on
            # case, and GitHub's rate-limit headers must be read reliably.
            h = {k.lower(): v for k, v in dict(raw_headers).items()}
            self._track_rate(h)

            if status in (502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue

            reset_in = self._ratelimit_reset_in(status, h, data)
            if reset_in is not None:
                if reset_in <= SHORT_RETRY_WINDOW and attempt < self.max_retries - 1:
                    time.sleep(reset_in + 1)
                    continue
                raise RateLimitError(
                    f"GitHub API rate limit exhausted; resets in {reset_in}s. "
                    "Provide a token with --token or GITHUB_TOKEN, or wait.",
                    reset_in=reset_in,
                )
            return status, h, data

        raise GitHubError(f"request failed after retries: {url} ({last_exc})")

    def _track_rate(self, h: dict[str, str]) -> None:
        """Record the latest rate-limit state for whichever bucket replied."""
        resource = h.get("x-ratelimit-resource")
        remaining = h.get("x-ratelimit-remaining")
        if not resource or remaining is None or not remaining.isdigit():
            return
        limit = h.get("x-ratelimit-limit", "0")
        reset = h.get("x-ratelimit-reset", "0")
        self.rate[resource] = (
            int(remaining),
            int(limit) if limit.isdigit() else 0,
            int(reset) if reset.isdigit() else 0,
        )

    def rate_summary(self) -> str:
        """Short human string of remaining quota per bucket we've touched."""
        labels = [("core", "REST"), ("graphql", "GraphQL"), ("search", "search")]
        parts = [
            f"{label} {self.rate[res][0]}/{self.rate[res][1]}"
            for res, label in labels if res in self.rate
        ]
        return " · ".join(parts)

    @staticmethod
    def _ratelimit_reset_in(status: int, h: dict[str, str], data: bytes) -> int | None:
        """Seconds until reset if this response is a rate-limit refusal, else None."""
        if status not in (403, 429):
            return None
        body = (data or b"").lower()
        limited = (
            h.get("x-ratelimit-remaining") == "0"
            or b"rate limit exceeded" in body
            or b"secondary rate limit" in body
        )
        if not limited:
            return None  # a 403 for another reason (e.g. forbidden path)
        retry_after = h.get("retry-after")
        if retry_after and retry_after.isdigit():
            return int(retry_after)
        reset = h.get("x-ratelimit-reset")
        if reset and reset.isdigit():
            return max(0, int(reset) - int(time.time()))
        return 60

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
            # GraphQL signals a rate limit as HTTP 200 with a RATE_LIMITED error
            # in the body (not a 403/429), so _request doesn't catch it. Convert
            # it to RateLimitError with the reset time (already tracked from the
            # response headers) so callers show a friendly "try again in ~X" —
            # same handling as the REST path.
            if any((e or {}).get("type") == "RATE_LIMITED"
                   or "rate limit" in str((e or {}).get("message", "")).lower()
                   for e in parsed["errors"]):
                raise RateLimitError(
                    "GitHub GraphQL API rate limit exceeded.",
                    reset_in=self._bucket_reset_in("graphql"),
                )
            # Otherwise partial data is common (e.g. a missing user field);
            # surface the error only if there is no usable data.
            if not parsed.get("data"):
                raise GitHubError(f"GraphQL errors: {parsed['errors']}")
        result = parsed.get("data") or {}
        self.cache.set(ck, result)
        return result

    def _bucket_reset_in(self, resource: str) -> int | None:
        """Seconds until the given rate-limit bucket resets, from tracked headers."""
        entry = self.rate.get(resource)
        if not entry:
            return None
        return max(0, entry[2] - int(time.time()))

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

    # -- file contents ------------------------------------------------------
    @staticmethod
    def _file_key(owner: str, repo: str, path: str, ref: str | None) -> str:
        # Unified key so REST and GraphQL fetches share one cache entry.
        return Cache.key("file", owner, repo, path, ref or "HEAD")

    def _file_cache_get(self, key: str) -> tuple[bool, str | None]:
        """Return (hit, value). value is None for a cached 404."""
        val = self.cache.get(key, default=None)
        if val is None:
            return False, None
        return True, (None if val == _FILE_404 else val)

    def _file_cache_set(self, key: str, value: str | None) -> None:
        self.cache.set(key, _FILE_404 if value is None else value)

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        """Raw text of a file via REST, or None if it does not exist."""
        key = self._file_key(owner, repo, path, ref)
        hit, val = self._file_cache_get(key)
        if hit:
            return val
        api = f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
        if ref:
            api += "?" + urllib.parse.urlencode({"ref": ref})
        status, _, data = self._request(
            "GET", f"{REST_BASE}{api}", accept="application/vnd.github.raw"
        )
        if status == 404:
            self._file_cache_set(key, None)
            return None
        if status != 200:
            raise GitHubError(f"raw file HTTP {status} for {owner}/{repo}/{path}")
        text = data.decode("utf-8", errors="replace")
        self._file_cache_set(key, text)
        return text

    def get_files(
        self, owner: str, repo: str, paths: list[str], ref: str | None = None
    ) -> dict[str, str | None]:
        """Batch-fetch many files, mapping path -> text (None if missing).

        Uses GraphQL (a separate rate bucket from REST, and one request for up
        to ``GQL_FILE_BATCH`` files) when authenticated, falling back to REST
        per file when there is no token or GraphQL is unavailable.
        """
        out: dict[str, str | None] = {}
        todo: list[str] = []
        for p in paths:
            hit, val = self._file_cache_get(self._file_key(owner, repo, p, ref))
            if hit:
                out[p] = val
            else:
                todo.append(p)
        if not todo:
            return out

        if not self.token:  # GraphQL requires authentication
            for p in todo:
                out[p] = self.get_file(owner, repo, p, ref)
            return out

        for i in range(0, len(todo), GQL_FILE_BATCH):
            chunk = todo[i : i + GQL_FILE_BATCH]
            try:
                texts, truncated = self._graphql_blobs(owner, repo, chunk, ref)
            except RateLimitError:
                raise
            except Exception:  # GraphQL hiccup: fall back to REST for this chunk
                for p in chunk:
                    out[p] = self.get_file(owner, repo, p, ref)
                continue
            for p in chunk:
                if p in truncated:  # too large for GraphQL text; use raw REST
                    out[p] = self.get_file(owner, repo, p, ref)
                    continue
                val = texts.get(p)
                self._file_cache_set(self._file_key(owner, repo, p, ref), val)
                out[p] = val
        return out

    def _graphql_blobs(
        self, owner: str, repo: str, paths: list[str], ref: str | None
    ) -> tuple[dict[str, str], set[str]]:
        """Return (path->text for resolved blobs, set of truncated paths)."""
        rev = ref or "HEAD"
        aliases: dict[str, str] = {}
        parts: list[str] = []
        for i, p in enumerate(paths):
            alias = f"f{i}"
            aliases[alias] = p
            expr = json.dumps(f"{rev}:{p}")
            parts.append(
                f"{alias}: object(expression:{expr}) "
                "{ ... on Blob { text isTruncated } }"
            )
        query = (
            "query($o:String!,$r:String!){ repository(owner:$o,name:$r){ "
            + " ".join(parts)
            + " } }"
        )
        data = self.graphql(query, {"o": owner, "r": repo})
        repo_obj = (data or {}).get("repository") or {}
        texts: dict[str, str] = {}
        truncated: set[str] = set()
        for alias, p in aliases.items():
            blob = repo_obj.get(alias)
            if not blob:
                continue  # missing path (or not a blob) -> treated as None
            if blob.get("isTruncated"):
                truncated.add(p)
            elif blob.get("text") is not None:
                texts[p] = blob["text"]
        return texts, truncated

    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        """Fetch an arbitrary (non-GitHub-API) URL as text, cached.

        Sends NO Authorization header — these are external pages (project team /
        governance sites), and the GitHub token must never leak to them. Pass
        ``accept`` for JSON APIs (e.g. package registries); npm's CDN returns
        406 to the default HTML Accept. Cache is keyed on (url, accept) so the
        same URL fetched as HTML vs JSON doesn't collide.
        """
        ck = Cache.key("url", url, accept)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return None if cached == "__404__" else cached
        try:
            status, _, data = self._request("GET", url, accept=accept, auth=False)
        except GitHubError:
            return None
        if status >= 400:
            self.cache.set(ck, "__404__")
            return None
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

    def search_commits(self, query: str, per_page: int = 100) -> list[dict[str, Any]]:
        """Commit search items (each has a .repository). Needs auth."""
        result = self.rest_json(
            "/search/commits",
            params={"q": query, "per_page": per_page,
                    "sort": "author-date", "order": "desc"},
        )
        if isinstance(result, dict):
            return result.get("items", []) or []
        return []

    def merged_pr_count(self, owner: str, repo: str, login: str) -> int:
        """Number of MERGED pull requests authored by the user in a repo.

        A workflow-agnostic size metric: squash- and ghstack-based repos land one
        commit per PR (so commit counts understate), and PR authorship is tied to
        the GitHub account even when a commit's email isn't linked.
        """
        q = f"repo:{owner}/{repo} author:{login} is:pr is:merged"
        result = self.rest_json("/search/issues", params={"q": q, "per_page": 1})
        if isinstance(result, dict):
            return int(result.get("total_count", 0) or 0)
        return 0

    def path_commit_count(
        self, owner: str, repo: str, path: str, login: str, max_pages: int = 5
    ) -> int:
        """How many commits by ``login`` touch ``path`` (a subdirectory/file).

        Used for subcomponent-level role detection — credit for owning/leading a
        part of a monorepo (e.g. f2py in NumPy). Capped at ``max_pages``*100.
        """
        total = 0
        for page in range(1, max_pages + 1):
            result = self.rest_json(
                f"/repos/{owner}/{repo}/commits",
                params={"path": path, "author": login, "per_page": 100, "page": page},
            )
            if not isinstance(result, list) or not result:
                break
            total += len(result)
            if len(result) < 100:
                break
        return total

    def repo_contributors(
        self, owner: str, repo: str, max_pages: int = 2
    ) -> list[dict[str, Any]] | None:
        """Top contributors [{login, contributions}], sorted desc.

        Paginates up to GitHub's ~500-contributor cap. Returns None if the data
        could not be fetched (so callers can stay lenient rather than assume the
        user is uninvolved).
        """
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            try:
                chunk = self.rest_json(
                    f"/repos/{owner}/{repo}/contributors",
                    params={"per_page": 100, "page": page, "anon": "false"},
                )
            except GitHubError:
                return out or None
            if not isinstance(chunk, list) or not chunk:
                break
            out.extend(chunk)
            if len(chunk) < 100:
                break
        return out

    def repo_release_authors(
        self, owner: str, repo: str, max_releases: int = 100
    ) -> list[str] | None:
        """Author logins of the most recent releases (who published each release),
        most-recent-first. Bot logins (``…[bot]``) are included — the caller
        decides how to treat automation. None if it couldn't be fetched."""
        try:
            data = self.rest_json(
                f"/repos/{owner}/{repo}/releases",
                params={"per_page": min(100, max_releases)},
            )
        except GitHubError:
            return None
        if not isinstance(data, list):
            return None
        return [(r.get("author") or {}).get("login") or "" for r in data]

    def repo_contributor_count(
        self, owner: str, repo: str, anon: bool = True
    ) -> int | None:
        """Total number of contributors, in ONE request, via the `Link` header.

        ``per_page=1`` makes the ``rel="last"`` page number equal the total
        contributor count. ``anon=True`` counts distinct commit-author
        *identities* — the real total, and **uncapped** (a huge repo returns its
        true tens of thousands). ``anon=False`` counts GitHub *accounts*, which
        GitHub caps near ~500 (only the first ~500 author emails resolve to
        accounts), so it badly undercounts large repos. Returns None when it
        can't be determined; propagates RateLimitError."""
        ck = Cache.key("contrib-count", owner, repo, anon)
        cached = self.cache.get(ck, default=None)
        if cached is not None:
            return cached
        url = (f"{REST_BASE}/repos/{owner}/{repo}/contributors?"
               + urllib.parse.urlencode(
                   {"per_page": 1, "anon": "true" if anon else "false"}))
        try:
            status, h, data = self._request(
                "GET", url, accept="application/vnd.github+json")
        except RateLimitError:
            raise
        except GitHubError:
            return None
        if status != 200:
            return None
        count = _link_last_page(h.get("link", ""))
        if count is None:            # no Link header -> single page: count items
            try:
                body = json.loads(data) if data else []
            except ValueError:
                body = []
            count = len(body) if isinstance(body, list) else 0
        self.cache.set(ck, count)
        return count

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

"""Generic cgit backend for API-less git hosts served by a cgit/gitweb frontend.

One parameterised backend covers several important hosts that expose no REST
API — you point it at an instance with ``--forge-url`` and name repos with
``--add-repo``:

* **kernel.org** (`https://git.kernel.org`) — the Linux kernel + subsystem trees.
* **Savannah** (`https://git.savannah.gnu.org`) — GNU / nongnu projects.
* **Launchpad git** (`https://git.launchpad.net`).

There is no user API, no metadata, and no star metric, so this sits at the
minimal tier of the Forge contract: file fetch via cgit ``plain/`` + the
file-based extractors (ownership/manifests/authors/maintainers/governance/
codeowners). Discovery is ``--add-repo``-driven (``resolve_user`` /
``user_repositories`` inherit the safe defaults), and ``has_stars`` is False so
ranking falls back to forks (0 here — records survive because ``--add-repo``
force-includes them).

The repo identifier is the **cgit-relative path** to the repo, e.g.
``cgit/gnulib.git`` on Savannah or ``pub/scm/git/git.git`` on kernel.org — so
``owner``/``repo`` rejoin into the exact path cgit expects, and evidence links
point at the cgit repo page. ``list_dir`` is deferred (cgit's tree view is HTML,
not JSON), so directory-scanning extractors (enhancement-proposals) are skipped;
fixed-path extractors work.
"""

import urllib.parse

from ..cache import Cache
from ._http import USER_AGENT, fetch_text, make_session
from .base import DirEntry, Forge, RepoMeta


class CgitForge(Forge):
    name = "cgit"
    has_stars = False  # no star metric; ranking falls back to forks

    def __init__(
        self,
        token: str | None,
        cache: Cache,
        *,
        base_url: str = "https://git.kernel.org",
        name: str | None = None,
        verbose: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self.web_base = self._base
        if name is not None:
            self.name = name
        self._cache = cache
        self._session = make_session()

    def _get(self, url: str, accept: str = "*/*") -> str | None:
        return fetch_text(
            self._session, url,
            headers={"Accept": accept, "User-Agent": USER_AGENT},
            cache=self._cache, cache_key=Cache.key("cgit", url, accept),
        )

    # -- web identity -------------------------------------------------------
    def web_url(self, name_with_owner: str) -> str:
        return f"{self._base}/{name_with_owner}"

    # -- files --------------------------------------------------------------
    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        url = f"{self._base}/{owner}/{repo}/plain/{urllib.parse.quote(path)}"
        if ref:
            url += "?" + urllib.parse.urlencode({"h": ref})
        return self._get(url)

    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        # cgit's tree view is HTML, not a machine format — deferred. Directory
        # scanning extractors are skipped on cgit; fixed-path ones still work.
        return []

    # -- repository metadata ------------------------------------------------
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        # No metadata API; confirm the repo exists via its cgit summary page so
        # a mistyped --add-repo is dropped rather than silently kept.
        if self._get(f"{self._base}/{owner}/{repo}/") is None:
            return None
        return RepoMeta(name_with_owner=f"{owner}/{repo}")

    # -- generic HTTP + housekeeping ----------------------------------------
    def get_url(
        self, url: str, accept: str = "text/html,application/xhtml+xml"
    ) -> str | None:
        return self._get(url, accept)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()

"""Package-registry discovery (PyPI / npm / crates.io).

A maintainer/owner of a published package holds a real elevated role regardless
of where the code is hosted, and registry metadata names the package's source
repository — so this module turns "what does this user publish?" into both
extra candidate repos and a per-repo maintainer signal.

npm and crates.io support a *forward* lookup keyed on the user's login (crates.io
logins *are* GitHub logins; npm usernames usually match), which both establishes
maintainership and yields the source repo. A handle collision can't produce a
false credit: a package only attaches to a candidate when the package's own
source URL points at that repo (see ``PackageRef``), so an unrelated namesake's
packages — pointing elsewhere — never match.

PyPI is different: it offers no unauthenticated user→packages enumeration (the
profile page is bot-walled) and its JSON API never lists a project's PyPI
account maintainers — only ``author``/``maintainer`` *name* strings. So PyPI is
done as a *reverse* probe (guess the package from the repo name) and is credited
to the user only when that author/maintainer name matches the identity — i.e. a
package the user authored — recorded as AUTHOR. This refuses to mis-credit a
popular package (e.g. numpy, author "Travis Oliphant et al.") to a contributor.

All network access goes through an injected ``fetch(url) -> str | None`` (the
production caller passes ``Forge.get_url``, which caches and sends a
User-Agent — crates.io rejects requests without one). Parsing is split into
pure helpers so the whole module is unit-testable offline.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from .models import Identity, PackageRef

Fetch = Callable[[str], "str | None"]

# Accept header for registry JSON APIs. npm's CDN returns 406 to an HTML-only
# Accept, so callers must wrap ``Forge.get_url`` with this.
JSON_ACCEPT = "application/json, */*;q=0.8"

# Paths under github.com that are not repositories.
_NON_REPO_OWNERS = {"sponsors", "orgs", "users", "topics", "features", "about",
                    "settings", "marketplace", "apps"}
_GITHUB_REPO_RE = re.compile(
    r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.IGNORECASE
)
# A prolific account can publish a lot; bound per-registry work (cached anyway).
_MAX_PACKAGES = 100


def github_nwo(url: str | None) -> str | None:
    """Extract a canonical ``owner/repo`` from a source URL, or None.

    Handles ``https://github.com/o/r``, ``...r.git``, ``git+https://``,
    ``git@github.com:o/r`` and trailing paths/slashes; rejects non-repo paths
    like ``github.com/sponsors/x``.
    """
    if not url:
        return None
    m = _GITHUB_REPO_RE.search(url)
    if not m:
        return None
    nwo = m.group(1)
    if nwo.lower().endswith(".git"):
        nwo = nwo[:-4]
    nwo = nwo.strip("/")
    parts = nwo.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if owner.lower() in _NON_REPO_OWNERS:
        return None
    return f"{owner}/{repo}"


def _fetch_json(fetch: Fetch, url: str) -> Any | None:
    """Fetch and parse a JSON document, or None on any failure."""
    text = fetch(url)
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def index_by_repo(refs: list[PackageRef]) -> dict[str, list[PackageRef]]:
    """Group package refs by their (lowercased) GitHub ``owner/repo`` key."""
    index: dict[str, list[PackageRef]] = {}
    for ref in refs:
        if ref.repo:
            index.setdefault(ref.repo.lower(), []).append(ref)
    return index


# --- PyPI (reverse probe) -------------------------------------------------
def pypi_ref(info: dict, identity: Identity) -> PackageRef | None:
    """Build a PackageRef from a PyPI project's ``info`` block.

    ``author_match`` is true when the identity is named in any of the author /
    maintainer fields — the only user-linking signal the JSON API exposes.
    """
    name = info.get("name")
    if not name:
        return None
    urls = info.get("project_urls") or {}
    sources = list(urls.values()) + [info.get("home_page")]
    repo = next((nwo for u in sources if (nwo := github_nwo(u))), None)
    repo_url = next((u for u in sources if u and github_nwo(u)), None)
    author_match = (
        identity.matches_name(info.get("author"))
        or identity.matches_name(info.get("maintainer"))
        or identity.matches_email(info.get("author_email"))
        or identity.matches_email(info.get("maintainer_email"))
    )
    return PackageRef(
        registry="pypi",
        name=name,
        url=f"https://pypi.org/project/{name}/",
        repo=repo,
        repo_url=repo_url,
        author_match=author_match,
    )


def pypi_name_guesses(repo_name: str) -> list[str]:
    """Plausible PyPI distribution names for a repo (order = probe order)."""
    lower = repo_name.lower()
    seen: dict[str, None] = {}
    for g in (repo_name, lower, lower.replace("_", "-"), lower.replace("-", "_")):
        if g:
            seen.setdefault(g, None)
    return list(seen)


def pypi_ref_for_repo(
    fetch: Fetch, name_with_owner: str, identity: Identity
) -> PackageRef | None:
    """Reverse PyPI lookup for a candidate repo.

    Probes the repo name as a distribution and returns a ref only when the
    identity is the named author/maintainer and the package doesn't disclaim
    this repo (its source URL is absent or points back here). Returns None when
    the package is someone else's, or names a different source repo.
    """
    _, _, repo_name = name_with_owner.partition("/")
    if not repo_name:
        return None
    for guess in pypi_name_guesses(repo_name):
        data = _fetch_json(fetch, f"https://pypi.org/pypi/{guess}/json")
        info = (data or {}).get("info") if isinstance(data, dict) else None
        if not info:
            continue
        ref = pypi_ref(info, identity)
        if not ref or not ref.author_match:
            continue
        if ref.repo and ref.repo.lower() != name_with_owner.lower():
            continue  # the package names a *different* source repo — not this one
        ref.repo = name_with_owner  # anchor evidence to the candidate
        return ref
    return None


# --- npm -------------------------------------------------------------------
def npm_refs(search_json: dict | None, identity: Identity) -> list[PackageRef]:
    """PackageRefs from an npm registry search (``text=maintainer:<login>``)."""
    refs: list[PackageRef] = []
    for obj in (search_json or {}).get("objects", []) or []:
        pkg = obj.get("package") or {}
        name = pkg.get("name")
        if not name:
            continue
        links = pkg.get("links") or {}
        repo_url = links.get("repository") or _npm_repo_url(pkg.get("repository"))
        author = pkg.get("author") or {}
        author_match = (
            identity.matches_name(author.get("name"))
            or identity.matches_email(author.get("email"))
        )
        refs.append(PackageRef(
            registry="npm",
            name=name,
            url=f"https://www.npmjs.com/package/{name}",
            repo=github_nwo(repo_url),
            repo_url=repo_url,
            author_match=author_match,
        ))
    return refs


def _npm_repo_url(repository: Any) -> str | None:
    """npm ``repository`` is sometimes a string, sometimes ``{type,url}``."""
    if isinstance(repository, str):
        return repository
    if isinstance(repository, dict):
        return repository.get("url")
    return None


def npm_packages(fetch: Fetch, identity: Identity) -> list[PackageRef]:
    login = identity.primary_login
    data = _fetch_json(
        fetch,
        "https://registry.npmjs.org/-/v1/search"
        f"?text=maintainer:{login}&size={_MAX_PACKAGES}",
    )
    return npm_refs(data if isinstance(data, dict) else None, identity)


# --- crates.io -------------------------------------------------------------
def crates_refs(crates_json: dict | None) -> list[PackageRef]:
    """PackageRefs from a crates.io ``/crates?user_id=`` listing."""
    refs: list[PackageRef] = []
    for crate in (crates_json or {}).get("crates", []) or []:
        name = crate.get("name")
        if not name:
            continue
        repo_url = crate.get("repository")
        refs.append(PackageRef(
            registry="crates",
            name=name,
            url=f"https://crates.io/crates/{name}",
            repo=github_nwo(repo_url),
            repo_url=repo_url,
        ))
    return refs


def crates_packages(fetch: Fetch, identity: Identity) -> list[PackageRef]:
    login = identity.primary_login
    user = _fetch_json(fetch, f"https://crates.io/api/v1/users/{login}")
    uid = ((user or {}).get("user") or {}).get("id") if isinstance(user, dict) else None
    if uid is None:
        return []
    data = _fetch_json(
        fetch,
        f"https://crates.io/api/v1/crates?user_id={uid}&per_page={_MAX_PACKAGES}",
    )
    return crates_refs(data if isinstance(data, dict) else None)


# --- orchestration ---------------------------------------------------------
# Forward, login-keyed registries (PyPI has no such endpoint — it is probed in
# reverse, per candidate, by the packages extractor via ``pypi_ref_for_repo``).
_COLLECTORS = (npm_packages, crates_packages)


def discover_packages(fetch: Fetch, identity: Identity) -> list[PackageRef]:
    """Forward-discovered package roles (npm + crates.io) for ``identity``.

    One registry being unreachable or malformed never aborts the others.
    """
    refs: list[PackageRef] = []
    for collect in _COLLECTORS:
        try:
            refs.extend(collect(fetch, identity))
        except Exception:
            continue  # a flaky registry must not sink the run
    return refs

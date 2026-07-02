"""Framework-agnostic praiser service — the seam every frontend calls.

No UI framework here: a frontend collects options and populates the token env
vars (from its own secret store), then calls :func:`praise`. Swapping Streamlit
for FastAPI/Gradio reuses this unchanged.
"""

import base64
import functools
import os
import pickle

from praiser.cache import Cache
from praiser.config import Config
from praiser.pipeline import run
from praiser.popularity import filter_records
from praiser.registry import KnownProjects
from praiser.render import render, render_highlights

from .cache import local_cache, make_result_cache


def _dumps(result) -> str:
    """Serialize a RunResult to a cache-safe string (base64 of pickle)."""
    return base64.b64encode(pickle.dumps(result)).decode("ascii")


def _loads(blob: str):
    # Trusted: the result cache is written only by this service.
    return pickle.loads(base64.b64decode(blob))


@functools.lru_cache(maxsize=1)
def _registry():
    """The known-projects registry, loaded once (cheap; used for render-time
    min-stars filtering — importance labels + min_stars overrides)."""
    return KnownProjects.load()

FORGES = ["github", "gitlab", "codeberg", "gitee", "bitbucket", "cgit"]
VIEWS = ["highlights", "markdown", "json"]

# Per-forge token env vars (the anchor forge's token; praiser reads the rest of
# discovery unauthenticated). Frontends set these from their secret store.
_TOKEN_ENV = {
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gitlab": ("GITLAB_TOKEN",),
    "codeberg": ("CODEBERG_TOKEN", "FORGEJO_TOKEN"),
    "gitee": ("GITEE_TOKEN",),
    "bitbucket": ("BITBUCKET_TOKEN",),
    "cgit": (),
}


def _token_for(forge: str) -> str | None:
    for var in _TOKEN_ENV.get(forge, ()):
        if os.environ.get(var):
            return os.environ[var]
    return None


# Bump whenever praiser's *extraction logic* changes (extractors, role rules,
# ranking, discovery) so previously-cached results — computed by the old logic —
# are abandoned and recomputed. Folded into the result-cache key: one bump
# refreshes every user (old entries just TTL out). e.g. bumped for the
# subcomponents-are-contribution fix (#47) + credit-based authorship (#48);
# bumped to 3 for the discovery/attribution false-negative fixes (#57 #58 #59
# #62 #63) so the web app recomputes with the improved recall.
CACHE_VERSION = 4

# A small index of recently-scanned (forge, login) pairs — the cache keys are
# hashed and can't be enumerated, so we track names separately for a UI picker.
_RECENT_KEY = Cache.key("recent-scans-index")
_RECENT_CAP = 50


def _record_recent(rcache, forge: str, username: str) -> None:
    """Prepend (forge, username) to the recent-scans index (best-effort)."""
    if rcache is None:
        return
    try:
        idx = rcache.get(_RECENT_KEY) or []
        entry = [forge, username]
        idx = [entry] + [e for e in idx if e != entry]
        rcache.set(_RECENT_KEY, idx[:_RECENT_CAP])
    except Exception:
        pass


def recent_scans(result_cache=None) -> list[dict]:
    """Recently-scanned ``[{"forge", "username"}]``, most-recent-first (for a UI
    picker). Reads the shared index once; degrades to [] on any error."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return []
    try:
        idx = rcache.get(_RECENT_KEY) or []
    except Exception:
        return []
    return [{"forge": e[0], "username": e[1]} for e in idx
            if isinstance(e, list) and len(e) == 2]


# The options that affect DATA COLLECTION (a change here needs a re-scan). The
# display options — ``view``, ``highlights`` N, and ``min_stars`` — are excluded:
# a frontend re-renders them from a cached result without re-scanning. (min_stars
# is a display filter here: we collect the full superset at floor 0 and apply the
# threshold at render time — see ``collect``/``render_result``.)
DATA_OPTIONS = (
    "forge", "forge_url", "discover_roles", "wikidata",
    "package_registries", "cross_forge",
)


def collect(
    username: str,
    *,
    forge: str = "github",
    forge_url: str | None = None,
    discover_roles: bool = False,   # LLM + web search (cost/quota) — opt-in
    wikidata: bool = True,
    package_registries: bool = True,
    cross_forge: bool = False,
    http_cache=None,
    result_cache=None,
    progress=None,
):
    """Run praiser's data collection and return the ``RunResult`` (records etc.).

    Two-layer caching (see ``web.core.cache``): the shared **result cache** is
    checked first — a warm user returns with ONE cache read and **zero** praiser
    HTTP calls; on a miss the scan runs against a **local** HTTP cache and the
    result is stored (one write). This keeps shared-cache traffic to ~1–2 ops per
    scan instead of hundreds.

    Collects the full role-bearing superset (``min_stars=0``) so the popularity
    threshold is applied at *render* time (min-stars slider without re-scanning).
    LLM/Wikidata cost is unaffected (gated on ``role_discovery_floor``).
    ``progress(msg)`` gets live status lines (only on an actual scan).
    """
    rcache = result_cache if result_cache is not None else make_result_cache()
    rkey = Cache.key(
        "result", CACHE_VERSION, username, forge, forge_url or None,
        discover_roles, wikidata, package_registries, cross_forge,
    )
    if rcache is not None:
        blob = rcache.get(rkey)
        if blob is not None:
            try:
                return _loads(blob)     # warm: no scan, no praiser HTTP calls
            except Exception:
                pass                    # corrupt/incompatible entry -> re-scan

    config = Config(
        username=username,
        forge=forge,
        forge_url=forge_url or None,
        token=_token_for(forge),
        min_stars=0,                     # collect everything; filter at render time
        use_llm=discover_roles,          # only load the LLM when it's wanted
        discover_roles=discover_roles,
        use_wikidata=wikidata,
        use_package_registries=package_registries,
        cross_forge=cross_forge,
        quiet=True,
        save_registry=False,             # a shared service shouldn't mutate the registry
    )
    result = run(
        config,
        cache=http_cache if http_cache is not None else local_cache(),
        progress_cb=progress,
        # The reverse-index (#59) rides the SHARED cache (rcache = Redis), so the
        # app READS what the org seeder (#65) populated. But we do NOT write it
        # per interactive scan: that's a per-login write storm on Redis — slow
        # (the "quiet hang" after scanning) and command-expensive. Seeding is the
        # controlled population path.
        index_cache=rcache,
        populate_index=False,
    )
    # Only cache COMPLETE scans — a partial result (rate limit hit mid-scan)
    # must not be frozen for the cache TTL; let a later retry get the full data.
    if rcache is not None and result.partial_reset_in is None:
        rcache.set(rkey, _dumps(result))
        _record_recent(rcache, forge, username)
    return result


def render_result(result, username: str, *, view: str = "highlights",
                  highlights: int = 8, min_stars: int = 50) -> str:
    """Render an already-collected ``RunResult`` for ``view`` (cheap, no network).

    Applies the ``min_stars`` popularity split here (the result was collected at
    floor 0), so changing it re-renders instantly without re-scanning.
    """
    # The collected superset = records + secondary (collected at floor 0).
    allrecs = [*result.records, *result.secondary]
    primary, secondary = filter_records(
        allrecs, min_stars=min_stars, registry=_registry()
    )
    primary.sort(key=lambda r: r.score, reverse=True)
    secondary.sort(key=lambda r: r.score, reverse=True)
    if view == "highlights":
        return render_highlights(username, primary, highlights, secondary)
    fmt = "json" if view == "json" else "md"
    return render(username, primary, fmt, secondary)


def praise(username: str, *, view: str = "highlights", highlights: int = 8,
           min_stars: int = 50, progress=None, **data_options) -> str:
    """Convenience: collect then render in one call (CLI-like callers)."""
    result = collect(username, progress=progress, **data_options)
    return render_result(result, username, view=view, highlights=highlights,
                         min_stars=min_stars)

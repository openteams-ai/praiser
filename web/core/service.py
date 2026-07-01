"""Framework-agnostic praiser service — the seam every frontend calls.

No UI framework here: a frontend collects options and populates the token env
vars (from its own secret store), then calls :func:`praise`. Swapping Streamlit
for FastAPI/Gradio reuses this unchanged.
"""

import functools
import os

from praiser.config import Config
from praiser.pipeline import run
from praiser.popularity import filter_records
from praiser.registry import KnownProjects
from praiser.render import render, render_highlights

from .cache import make_cache


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
    cache=None,
    progress=None,
):
    """Run praiser's data collection and return the ``RunResult`` (records etc.).

    Collects the full role-bearing superset (``min_stars=0``) so the popularity
    threshold can be applied at *render* time — letting a frontend move the
    min-stars slider without re-scanning. LLM/Wikidata cost is unaffected (those
    are gated on ``role_discovery_floor``, not min_stars). ``cache`` defaults to
    the backend from :func:`make_cache`; ``progress(msg)`` gets live status lines.
    """
    cache = cache if cache is not None else make_cache()
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
    return run(config, cache=cache, progress_cb=progress)


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
           min_stars: int = 50, cache=None, progress=None, **data_options) -> str:
    """Convenience: collect then render in one call (CLI-like callers)."""
    result = collect(username, cache=cache, progress=progress, **data_options)
    return render_result(result, username, view=view, highlights=highlights,
                         min_stars=min_stars)

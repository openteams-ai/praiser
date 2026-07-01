"""Framework-agnostic praiser service — the seam every frontend calls.

No UI framework here: a frontend collects options and populates the token env
vars (from its own secret store), then calls :func:`praise`. Swapping Streamlit
for FastAPI/Gradio reuses this unchanged.
"""

import os

from praiser.config import Config
from praiser.pipeline import run
from praiser.render import render, render_highlights

from .cache import make_cache

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


def praise(
    username: str,
    *,
    forge: str = "github",
    forge_url: str | None = None,
    min_stars: int = 50,
    discover_roles: bool = False,   # LLM + web search (cost/quota) — opt-in
    wikidata: bool = True,
    package_registries: bool = True,
    cross_forge: bool = False,
    view: str = "highlights",
    highlights: int = 8,
    cache=None,
    progress=None,
) -> str:
    """Run praiser for ``username`` and return the rendered output for ``view``.

    ``cache`` defaults to the shared/local backend from :func:`make_cache` — so
    the expensive, option-independent data collection is reused across requests
    and hosts. ``progress(msg)`` receives live phase/status lines (for a UI).
    """
    cache = cache if cache is not None else make_cache()
    config = Config(
        username=username,
        forge=forge,
        forge_url=forge_url or None,
        token=_token_for(forge),
        min_stars=min_stars,
        use_llm=discover_roles,          # only load the LLM when it's wanted
        discover_roles=discover_roles,
        use_wikidata=wikidata,
        use_package_registries=package_registries,
        cross_forge=cross_forge,
        quiet=True,
        save_registry=False,             # a shared service shouldn't mutate the registry
        highlights=highlights if view == "highlights" else None,
        fmt="json" if view == "json" else "md",
    )
    result = run(config, cache=cache, progress_cb=progress)
    if view == "highlights":
        return render_highlights(
            username, result.records, highlights, result.secondary
        )
    return render(username, result.records, config.fmt, result.secondary)

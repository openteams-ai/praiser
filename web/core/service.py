"""Framework-agnostic praiser service — the seam every frontend calls.

No UI framework here: a frontend collects options and populates the token env
vars (from its own secret store), then calls :func:`praise`. Swapping Streamlit
for FastAPI/Gradio reuses this unchanged.
"""

import base64
import functools
import os
import pickle
import urllib.parse

from praiser.cache import Cache
from praiser.config import Config
from praiser.github_client import USER_AGENT, RateLimitError
from praiser.pipeline import run
from praiser.popularity import filter_records
from praiser.registry import KnownProjects
from praiser.render import render, render_highlights

from .cache import local_cache, make_result_cache


# External data sources praiser depends on, for the ?diag reachability panel.
# The founder/creator roles come from Wikidata (WDQS) → Wikipedia; those hosts
# rate-limit per IP and throttle shared cloud egress (e.g. Streamlit Community
# Cloud) harder than residential IPs — so a role can be missing purely because
# the deployed host can't reach them. GitHub is the baseline (must be reachable).
_DIAG_SOURCES = [
    ("Wikidata Query Service",
     "https://query.wikidata.org/sparql?format=json&query="
     + urllib.parse.quote("SELECT ?a WHERE { ?item wdt:P1324 ?repo . "
       'FILTER(REGEX(STR(?repo),"github.com/scipy/scipy","i")) '
       "?a schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . } LIMIT 1"),
     "application/sparql-results+json"),
    ("Wikipedia API",
     "https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext&section=0&format=json&page=SciPy",
     "application/json"),
    ("GitHub API (baseline)",
     "https://api.github.com/repos/scipy/scipy", "application/vnd.github+json"),
]


def _praiser_geturl_probe(url: str, accept: str):
    """Fetch via praiser's REAL client (the same get_url the extractors use), so
    the check reflects what praiser can actually reach. Returns (ok, detail)."""
    import tempfile

    from praiser.forge import GitHubForge
    forge = GitHubForge(None, Cache(tempfile.mkdtemp()))  # no auth (external URLs)
    try:
        body = forge.get_url(url, accept=accept)
        return (body is not None), (f"{len(body)} bytes" if body
                                    else "unreachable (throttled/blocked/timeout)")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        forge.close()


def diagnose_external_sources(probe=_praiser_geturl_probe):
    """Probe each external source from THIS host via praiser's real get_url — a
    lightweight, opt-in (?diag) reachability check for the intermittent WDQS/
    Wikipedia throttling of cloud IPs. ``probe`` is injectable for tests. Returns
    ``{"user_agent", "checks": [{name, url, ok, detail}]}``."""
    checks = []
    for name, url, accept in _DIAG_SOURCES:
        ok, detail = probe(url, accept)
        checks.append({"name": name, "url": url, "ok": ok, "detail": detail})
    return {"user_agent": USER_AGENT, "checks": checks}


def diagnose_founder(repo: str = "scipy/scipy", name: str = "Pearu Peterson") -> dict:
    """Trace founder resolution for one repo FROM THIS HOST, to localize why a
    known Author is missing on the deployment (#108). Reports: the registry title
    hint, the entry currently in the shared founder cache (which shadows a fix and
    survives Refresh), and a LIVE re-resolution (founder cache bypassed) with the
    roles it yields for ``name``."""
    from praiser.forge import GitHubForge
    from praiser.identity import resolve_identity
    from praiser.models import Candidate, Identity
    from praiser.extractors.base import ExtractContext
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor

    reg = _registry()
    shared = make_result_cache()
    cache_key = Cache.key("wikipedia-authors", repo)
    cached = shared.get(cache_key, default=None) if shared is not None else None

    forge = GitHubForge(_token_for("github"), local_cache())
    ext = WikipediaFoundersExtractor()
    cand = Candidate(repo, stars=15000)
    login = repo.split("/", 1)[0]                        # scipy → "scipy" (a real login)
    login = "pearu"                                      # the reported user

    def _roles_for(identity):
        ctx = ExtractContext(identity=identity, forge=forge, registry=reg,
                             use_wikidata=True, role_discovery_floor=1000,
                             founder_cache=None)          # bypass cache → LIVE
        return ext._authors(cand, ctx), [e.role for e in ext.extract(cand, ctx)]

    try:
        # (1) control: hardcoded name — proves the extractor path works.
        resolved, roles = _roles_for(Identity(primary_login="_diag", names={name}))
        # (2) the REAL thing: resolve the scanned user's identity like a scan does,
        # and see whether it carries the name the extractor matches on.
        real = resolve_identity(forge, login)
        real_names = sorted(real.names)
        _, real_roles = _roles_for(real)
    except Exception as exc:                             # noqa: BLE001
        resolved, roles, real_names, real_roles = (
            f"EXC {type(exc).__name__}: {exc}", [], [], [])
    finally:
        forge.close()
    return {
        "repo": repo, "name": name,
        "registry_title": reg.wikipedia_title(repo),
        "shared_cache_entry": cached,      # shadows fixes; Refresh won't clear
        "live_resolved": resolved,         # (title, [authors]) resolved now, cache bypassed
        "live_roles": roles,               # roles for the hardcoded `name` (control)
        "resolved_identity_login": login,
        "resolved_identity_names": real_names,   # what resolve_identity gives the scan
        "roles_with_resolved_identity": real_roles,   # what the SCAN would get
    }


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

# One-click feedback: buttons under a result that open a pre-filled praiser issue
# (GitHub shows the form pre-filled for review — nothing is submitted until the
# user clicks, so this never posts data on its own). The scan context makes a
# report reproducible; the user still writes the "what's wrong" part.
_ISSUES_NEW = "https://github.com/openteams-ai/praiser/issues/new"
# GitHub's prefill is a GET request; keep the whole URL well under the ~8KB cap.
_FEEDBACK_MAX_BODY = 5000

# Triage queue: EVERY feedback button applies `needs-triage`, so web-submitted
# issues land in one scannable queue (a human can add it by hand too). A scan
# reviews `label:needs-triage` issues, then swaps `needs-triage` -> `agent-triaged`
# so it isn't re-scanned. `false-positive`/`false-negative` are orthogonal accuracy
# sub-types — an issue with no triage label is intentionally left alone.
TRIAGE_LABEL = "needs-triage"
TRIAGED_LABEL = "agent-triaged"

FEEDBACK_KINDS = [
    {
        "key": "false-positive",
        "button": "🚩 Wrong / over-credited role",
        "labels": f"{TRIAGE_LABEL},false-positive",
        "lead": "A role shown in this result looks wrong or over-credited.",
        "prompt": "Which project and role is inaccurate, and why? (e.g. only a "
                  "few drive-by PRs, a name collision, or a vendored/forked copy)",
    },
    {
        "key": "false-negative",
        "button": "🔍 Missing a role",
        "labels": f"{TRIAGE_LABEL},false-negative",
        "lead": "A real elevated role is missing from this result.",
        "prompt": "Which project and role should appear? A link as evidence "
                  "(CODEOWNERS, a release, a governance page) helps a lot.",
    },
    {
        "key": "feedback",
        "button": "🐛 Bug or 💡 idea",
        "labels": TRIAGE_LABEL,     # queued for triage; sub-type decided on review
        "lead": "Bug report or feature request.",
        "prompt": "What happened (and what did you expect), or what would you "
                  "like praiser to do?",
    },
]


def _feedback_body(kind, username, forge, version, opts, result_text, reporter):
    body = (
        f"{kind['lead']}\n\n"
        f"{kind['prompt']}\n\n"
        "<!-- Scan context is pre-filled below to help reproduce. "
        "Please review before submitting. -->\n\n"
        "### Scan\n"
        f"- forge: `{forge}`\n"
        f"- username: `{username}`\n"
        f"- praiser: `{version}`\n"
        + (f"- options: `{opts}`\n" if opts else "")
        # The GitHub account that authors the issue is whoever is logged into
        # github.com in the browser; this only records the app's signed-in user
        # (usually the same person) so we know who to follow up with.
        + (f"- reported by: @{reporter}\n" if reporter else "")
    )
    if result_text:
        budget = _FEEDBACK_MAX_BODY - len(body) - 40
        if budget > 200:
            snippet = (result_text if len(result_text) <= budget
                       else result_text[:budget] + "\n… (truncated)")
            body += "\n### Result\n```\n" + snippet + "\n```\n"
    return body


def feedback_links(username, *, forge, version, result_text="", data_opts=None,
                   reporter=None):
    """Pre-filled 'open a praiser issue' links for the feedback buttons.

    Returns ``[{"label", "url"}]`` — one per :data:`FEEDBACK_KINDS`. Each URL
    pre-fills the issue title, a body with the reproducible scan context + the
    rendered result, and (for accuracy reports) a triage label. ``reporter`` (the
    app's signed-in GitHub login, when available) is recorded in the body for
    follow-up — the issue's actual author is still whoever is logged into
    github.com when they submit."""
    data_opts = data_opts or {}
    opts = ", ".join(
        f"{k}={data_opts[k]}"
        for k in ("discover_roles", "wikidata", "package_registries", "cross_forge")
        if k in data_opts
    )
    links = []
    for kind in FEEDBACK_KINDS:
        params = {
            "title": f"[{kind['key']}] {username} ({forge})",
            "body": _feedback_body(kind, username, forge, version, opts,
                                   result_text, reporter),
        }
        if kind["labels"]:
            params["labels"] = kind["labels"]
        links.append({"label": kind["button"],
                      "url": f"{_ISSUES_NEW}?{urllib.parse.urlencode(params)}"})
    return links

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
# v6: recompute for this session's extraction-logic changes that v5 predates —
# Wikipedia-infobox authors (#94), release_manager role (#95/#99), score by
# strongest-supported claim (#98), contributor totals #R/N (#87), sole-contributor
# suppression (#106), and the transient-URL-cache fix (#105). Without this bump
# the shared result cache kept serving pre-Author results (e.g. scipy missing
# pearu's Author) for 30 days — the real cause of #108.
CACHE_VERSION = 6

# A small index of recently-scanned (forge, login) pairs — the cache keys are
# hashed and can't be enumerated, so we track names separately for a UI picker.
_RECENT_KEY = Cache.key("recent-scans-index")
_RECENT_CAP = 50


def _record_recent(rcache, forge: str, username: str) -> None:
    """Prepend (forge, username) to the recent-scans index (best-effort).

    Forge usernames are case-insensitive, so store the canonical lowercase handle
    and dedupe case-insensitively — "Pearu" and "pearu" are one entry."""
    if rcache is None:
        return
    try:
        idx = rcache.get(_RECENT_KEY) or []
        entry = [forge, username.lower()]
        idx = [entry] + [e for e in idx if not _same_scan(e, entry)]
        rcache.set(_RECENT_KEY, idx[:_RECENT_CAP])
    except Exception:
        pass


def _same_scan(a, b) -> bool:
    """Whether two recent-index entries name the same (forge, user), case-insensitively."""
    return (isinstance(a, list) and len(a) == 2
            and a[0] == b[0] and str(a[1]).lower() == str(b[1]).lower())


def recent_scans(result_cache=None) -> list[dict]:
    """Recently-scanned ``[{"forge", "username"}]``, most-recent-first (for a UI
    picker). Reads the shared index once; degrades to [] on any error. Dedupes
    case-insensitively so pre-existing mixed-case entries collapse to one."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return []
    try:
        idx = rcache.get(_RECENT_KEY) or []
    except Exception:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in idx:
        if not (isinstance(e, list) and len(e) == 2):
            continue
        key = (e[0], str(e[1]).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"forge": e[0], "username": e[1]})
    return out


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
    refresh: bool = False,          # re-scan: bypass caches for person-anchored fetches
    token: str | None = None,       # explicit token (e.g. a signed-in user's) — overrides env
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
    if rcache is not None and not refresh:      # refresh forces a re-scan
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
        token=token or _token_for(forge),   # signed-in user's token wins, else the shared bot
        min_stars=0,                     # collect everything; filter at render time
        use_llm=discover_roles,          # only load the LLM when it's wanted
        discover_roles=discover_roles,
        use_wikidata=wikidata,
        use_package_registries=package_registries,
        cross_forge=cross_forge,
        refresh=refresh,                 # scoped in the pipeline: anchored repos only
        quiet=True,
        save_registry=False,             # a shared service shouldn't mutate the registry
    )
    result = run(
        config,
        cache=http_cache if http_cache is not None else local_cache(refresh=refresh),
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


def scan_with_fallback(username, token_options, *, data_opts, exhausted, now,
                       collect_fn=collect):
    """Scan trying each token in order, skipping ones known rate-limited, and
    falling back on a hit — so a signed-in user's own quota is used first and the
    shared bot token backs it up (and vice-versa as limits reset).

    ``token_options``: ordered ``[(label, token), …]``. ``exhausted``: a
    ``{label: reset_epoch}`` map (mutated in place; persist it across scans in the
    session so a token is skipped until it resets). ``now``: current epoch.

    Returns ``(result, label, soonest_reset)``:
      * complete scan → ``(RunResult, label, None)``
      * every token rate-limited, but one produced a partial result →
        ``(partial_RunResult, label, soonest_reset_epoch)``
      * every token rate-limited with nothing usable → ``(None, None, soonest)``
    A mid-scan hit (partial result) counts as rate-limited and falls back; the
    shared HTTP cache makes each retry resume cheaply, so a second token with
    fresh quota usually completes."""
    soonest = None
    partial = None  # (result, label) — best-effort if nothing completes
    attempted = False  # has any token actually run a scan yet?

    def _mark(label, reset_epoch):
        nonlocal soonest
        exhausted[label] = reset_epoch
        soonest = reset_epoch if soonest is None else min(soonest, reset_epoch)

    for label, token in token_options:
        if exhausted.get(label, 0) > now:            # still cooling down — skip
            soonest = exhausted[label] if soonest is None else min(soonest, exhausted[label])
            continue
        # A --refresh applies to the FIRST token that runs; a fallback token then
        # resumes from the now-warm cache (refresh=False) instead of re-fetching
        # everything again — else a rate-limited refresh burns the backup quota too.
        opts = data_opts
        if attempted and data_opts.get("refresh"):
            opts = {**data_opts, "refresh": False}
        attempted = True
        try:
            result = collect_fn(username, token=token, **opts)
        except RateLimitError as exc:
            _mark(label, now + (exc.reset_in or 60))
            continue
        if getattr(result, "partial_reset_in", None):   # hit mid-scan
            _mark(label, now + result.partial_reset_in)
            if partial is None:
                partial = (result, label)
            continue
        return result, label, None                       # complete
    if partial is not None:
        return partial[0], partial[1], soonest           # best-effort partial
    return None, None, soonest


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
        # link_repos: the web renders highlights as markdown (repos clickable).
        return render_highlights(username, primary, highlights, secondary,
                                 link_repos=True)
    fmt = "json" if view == "json" else "md"
    return render(username, primary, fmt, secondary)


def praise(username: str, *, view: str = "highlights", highlights: int = 8,
           min_stars: int = 50, progress=None, **data_options) -> str:
    """Convenience: collect then render in one call (CLI-like callers)."""
    result = collect(username, progress=progress, **data_options)
    return render_result(result, username, view=view, highlights=highlights,
                         min_stars=min_stars)

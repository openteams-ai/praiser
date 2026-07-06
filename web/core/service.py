"""Framework-agnostic praiser service — the seam every frontend calls.

No UI framework here: a frontend collects options and populates the token env
vars (from its own secret store), then calls :func:`praise`. Swapping Streamlit
for FastAPI/Gradio reuses this unchanged.
"""

import base64
import functools
import os
import pickle
import time
import urllib.parse

from praiser.cache import Cache
from praiser.config import Config
from praiser.github_client import USER_AGENT, RateLimitError
from praiser.pipeline import run
from praiser.popularity import filter_records
from praiser.registry import KnownProjects
from praiser.render import render, render_highlights

from .cache import local_cache, make_result_cache


# External data sources praiser depends on, for the admin reachability panel.
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
    lightweight reachability check (shown in the admin frame) for the intermittent
    WDQS/Wikipedia throttling of cloud IPs. ``probe`` is injectable for tests.
    Returns ``{"user_agent", "checks": [{name, url, ok, detail}]}``."""
    checks = []
    for name, url, accept in _DIAG_SOURCES:
        ok, detail = probe(url, accept)
        checks.append({"name": name, "url": url, "ok": ok, "detail": detail})
    return {"user_agent": USER_AGENT, "checks": checks}


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
        "button": "🔍 Missing a role or project",
        "labels": f"{TRIAGE_LABEL},false-negative",
        "lead": "A real elevated role — or a whole project — is missing from this "
                "result.",
        "prompt": "Which project (and role) should appear? A link as evidence "
                  "(CODEOWNERS, a release, a governance page) helps a lot.\n\n"
                  "Tip: if praiser missed a project entirely, an easy fix is to "
                  "link it as `owner/repo` in your GitHub **profile README** "
                  "(github.com/<you>/<you>) — praiser reads that as self-reported "
                  "work, so the project then shows up on a re-scan.",
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
        # (usually the same person) so we know who to follow up with. Note: no
        # bare "@handle" — that would fire a mention/notification. Backticked.
        + (f"- reported by: `{reporter}`\n" if reporter else "")
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


# Name→username resolution lives in praiser core (shared with the CLI); re-exported
# here so the web app keeps importing service.looks_like_name / service.name_matches.
from praiser.nameresolve import looks_like_name, name_matches, resolve_name  # noqa: E402,F401


def rate_budget(token: str | None = None) -> dict:
    """Live GitHub quota per resource for the given token (or the shared bot),
    ``{name: (remaining, limit, reset_epoch)}`` — via the free ``/rate_limit``
    endpoint. {} on error. Lets the UI warn before the limit bites."""
    from praiser.forge import GitHubForge
    f = GitHubForge(token or _token_for("github"), local_cache())
    try:
        return f.rate_limit_status()
    except Exception:
        return {}
    finally:
        try:
            f.close()
        except Exception:
            pass


def search_people(name: str, *, forge: str = "github", token: str | None = None,
                  use_wikidata: bool = True, limit: int = 8):
    """Resolve a full name → candidate accounts (login/name/bio) for the
    scan-by-name flow. GitHub-only for now (the one forge with user search wired
    up); other forges return [] so the caller shows guidance rather than guessing.
    ``use_wikidata`` adds an opt-in P2037 fallback when the search finds nothing.
    Returns [] on any error too (rate limits propagate — not "no match")."""
    if forge != "github":
        return []
    from praiser.forge import GitHubForge
    f = GitHubForge(token or _token_for("github"), local_cache())
    try:
        _confident, candidates = resolve_name(
            f, name, use_wikidata=use_wikidata, limit=limit)
        return candidates
    except RateLimitError:
        raise                     # surfaced to the caller so it's not "no match"
    except Exception:
        return []
    finally:
        try:
            f.close()
        except Exception:
            pass


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

# Cache catalog: a durable index of the result-cache entries we've written, so the
# admin UI can list and individually invalidate them (the cache keys are hashed and
# otherwise un-enumerable). One shared value keyed by cache_id — works on both
# backends via get/set (no backend-specific commands). Flat, extensible record per
# entry: {forge, username, created} keyed by the entry's cache_id. Also backs the
# "recent scans" picker. Soft-capped (oldest evicted) to bound the value size.
_CATALOG_KEY = Cache.key("cache-catalog")
_CATALOG_CAP = 1000
# Pre-#181 recent-scans index key — abandoned, cleaned up by the admin clear.
_LEGACY_RECENT_KEY = Cache.key("recent-scans-index")

# One-shot "refresh on next scan" marker. Admin Trash writes this in place of the
# cached result (instead of just deleting): a plain delete only clears the shared
# result, so the next scan could still be served stale from the still-warm local
# HTTP fetch cache. The marker makes collect() force a live re-fetch for THAT user
# (only — it lives at the user's own result key). Consumed by the next scan, which
# overwrites the key with the real result. A real result is a base64 string, so a
# dict is an unambiguous marker.
_REFRESH_FLAG = "__praiser_refresh__"
_REFRESH_MARKER = {_REFRESH_FLAG: True}

# Usage stats, all under a readable ``stats:`` sub-namespace so they can be kept
# out of "Wipe ALL cache" by prefix (they're metrics, not cache) — see
# ``wipe_all_cache``. Cheap: a couple of writes per *actual* scan (cache hits
# don't count — see collect). Counters are plain INCRs; distinct people/repos use
# HyperLogLog (approx-distinct, ~12KB fixed, monotonic, deduped across users —
# a repo covered for many users counts once). None store the raw username/repo.
_STATS_PREFIX = "stats:"                 # protected from wipe
_STATS_SCANS = "stats:scans"             # total completed scans (INCR)
_STATS_USERS = "stats:users"             # distinct forge:username queried (HLL)
_STATS_REPOS = "stats:repos"             # distinct elevated-role repos covered (HLL)
_STATS_DAY_TTL = 120 * 86_400            # keep ~4 months of daily buckets


def _stats_day_key(day: str | None = None) -> str:
    return f"stats:scans:{day or time.strftime('%Y-%m-%d', time.gmtime())}"


def _record_scan_metric(rcache, forge: str, username: str, result) -> None:
    """Record one completed scan in the usage stats (best-effort; never breaks a
    scan): total + per-day counters, distinct people, distinct elevated-role repos."""
    if rcache is None:
        return
    try:
        rcache.incr(_STATS_SCANS)
        rcache.incr(_stats_day_key(), ttl=_STATS_DAY_TTL)
        rcache.pfadd(_STATS_USERS, f"{forge}:{username.lower()}")
        repos = [r.name_with_owner for r in getattr(result, "records", [])
                 if getattr(r, "name_with_owner", None)]
        if repos:
            rcache.pfadd(_STATS_REPOS, *repos)
    except Exception:
        pass


def public_stats(result_cache=None) -> dict:
    """Cheap, non-identifying usage totals for the public page: distinct people
    scanned, distinct elevated-role projects covered, and total scans. A few O(1)
    reads (cache once per session on the frontend). Zeroes on any error/miss."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    out = {"people": 0, "repos": 0, "scans": 0}
    if rcache is None:
        return out
    try:
        if hasattr(rcache, "pfcount"):
            out["people"] = rcache.pfcount(_STATS_USERS)
            out["repos"] = rcache.pfcount(_STATS_REPOS)
        out["scans"] = int(rcache.get(_STATS_SCANS) or 0)
    except Exception:
        pass
    return out


def _catalog_record(rcache, forge: str, username: str, cache_id: str) -> None:
    """Record a written result-cache entry in the catalog (best-effort)."""
    if rcache is None:
        return
    try:
        cat = rcache.get(_CATALOG_KEY) or {}
        if not isinstance(cat, dict):
            cat = {}
        cat[cache_id] = {"forge": forge, "username": username.lower(),
                         "created": time.time()}
        if len(cat) > _CATALOG_CAP:      # evict oldest by created
            keep = sorted(cat.items(), key=lambda kv: kv[1].get("created", 0),
                          reverse=True)[:_CATALOG_CAP]
            cat = dict(keep)
        rcache.set(_CATALOG_KEY, cat)
    except Exception:
        pass


def cache_catalog(result_cache=None) -> list[dict]:
    """Recorded result-cache entries, most-recent-first:
    ``[{"forge", "username", "cache_id", "created"}]``. [] on any error."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return []
    try:
        cat = rcache.get(_CATALOG_KEY) or {}
    except Exception:
        return []
    if not isinstance(cat, dict):
        return []
    rows = [{"forge": r.get("forge"), "username": r.get("username"),
             "cache_id": cid, "created": r.get("created", 0)}
            for cid, r in cat.items() if isinstance(r, dict)]
    rows.sort(key=lambda r: r["created"], reverse=True)
    return rows


def trash_cache_entry(cache_id: str, result_cache=None) -> bool:
    """Admin: force the next scan of one user to re-fetch live, and drop its catalog
    row. Writes a one-shot refresh marker over the cached result (see
    ``_REFRESH_MARKER``) rather than a plain delete, so the next scan re-fetches
    instead of being served stale from the warm local HTTP cache. Touches only this
    entry (never other users' cache or the shared founder/reverse-index data).
    Returns True on best-effort success."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return False
    try:
        rcache.set(cache_id, _REFRESH_MARKER)
        cat = rcache.get(_CATALOG_KEY) or {}
        if isinstance(cat, dict) and cache_id in cat:
            del cat[cache_id]
            rcache.set(_CATALOG_KEY, cat)
        return True
    except Exception:
        return False


def clear_tracked_scans(result_cache=None) -> int:
    """Admin: trash every user the catalog tracks — i.e. force each one's next scan
    to re-fetch live (a one-shot refresh marker per entry, like ``trash_cache_entry``,
    NOT a plain delete: a delete would let the still-warm local HTTP cache serve
    stale fetches). Also drops the catalog and the legacy recent-index. Keeps the
    expensive shared data (per-repo founder cache, contributor reverse-index).
    Returns the number of tracked scans cleared. Note: cache keys are opaque hashes,
    so this reaches only entries recorded since the catalog existed — older result
    blobs are indistinguishable from founder keys and just TTL out (use
    wipe_all_cache for a true clean slate)."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return 0
    rows = cache_catalog(rcache)
    try:
        for r in rows:
            rcache.set(r["cache_id"], _REFRESH_MARKER)   # trash-all: force fresh
        rcache.delete(_CATALOG_KEY)
        rcache.delete(_LEGACY_RECENT_KEY)
    except Exception:
        pass
    return len(rows)


def wipe_all_cache(result_cache=None) -> int:
    """Admin: wipe the ENTIRE praiser cache namespace — result entries, catalog,
    founder cache, contributor reverse-index, everything — for a clean slate, then
    re-mark the users that were tracked so each one's next scan re-fetches live
    (the per-instance local HTTP fetch cache isn't wiped and would otherwise serve
    stale fetches). Returns the number of keys/entries removed (best-effort).

    The markers are one-shot: consumed on a user's first re-scan (overwritten by
    the real result). Residue for a user who never returns is cleared by the next
    wipe (which wipes markers too before re-marking the then-tracked set) and TTLs
    out regardless — so wipe stays self-cleaning rather than accumulating markers."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return 0
    tracked = cache_catalog(rcache)          # capture BEFORE wiping (catalog goes too)
    removed = 0
    try:
        # Keep usage stats (stats:*) — they're metrics, not cache; a wipe is a
        # cache reset, not a usage-history reset.
        if hasattr(rcache, "clear_all"):     # RedisCache: SCAN + DEL praiser:*
            removed = rcache.clear_all(protect_prefix=_STATS_PREFIX)
        elif hasattr(rcache, "clear"):       # local file Cache: wipe the dir
            removed = rcache.clear(protect_prefix=_STATS_PREFIX)
    except Exception:
        pass
    try:                                     # re-mark known users for a fresh re-scan
        for r in tracked:
            rcache.set(r["cache_id"], _REFRESH_MARKER)
    except Exception:
        pass
    return removed


def usage_summary(result_cache=None) -> dict:
    """Cheap admin cache/usage stats — a handful of O(1) reads, nothing that runs
    during a scan. Returns::

        {"keys": int|None,          # total keys in the cache DB (DBSIZE)
         "tracked_scans": int,      # entries the catalog tracks (a subset of keys)
         "scans_total": int|None,   # lifetime completed scans (counter)
         "scans_today": int|None,   # completed scans today (UTC, rolling counter)
         "people": int,             # distinct usernames queried (HLL)
         "repos": int,              # distinct elevated-role repos covered (HLL)
         "newest": float|None,      # newest tracked-scan created ts
         "oldest": float|None}      # oldest tracked-scan created ts

    Per-category memory (results vs founder cache vs contributor reverse-index)
    is deliberately absent: keys are opaque SHA hashes, so those categories can't
    be told apart without an O(N) MEMORY-USAGE sweep — too costly for a summary.
    """
    rcache = result_cache if result_cache is not None else make_result_cache()
    out = {"keys": None, "tracked_scans": 0, "scans_total": None,
           "scans_today": None, "people": 0, "repos": 0,
           "newest": None, "oldest": None}
    if rcache is None:
        return out
    rows = cache_catalog(rcache)
    out["tracked_scans"] = len(rows)
    if rows:
        out["newest"] = rows[0].get("created")
        out["oldest"] = rows[-1].get("created")
    try:
        if hasattr(rcache, "key_count"):
            out["keys"] = rcache.key_count()
    except Exception:
        pass
    for field, key in (("scans_total", _STATS_SCANS),
                       ("scans_today", _stats_day_key())):
        try:
            v = rcache.get(key)
            out[field] = int(v) if v is not None else None
        except Exception:
            pass
    ps = public_stats(rcache)
    out["people"], out["repos"] = ps["people"], ps["repos"]
    return out


def recent_scans(result_cache=None) -> list[dict]:
    """Recently-scanned ``[{"forge", "username"}]``, most-recent-first, distinct —
    derived from the cache catalog."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in cache_catalog(result_cache):
        key = (r["forge"], str(r["username"]).lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"forge": r["forge"], "username": r["username"]})
    return out


# Seed catalog — a readable log of reverse-index seed runs (target + counts + when).
# The per-repo seed markers (roster-seeded:<repo>) are opaque SHA hashes, so this is
# the only way to show "what's been seeded". One shared key, updated per seed run
# (rare, admin-triggered). Wiped by "Wipe ALL cache"; kept by "Clear cached scans".
_SEED_CATALOG_KEY = Cache.key("seed-catalog")
_SEED_CATALOG_CAP = 500


def record_seed(result: dict, *, forge: str, kind: str, target: str,
                result_cache=None) -> None:
    """Record one seed run in the seed catalog (best-effort). Keyed by
    forge:kind:target. Stores the **authoritative cumulative distinct coverage**
    (``repos_distinct`` / ``contributors_distinct``, computed by praiser.seed from
    the per-target coverage set) — which never regresses because that set only
    grows, so a no-op re-run (0 new) still reports the full totals. Tracks
    first-seen (``created``) and last-run (``updated``) timestamps."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return
    try:
        cat = rcache.get(_SEED_CATALOG_KEY) or {}
        if not isinstance(cat, dict):
            cat = {}
        ckey = f"{forge}:{kind}:{target}".lower()
        prev = cat.get(ckey)
        prev = prev if isinstance(prev, dict) else {}
        now = time.time()
        # Authoritative distinct totals; fall back to the previous row (never a
        # this-run count) if a run didn't report them, so we never regress.
        repos = int(result.get("repos_distinct", prev.get("repos", 0)) or 0)
        contribs = int(result.get("contributors_distinct",
                                  prev.get("contributors", 0)) or 0)
        cat[ckey] = {
            "forge": forge, "kind": kind, "target": target,
            "repos": repos, "contributors": contribs,
            "created": prev.get("created", now),   # first seen
            "updated": now,                        # last run
        }
        if len(cat) > _SEED_CATALOG_CAP:      # evict oldest by last-run
            keep = sorted(cat.items(),
                          key=lambda kv: kv[1].get("updated", kv[1].get("created", 0)),
                          reverse=True)[:_SEED_CATALOG_CAP]
            cat = dict(keep)
        rcache.set(_SEED_CATALOG_KEY, cat)
    except Exception:
        pass


def seed_catalog(result_cache=None) -> list[dict]:
    """Recorded seed runs, most-recently-run first: ``[{"forge", "kind", "target",
    "repos", "contributors", "created", "updated"}]``. ``repos``/``contributors``
    are the cumulative distinct coverage for the target (see ``record_seed``)."""
    rcache = result_cache if result_cache is not None else make_result_cache()
    if rcache is None:
        return []
    try:
        cat = rcache.get(_SEED_CATALOG_KEY) or {}
    except Exception:
        return []
    if not isinstance(cat, dict):
        return []
    rows = [{"forge": r.get("forge"), "kind": r.get("kind"),
             "target": r.get("target"),
             "repos": r.get("repos", r.get("seeded", 0)),   # legacy row fallback
             "contributors": r.get("contributors", 0),
             "created": r.get("created", 0),
             "updated": r.get("updated", r.get("created", 0))}
            for r in cat.values() if isinstance(r, dict)]
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


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
    effective_refresh = refresh
    if rcache is not None and not refresh:      # refresh forces a re-scan
        blob = rcache.get(rkey)                 # the read we'd do anyway (no extra op)
        if isinstance(blob, dict) and blob.get(_REFRESH_FLAG):
            effective_refresh = True    # trashed → re-fetch live for THIS user only
            try:                        # consume: one-shot, so a rate-limit fallback
                rcache.delete(rkey)     # token doesn't force-refresh too (see
            except Exception:           # scan_with_fallback's refresh handling)
                pass
        elif blob is not None:
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
        refresh=effective_refresh,       # scoped in the pipeline: anchored repos only
        quiet=True,
        save_registry=False,             # a shared service shouldn't mutate the registry
    )
    result = run(
        config,
        cache=http_cache if http_cache is not None else local_cache(refresh=effective_refresh),
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
        _catalog_record(rcache, forge, username, rkey)
        _record_scan_metric(rcache, forge, username, result)
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


def filtered_records(result, *, min_stars: int = 50):
    """``(primary, secondary)`` for a display ``min_stars``, score-sorted.

    The single place the display-time popularity split lives (the scan collected
    the full superset at floor 0). Both the text renderers and the web card view
    build on this, so they never diverge.
    """
    allrecs = [*result.records, *result.secondary]
    primary, secondary = filter_records(
        allrecs, min_stars=min_stars, registry=_registry()
    )
    primary.sort(key=lambda r: r.score, reverse=True)
    secondary.sort(key=lambda r: r.score, reverse=True)
    return primary, secondary


def render_result(result, username: str, *, view: str = "highlights",
                  highlights: int = 8, min_stars: int = 50) -> str:
    """Render an already-collected ``RunResult`` for ``view`` (cheap, no network).

    Applies the ``min_stars`` popularity split here (the result was collected at
    floor 0), so changing it re-renders instantly without re-scanning.
    """
    primary, secondary = filtered_records(result, min_stars=min_stars)
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

"""Streamlit UI for praiser — a thin frontend over web.core.

Deploy on Streamlit Community Cloud: main file = web/streamlit/app.py, and set
the token secrets (see web/README.md). All queries and tokens stay server-side.
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

# Streamlit runs this file directly, so only its own directory is on sys.path.
# Add the repo root so `web.core` and `praiser` import regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

import praiser  # noqa: E402
from praiser.github_client import RateLimitError  # noqa: E402
from praiser.pipeline import humanize_wait  # noqa: E402
from web.core import service  # noqa: E402
from web.core.resultcache import SizeBoundedLRU  # noqa: E402

# The version the app actually RUNS: the imported module's __version__ (the repo
# checkout on Streamlit Cloud), not importlib.metadata (which reflects a possibly
# different/absent installed distribution).
PRAISER_VERSION = getattr(praiser, "__version__", "dev")

REPO_URL = "https://github.com/openteams-ai/praiser"
_SCANNED_RE = re.compile(r"scanned (\d+)/(\d+)")
# Per-session result cache, bounded by total size (a RunResult is tens of KB, so
# this holds a whole session's scans; oldest evicted only when over budget).
_CACHE_MB = 200

# --- secrets -> env (server-side only; never sent to the browser) ----------
_SECRET_KEYS = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN", "FORGEJO_TOKEN",
    "GITEE_TOKEN", "BITBUCKET_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
)
for _k in _SECRET_KEYS:
    if _k in st.secrets and not os.environ.get(_k):
        os.environ[_k] = str(st.secrets[_k])


def _fetch_login(token: str) -> str | None:
    """The signed-in user's GitHub login (works with any user token; login is
    public). Best-effort — greeting only."""
    try:
        import httpx
        r = httpx.get("https://api.github.com/user", timeout=10,
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            return r.json().get("login")
    except Exception:
        pass
    return None


def github_account():
    """(login, token) for the signed-in GitHub user, or (None, None), rendering a
    sign-in/out control in the sidebar. Dormant unless the OAuth app is configured
    (GITHUB_OAUTH_CLIENT_ID/SECRET in the deployment's secrets). The token is
    session-only — never logged, cached, or written anywhere."""
    if "GITHUB_OAUTH_CLIENT_ID" not in st.secrets or \
       "GITHUB_OAUTH_CLIENT_SECRET" not in st.secrets:
        return None, None
    with st.sidebar:
        st.markdown("### GitHub account")
        tok = st.session_state.get("gh_user_token")
        if tok:
            login = st.session_state.get("gh_user_login")
            st.success(f"Signed in as @{login}" if login else "Signed in")
            st.caption("Scans use your GitHub rate limit first, so you're not "
                       "blocked when the shared demo limit is hit.")
            if st.button("Sign out"):
                st.session_state.pop("gh_user_token", None)
                st.session_state.pop("gh_user_login", None)
                st.rerun()
            return login, tok
        st.caption("Sign in with GitHub to scan on your own rate limit (public, "
                   "read-only) — handy when the shared demo limit is reached.")
        try:
            from streamlit_oauth import OAuth2Component
        except Exception:
            st.caption("_(sign-in unavailable: streamlit-oauth not installed)_")
            return None, None
        redirect = (st.secrets["GITHUB_OAUTH_REDIRECT_URI"]
                    if "GITHUB_OAUTH_REDIRECT_URI" in st.secrets
                    else "https://praiser.streamlit.app/")
        oauth = OAuth2Component(
            st.secrets["GITHUB_OAUTH_CLIENT_ID"],
            st.secrets["GITHUB_OAUTH_CLIENT_SECRET"],
            "https://github.com/login/oauth/authorize",
            "https://github.com/login/oauth/access_token")
        # read:org lets discovery read the user's org memberships (org-repo
        # discovery + affiliation). Public repo reads need no scope; without
        # read:org the scan still works but org features degrade gracefully.
        result = oauth.authorize_button("Sign in with GitHub", redirect,
                                        scope="read:org", key="gh_oauth")
        if result and "token" in result:
            t = result["token"]["access_token"]
            st.session_state["gh_user_token"] = t
            st.session_state["gh_user_login"] = _fetch_login(t)
            st.rerun()
    return None, None


st.set_page_config(page_title="praiser", page_icon="🌟")
st.title("🌟 praiser")
st.caption("The open-source projects where a person holds an elevated role — "
           "author, maintainer, steering council, standards author — with "
           "evidence links, across GitHub, GitLab, Codeberg, Gitee, Bitbucket "
           "and cgit hosts.")
st.caption(f"ℹ️ More information: [{REPO_URL.split('//', 1)[1]}]({REPO_URL}) · "
           f"praiser v{PRAISER_VERSION}")

USER_LOGIN, USER_TOKEN = github_account()   # signed-in GitHub user (or None, None)

# Forges usable from just a username (cgit needs an instance URL + --add-repo,
# which this demo doesn't expose; the core library still supports it via CLI).
DEMO_FORGES = [f for f in service.FORGES if f != "cgit"]

# Recently-scanned names (loaded once per session — one shared-cache read — then
# kept fresh locally). Picking one pre-fills the form via the on_change callback.
if "recent" not in st.session_state:
    st.session_state["recent"] = service.recent_scans()


def _pick_recent():
    choice = st.session_state.get("recent_pick")
    if choice and " · " in choice:
        forge_name, uname = choice.split(" · ", 1)
        st.session_state["forge_sel"] = forge_name
        st.session_state["uname"] = uname


recent = st.session_state["recent"]
if recent:
    st.selectbox(
        "Recent scans", ["—", *(f"{r['forge']} · {r['username']}" for r in recent)],
        key="recent_pick", on_change=_pick_recent,
        help="Pick a previously scanned account to pre-fill the form.")

# Data-collection controls live in a form: they only take effect on "Praise",
# so a scan runs only on an explicit submit.
with st.form("q"):
    username = st.text_input("Forge username", key="uname", placeholder="e.g. pearu")
    forge = st.selectbox("Forge", DEMO_FORGES, key="forge_sel")
    forge_url = ""  # self-hosted instance URL is a CLI/library feature, not the demo
    c3, c4 = st.columns(2)
    wikidata = c3.checkbox("Wikidata roles", value=True)
    package_registries = c4.checkbox("Package registries", value=True)
    cross_forge = c3.checkbox("Cross-forge (follow profile links)", value=False)
    discover_roles = c4.checkbox("LLM founder/role discovery (slower, costs)",
                                 value=False)
    refresh = c3.checkbox(
        "Refresh (ignore cache, re-scan)", value=False,
        help="Force a fresh scan instead of returning a cached result. Only the "
             "repos you're actually connected to are re-fetched; repos surfaced "
             "solely by org membership keep using the cache, so a refresh won't "
             "exhaust the API rate limit.")
    submitted = st.form_submit_button(
        "🌟 Praise", type="primary", use_container_width=True)

# Display controls live OUTSIDE the form: changing them reruns immediately and
# re-renders the already-collected result from cache — no button, no re-scan.
# (min_stars is a display filter — the scan collects the full superset.)
d1, d2, d3 = st.columns(3)
view = d1.selectbox("View", service.VIEWS, index=0)
highlights = d2.slider("Highlights (top N)", 3, 100, 8)
min_stars = d3.slider("Min stars", 0, 1000, 50, step=10)


def _show(result, uname):
    """Render a RunResult with the current display controls (view/N/min-stars)."""
    out = service.render_result(result, uname, view=view,
                                highlights=highlights, min_stars=min_stars)
    if view == "json":
        st.json(json.loads(out))
    else:
        st.markdown(out)   # highlights (repos linked) + markdown both render here


def _run_scan(username, data_opts, token_options, exhausted):
    """Scan in a worker thread (live progress bar on the main thread), trying the
    token options in order and falling back on rate limits (signed-in user's
    quota first, shared bot behind it). Returns (result, elapsed, label): result
    may be complete, partial (with .partial_reset_in), or None (all limited).
    The worker never touches st.* — it only mutates the plain `state`/`exhausted`."""
    state = {"msg": "starting…", "result": None, "label": None,
             "reset": None, "error": None, "done": False}

    def _work():
        def _collect(u, token=None, **k):
            return service.collect(u, token=token,
                                   progress=lambda m: state.update(msg=m), **k)
        try:
            result, label, reset = service.scan_with_fallback(
                username, token_options, data_opts=data_opts,
                exhausted=exhausted, now=time.time(), collect_fn=_collect)
            state.update(result=result, label=label, reset=reset)
        except Exception as exc:  # surface a message, never a traceback
            state["error"] = str(exc)
        finally:
            state["done"] = True

    started = time.time()
    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    bar, status = st.empty(), st.empty()
    while not state["done"]:
        m = _SCANNED_RE.search(state["msg"])
        bar.progress(min(1.0, int(m.group(1)) / max(1, int(m.group(2)))) if m else 0.0)
        status.caption(f"⏳ {state['msg']}")
        time.sleep(0.2)
    worker.join()
    bar.empty()
    status.empty()
    if state["error"]:
        st.error(f"Failed: {state['error']}")
        st.stop()
    if state["result"] is None:      # every token rate-limited, nothing usable
        reset = state["reset"]
        wait = humanize_wait(int(reset - time.time())) if reset else "a while"
        msg = (f"⏳ The GitHub API rate limit was reached on all available tokens. "
               f"Please try again in {wait}.")
        if not USER_TOKEN:
            msg += ("\n\n**Sign in with GitHub** (sidebar) to scan on your own "
                    "rate limit — independent of other demo users.")
        msg += (f"\n\nOr run praiser locally: `pip install praiser`, then "
                f"`praiser {username}`. See {REPO_URL}.")
        st.warning(msg)
        st.stop()
    return state["result"], time.time() - started, state["label"]


# Size-bounded LRU across ALL scans this session, so revisiting an earlier
# user/options is instant (not just tweaking N of the latest scan).
if "results" not in st.session_state:
    st.session_state["results"] = SizeBoundedLRU(_CACHE_MB * 1024 * 1024)
cache = st.session_state["results"]

# A scan runs ONLY on submit. It stores the result and marks it "active"; the
# render block below then shows the active result with the CURRENT view/N — so
# a later change to a display control reruns and re-renders instantly, with no
# submit and no re-scan.
if submitted:
    if not username.strip():
        st.warning("Enter a username.")
        st.stop()
    uname = username.strip()
    data_opts = {
        "forge": forge, "forge_url": forge_url.strip(),
        "discover_roles": discover_roles, "wikidata": wikidata,
        "package_registries": package_registries, "cross_forge": cross_forge,
        # Not a cache-key discriminator (not in DATA_OPTIONS) — a refreshed result
        # is the same result, just recomputed. It reaches collect() to bypass the
        # caches for person-anchored fetches.
        "refresh": refresh,
    }
    # Cache key excludes the display options (view/highlights) on purpose.
    key = (uname, *(data_opts[k] for k in service.DATA_OPTIONS))
    if not refresh and cache.get(key) is not None:
        st.success("✅ Showing cached results — change the username, forge, or a "
                   "scan option to re-scan (or tick Refresh to force one).")
    else:
        st.info(
            ("🔄 Refreshing — re-scanning the repos you're connected to; "
             "org-membership repos ride the cache. "
             if refresh else
             "⏳ A first-time scan can take ~30 seconds to a few minutes — praiser "
             "queries the forge across many repositories (longer with cross-forge "
             "or LLM discovery on). ")
            + "Changing the view, top-N or min-stars is instant."
        )
        # Token options: a signed-in user's own quota first, the shared bot token
        # behind it (GitHub only — the OAuth token is a GitHub token). Other forges
        # use their configured env token via collect(). The exhausted map persists
        # in the session so a rate-limited token is skipped until it resets.
        exhausted = st.session_state.setdefault("tok_exhausted", {})
        if forge == "github":
            bot = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            token_options = ([("your GitHub account", USER_TOKEN)] if USER_TOKEN else [])
            token_options += ([("the shared demo token", bot)] if bot else [])
            token_options = token_options or [("anonymous (60/hr)", None)]
        else:
            token_options = [(forge, None)]     # forge's own env token, no fallback
        result, elapsed, label = _run_scan(uname, data_opts, token_options, exhausted)
        if result.partial_reset_in is not None:
            # Every token hit its limit mid-scan → incomplete. Don't cache it;
            # show what we have with a clear warning + wait time.
            st.warning(
                "⚠️ Partial results — the API rate limit was reached mid-scan on "
                "all available tokens, so some projects may be missing. Re-scan in "
                f"{humanize_wait(result.partial_reset_in)} for the full record"
                + ("" if USER_TOKEN else " (or sign in with GitHub for your own limit)")
                + f". Or run praiser locally (`pip install praiser`; see {REPO_URL})."
            )
            _show(result, uname)
            st.stop()
        cache.put(key, result)
        _via = f" (via {label})" if label and USER_TOKEN else ""
        st.success(f"✅ Scan finished in {elapsed:.1f} seconds{_via}.")
        # Reflect this scan in the recent-picker immediately (shared index is
        # updated inside service.collect()).
        entry = {"forge": forge, "username": uname}
        st.session_state["recent"] = [entry] + [
            r for r in st.session_state.get("recent", []) if r != entry][:49]
    st.session_state["active"] = (key, uname)

# Render the active result (from a fresh submit OR a display-only rerun).
active = st.session_state.get("active")
if active is not None:
    key, uname = active
    result = cache.get(key)
    if result is None:  # evicted from the LRU — ask for a re-scan
        st.info("Previous results expired — click Praise to scan again.")
    else:
        _show(result, uname)

# --- Seed the shared reverse-index (#65) --------------------------------------
# Shown ONLY when the URL has `?seed` AND the deployer opted in (SEED_ENABLED in
# secrets — one-time deployer config, not a secret the triggering user enters).
# So normal users never see it; a knowledgeable user opens `?seed=github/numpy`
# (org) or `?seed=github/pytorch/pytorch` (single repo) and clicks Seed — no
# login, no secret. (Streamlit has no path routing, so the query param is the
# `/seed/...` equivalent.) Seeding is bounded, idempotent (30-day per-repo
# markers), additive, and spends the deployment's bot quota, not the user's.
if "SEED_ENABLED" in st.secrets and "seed" in st.query_params:
    from web import seed as webseed
    _forge, _kind, _name = webseed.parse_seed_target(st.query_params.get("seed", ""))
    if _name and "seed_name" not in st.session_state:
        st.session_state["seed_name"] = _name             # pre-fill from the URL
    with st.expander("🌱 Seed reverse-index", expanded=True):
        a_forge = st.selectbox("Forge", service.FORGES,
                               index=service.FORGES.index(_forge)
                               if _forge in service.FORGES else 0,
                               key="seed_forge",
                               help="Only GitHub is functional today.")
        a_kind = st.radio("Seed", ["org", "repo"],
                          index=0 if _kind == "org" else 1,
                          horizontal=True, key="seed_kind",
                          help="An org's repos, or a single owner/repo.")
        a_name = st.text_input(
            "Org or owner/repo", key="seed_name",
            placeholder="numpy" if a_kind == "org" else "pytorch/pytorch")
        a_budget = st.number_input("Repos to seed (budget, org only)",
                                   1, 200, 30, key="seed_budget")
        if st.button("Seed", key="seed_go"):
            if not a_name.strip():
                st.warning("Enter an org or owner/repo.")
            else:
                with st.spinner(f"Seeding {a_forge}/{a_name}…"):
                    try:
                        res = webseed.run_seed(a_name.strip(), a_forge,
                                               int(a_budget), a_kind)
                    except Exception as exc:
                        st.error(f"Seed failed: {exc}")
                        res = None
                if res:
                    st.success(
                        f"Seeded {res['seeded']} repo(s), "
                        f"{res['contributors_indexed']} contributor entries — "
                        f"{res['stopped']}. Re-run to continue (resumes where it left off)."
                    )

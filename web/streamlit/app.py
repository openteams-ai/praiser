"""Streamlit UI for praiser — a thin frontend over web.core.

Deploy on Streamlit Community Cloud: main file = web/streamlit/app.py, and set
the token secrets (see web/README.md). All queries and tokens stay server-side.
"""

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
from praiser.render import (  # noqa: E402
    human_stars,
    render_highlights,
    render_role_glossary,
    role_display,
)
from web.core import service  # noqa: E402
from web.core.resultcache import SizeBoundedLRU  # noqa: E402

# The version the app actually RUNS: the imported module's __version__ (the repo
# checkout on Streamlit Cloud), not importlib.metadata (which reflects a possibly
# different/absent installed distribution).
PRAISER_VERSION = getattr(praiser, "__version__", "dev")

REPO_URL = "https://github.com/openteams-ai/praiser"
_SCANNED_RE = re.compile(r"scanned (\d+)/(\d+)")
# Proper display names per forge, for the username field label.
_FORGE_LABEL = {"github": "GitHub", "gitlab": "GitLab", "codeberg": "Codeberg",
                "gitee": "Gitee", "bitbucket": "Bitbucket", "cgit": "cgit"}
# GitHub mark (neutral gray so it reads on a light or dark button) for the
# sign-in control's icon — a data URI, self-contained.
_GH_ICON = (
    "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org"
    "%2F2000%2Fsvg%27%20viewBox%3D%270%200%2016%2016%27%20width%3D%2716%27%20"
    "height%3D%2716%27%3E%3Cpath%20fill%3D%27%23808A94%27%20d%3D%27M8%200C3.58%20"
    "0%200%203.58%200%208c0%203.54%202.29%206.53%205.47%207.59.4.07.55-.17.55-.38"
    "%200-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13"
    "-.28-.15-.68-.52-.01-.53.63-.01%201.08.58%201.23.82.72%201.21%201.87.87%20"
    "2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95%200-.87.31-1.59.82"
    "-2.15-.08-.2-.36-1.02.08-2.12%200%200%20.67-.21%202.2.82.64-.18%201.32-.27%20"
    "2-.27.68%200%201.36.09%202%20.27%201.53-1.04%202.2-.82%202.2-.82.44%201.1.16"
    "%201.92.08%202.12.51.56.82%201.27.82%202.15%200%203.07-1.87%203.75-3.65%20"
    "3.95.29.25.54.73.54%201.48%200%201.07-.01%201.93-.01%202.2%200%20.21.15.46"
    ".55.38A8.013%208.013%200%200%200%2016%208c0-4.42-3.58-8-8-8z%27%2F%3E%3C%2F"
    "svg%3E")
# Per-session result cache, bounded by total size (a RunResult is tens of KB, so
# this holds a whole session's scans; oldest evicted only when over budget).
_CACHE_MB = 200

# --- secrets -> env (server-side only; never sent to the browser) ----------
_SECRET_KEYS = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN", "FORGEJO_TOKEN",
    "GITEE_TOKEN", "BITBUCKET_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "PRAISER_DIAG",   # opt-in founder-resolution trace surfaced in RunResult.diag
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
    sign-in/out control INTO THE CURRENT CONTAINER (caller places it, e.g. as a
    sidebar option). Dormant unless the OAuth app is configured
    (GITHUB_OAUTH_CLIENT_ID/SECRET in the deployment's secrets). The token is
    session-only — never logged, cached, or written anywhere."""
    if "GITHUB_OAUTH_CLIENT_ID" not in st.secrets or \
       "GITHUB_OAUTH_CLIENT_SECRET" not in st.secrets:
        return None, None
    tok = st.session_state.get("gh_user_token")
    if tok:
        login = st.session_state.get("gh_user_login")
        st.caption(f"✅ Signed in as @{login}" if login else "✅ Signed in")
        if st.button("Sign out", use_container_width=True):
            st.session_state.pop("gh_user_token", None)
            st.session_state.pop("gh_user_login", None)
            st.rerun()
        return login, tok
    # A short caption explains the purpose; the full-width button below is
    # self-labelled ("Sign in with GitHub"), so no separate heading is needed.
    st.caption("Optional — sign in to scan on your own GitHub rate limit "
               "(public, read-only; the token is session-only).")
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
    # Full-width + a GitHub mark so it reads as a proper option, not a raw button.
    result = oauth.authorize_button(
        "Sign in with GitHub", redirect, scope="read:org", key="gh_oauth",
        icon=_GH_ICON, use_container_width=True)
    if result and "token" in result:
        t = result["token"]["access_token"]
        st.session_state["gh_user_token"] = t
        st.session_state["gh_user_login"] = _fetch_login(t)
        st.rerun()
    return None, None


_BUDGET_BUCKETS = [("core", "REST"), ("graphql", "GraphQL"), ("search", "search")]


def _rate_budget(token):
    """Cached live GitHub quota {resource: (remaining, limit, reset_epoch)} for
    this session (via the free /rate_limit endpoint). Cleared after a scan so it
    re-fetches the now-lower budget."""
    if "rate_budget" not in st.session_state:
        st.session_state["rate_budget"] = service.rate_budget(token)
    return st.session_state["rate_budget"]


def _render_budget_note(token):
    """Sidebar note on remaining GitHub quota — warns when low, nudges sign-in."""
    b = _rate_budget(token)
    present = [(lbl, *b[res]) for res, lbl in _BUDGET_BUCKETS if res in b]
    if not present:
        return
    signed_in = bool(token)
    summary = " · ".join(f"{lbl} {rem}/{lim}" for lbl, rem, lim, _ in present)
    low = any(lim and rem < 0.15 * lim for _, rem, lim, _ in present)
    soonest = min((r for *_, r in present if r), default=0)
    resets = (f", resets in {humanize_wait(max(0, soonest - int(time.time())))}"
              if soonest else "")
    who = "Your GitHub budget" if signed_in else "Shared demo budget"
    if low:
        st.warning(f"⚠️ {who} running low: {summary}{resets}."
                   + ("" if signed_in else " Sign in above to scan on your own limit."))
    else:
        st.caption(f"{who}: {summary}{resets}."
                   + ("" if signed_in else " A few scans can use it up — sign in "
                      "above for your own limit."))


def _budget_reset_at():
    """Soonest reset epoch across the cached budget buckets (0 if unknown), for the
    scan-progress countdown."""
    b = st.session_state.get("rate_budget") or {}
    resets = [v[2] for v in b.values() if v and v[2]]
    return min(resets) if resets else 0


st.set_page_config(page_title="praiser", page_icon="🌟")
st.title("🌟 praiser")
st.caption("Find the open-source projects where someone holds an elevated role — "
           "with evidence for every claim.")
# One "About praiser" dropdown at the top holds both the intro and the role
# glossary — so the meaning of every role is one obvious place to look (the result
# cards point here). Kept out of the main flow so the landing screen stays lean.
with st.expander("About praiser"):
    st.markdown(
        "**praiser** records the projects where a person is an **author, "
        "maintainer, code owner, steering-council member, standards author, "
        "release manager, or core contributor** — each with a clickable "
        "**evidence link** and a **confidence** score. It works across GitHub, "
        "GitLab, Codeberg, Gitee, Bitbucket and cgit hosts.\n\n"
        f"ℹ️ More: [{REPO_URL.split('//', 1)[1]}]({REPO_URL}) · "
        f"praiser v{PRAISER_VERSION}")
    st.markdown("---")
    st.markdown("#### What do these roles mean?")
    st.markdown(render_role_glossary())

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


# LLM founder/role discovery spends the DEPLOYMENT's shared LLM budget, so it's
# hidden on the public demo unless the deployer opts in (mirrors SEED_ENABLED).
_LLM_ENABLED = "LLM_DISCOVERY_ENABLED" in st.secrets

# Options live in the sidebar (not the main form): they're rarely changed and have
# good defaults, so the main area stays a clean username + Praise. They're plain
# widgets read at submit time — toggling one just reruns (re-rendering the cached
# result); a scan still runs only on the Praise button.
with st.sidebar:
    st.markdown("### Options")
    forge = st.selectbox("Forge", DEMO_FORGES, key="forge_sel",
                         help="GitHub by default; switch to scan another host.")
    refresh = st.checkbox(
        "Refresh (ignore cache)", value=False,
        help="Force a fresh re-scan instead of a cached result. Only repos "
             "you're actually connected to are re-fetched, so a refresh won't "
             "exhaust the API rate limit.")
    package_registries = st.checkbox(
        "Package registries", value=True,
        help="Also check PyPI / npm / crates.io: credit the person as author "
             "(PyPI) or maintainer (npm/crates) of a package — but only when "
             "the package's metadata links back to a repo praiser found.")
    cross_forge = st.checkbox(
        "Cross-forge", value=False,
        help="Follow the person's own profile links to their accounts on other "
             "forges and merge into one record. Rarely needed.")
    discover_roles = (st.checkbox(
        "LLM founder/role discovery", value=False,
        help="Use an LLM to infer founders/roles in hard cases. Slower, and "
             "spends the deployment's shared LLM budget.")
        if _LLM_ENABLED else False)
    # GitHub sign-in is itself an option — scan on your own rate limit instead of
    # the shared demo one. Rendered here as the last Options item (no-op unless
    # the OAuth app is configured). The divider only shows when there's a control.
    if "GITHUB_OAUTH_CLIENT_ID" in st.secrets:
        st.divider()
    USER_LOGIN, USER_TOKEN = github_account()
    _render_budget_note(USER_TOKEN)
forge_url = ""       # self-hosted instance URL is a CLI/library feature
wikidata = True      # always on — a cheap, broadly-useful role source

# Recent scans in the sidebar too (below Options) — a quick picker + debugging aid.
recent = st.session_state["recent"]
if recent:
    with st.sidebar:
        st.selectbox(
            "Recent scans",
            ["—", *(f"{r['forge']} · {r['username']}" for r in recent)],
            key="recent_pick", on_change=_pick_recent,
            help="Accounts scanned recently (this session + the shared cache). "
                 "Pick one to pre-fill the form — quick to re-open and handy for "
                 "debugging.")

# The main form is just the hero: username + Praise. A scan runs only on submit.
with st.form("q"):
    _label = (f"{_FORGE_LABEL.get(forge, forge)} username"
              + (" or person's name" if forge == "github" else ""))
    _ph = ("e.g. torvalds — or a full name to look up"
           if forge == "github" else "e.g. torvalds")
    username = st.text_input(_label, key="uname", placeholder=_ph,
                             help="A username to scan. On GitHub you can also type "
                                  "a full name and pick the right account.")
    submitted = st.form_submit_button(
        "🌟 Praise", type="primary", use_container_width=True)

# The View dropdown consolidates every output mode — cards, full report,
# copy-paste text, file export — into one control.
_VIEWS = ["Highlights", "Markdown report", "Copy as text", "Export files"]
# Log-spaced min-stars steps (GitHub stars span 0 → ~500k).
_MIN_STAR_STEPS = [0, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000,
                   25000, 50000, 100000, 250000, 500000]
# Display controls live in this placeholder and are rendered ONLY when results are
# shown (see _render_controls, called from the render block) — so they stay hidden
# on the empty landing screen and while a scan runs (pointless without results).
# Defaults keep view/highlights/min_stars bound until then.
controls_box = st.empty()
view, highlights, min_stars = "Highlights", 8, 50


def _render_controls():
    """Render the display controls into their placeholder and bind the module-level
    view/highlights/min_stars the renderers read. Called only when results exist."""
    global view, highlights, min_stars
    with controls_box.container():
        dc1, dc2, dc3 = st.columns([1.5, 1, 1])
        view = dc1.selectbox(
            "View", _VIEWS, key="view_sel",
            help="Highlights = ranked cards; Markdown report = full report with "
                 "every evidence link; Copy as text = plain-text summary to paste; "
                 "Export files = download the report as Markdown or JSON.")
        highlights = dc2.slider("Top N", 3, 100, 8, key="topn")
        # Log-spaced steps: stars span 0 → ~500k, so a linear slider wastes its
        # whole range on tiny values. select_slider gives a logarithmic feel.
        min_stars = dc3.select_slider(
            "Min stars", options=_MIN_STAR_STEPS, value=50, key="minstars",
            format_func=lambda n: (f"{n / 1000:g}k" if n >= 1000 else str(n)))


# Role → badge color, tuned toward the OpenTeams brand family (Streamlit only
# allows NAMED colors, so these approximate the brand): "primary" = the theme's
# brand blue (#4D75FE), plus blue, orange (≈ gold), red (≈ coral), gray — the
# off-brand violet/green are avoided. Assignments keep the roles that commonly
# co-occur on one project visually distinct (e.g. scipy: core/code_owner/release).
_ROLE_BADGE_COLOR = {
    "author": "primary",          # brand blue — praiser's signature role
    "steering_council": "red",    # coral-ish — governance
    "maintainer": "blue",
    "standards_author": "orange",  # gold-ish
    "code_owner": "red",          # coral-ish (rarely co-occurs with steering)
    "release_manager": "orange",  # gold-ish
    "core_contributor": "gray",   # neutral — the ubiquitous role
}
# A hoverable ⓘ after the badges points to the glossary in "About praiser".
_ROLE_HINT = ('&nbsp;<span title="See “About praiser” (top of page) for what each '
              'role means" style="cursor:help;opacity:0.5">ⓘ</span>')


def _role_badges(rec) -> str:
    """A row of inline colored badges for a record's roles, each carrying its
    rank/qualifier suffix (reuses render.role_display so labels never diverge)."""
    return " ".join(
        f":{_ROLE_BADGE_COLOR.get(role, 'gray')}-badge[{role_display(rec, role)}]"
        for role in rec.roles)


def _show_highlights(result, uname, controls_shown=True):
    """The default view: summary metrics + one compact line per top project."""
    primary, secondary = service.filtered_records(result, min_stars=min_stars)
    top = primary[:max(1, highlights)]
    if not top:
        # Only suggest lowering Min stars when that control is actually shown AND
        # the floor is above 0 (else the hint is pointless — e.g. partial results
        # have no controls, and a scan can simply find no elevated roles).
        floor_hides_some = min_stars > 0 and bool(result.records or result.secondary)
        if controls_shown and floor_hides_some:
            st.info("No elevated roles at this popularity threshold — lower **Min "
                    "stars** in the controls above to see more.")
        else:
            st.info(f"No elevated roles found for **{uname}**.")
        return
    allrecs = [*primary, *secondary]
    communities = {
        o for r in allrecs
        if (o := r.name_with_owner.split("/", 1)[0]).lower() != uname.lower()}
    commits = sum(r.contributions or 0 for r in allrecs)
    # Broad → narrow: communities (orgs) contain projects, which contain commits.
    m1, m2, m3 = st.columns(3)
    m1.metric("Communities", len(communities),
              help="Distinct organisations (owners other than the person).")
    m2.metric("Projects", len(allrecs))
    m3.metric("Total commits", f"{commits:,}" if commits else "—",
              help="All-time commits summed across these projects, where "
                   "measurable from the contributor data.")
    if commits:
        st.caption("_Total commits is a rough scale, not effort — "
                   "not comparable between people._")
    # Compact one-line entries: repo · stars · role badges — so many projects fit
    # above the fold and the feedback controls below stay reachable.
    for r in top:
        st.markdown(
            f"**[{r.name_with_owner}]({r.url})** &nbsp;·&nbsp; "
            f"{human_stars(r.stars)}★ &nbsp;·&nbsp; {_role_badges(r)}{_ROLE_HINT}",
            unsafe_allow_html=True)
    bits = []
    if (extra := len(primary) - len(top)) > 0:
        bits.append(f"{extra} more elevated-role project(s)")
    if secondary:
        bits.append(f"{len(secondary)} smaller but widely-used project(s) "
                    "with a notable role")
    if bits:
        st.caption("…plus " + "; ".join(bits) + ".")


def _export_view(result, uname):
    md = service.render_result(result, uname, view="markdown",
                               highlights=highlights, min_stars=min_stars)
    js = service.render_result(result, uname, view="json",
                               highlights=highlights, min_stars=min_stars)
    e1, e2 = st.columns(2)
    e1.download_button("⬇ Markdown", md, file_name=f"praiser-{uname}.md",
                       mime="text/markdown", use_container_width=True)
    e2.download_button("⬇ JSON", js, file_name=f"praiser-{uname}.json",
                       mime="application/json", use_container_width=True)


def _show(result, uname, controls_shown=True):
    """Render a RunResult in the selected View. ``controls_shown`` tells the empty
    state whether the Min-stars control is on screen (it isn't for partial results)."""
    if view == "Markdown report":
        st.markdown(service.render_result(result, uname, view="markdown",
                                          highlights=highlights,
                                          min_stars=min_stars))
    elif view == "Copy as text":
        primary, secondary = service.filtered_records(result, min_stars=min_stars)
        st.code(render_highlights(uname, primary, highlights, secondary,
                                  link_repos=False), language=None)
    elif view == "Export files":
        _export_view(result, uname)
    else:
        _show_highlights(result, uname, controls_shown=controls_shown)


def _feedback_buttons(result, uname, forge, data_opts):
    """One-click 'open a pre-filled praiser issue' buttons under a result, so a
    user who spots a wrong/missing role (or a bug/idea) can report it with the
    exact scan context attached. The highlights view is embedded regardless of the
    displayed view — it's compact and the most useful context for accuracy reports."""
    context = service.render_result(result, uname, view="highlights",
                                    highlights=highlights, min_stars=min_stars)
    links = service.feedback_links(uname, forge=forge, version=PRAISER_VERSION,
                                   result_text=context, data_opts=data_opts,
                                   reporter=USER_LOGIN)
    st.caption("Spotted a wrong or missing role, a bug, or have an idea? "
               "Open a pre-filled issue (you can review it before submitting):")
    for col, ln in zip(st.columns(len(links)), links):
        col.link_button(ln["label"], ln["url"], use_container_width=True)
    # GitHub requires an account to create an issue. Only surface that caveat to
    # users who aren't signed in via the app — for signed-in users it's just noise
    # (praiser only pre-fills the form; GitHub authors the issue under their login).
    if not USER_LOGIN:
        st.caption("_Submitting requires a GitHub account — the buttons open a "
                   "pre-filled issue you post under your own GitHub login._")


def _run_scan(username, data_opts, token_options, exhausted, status_ph, hint="",
              reset_at=0):
    """Scan in a worker thread (live progress bar on the main thread), trying the
    token options in order and falling back on rate limits (signed-in user's
    quota first, shared bot behind it). Returns (result, elapsed, label): result
    may be complete, partial (with .partial_reset_in), or None (all limited).
    The worker never touches st.* — it only mutates the plain `state`/`exhausted`.
    All progress/terminal output renders into ``status_ph`` (a placeholder) so the
    caller can keep the results area cleared while a scan runs. ``reset_at`` (an
    epoch) drives a "budget resets in ~Nm" countdown in the progress caption."""
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
    while not state["done"]:
        m = _SCANNED_RE.search(state["msg"])
        with status_ph.container():
            st.progress(min(1.0, int(m.group(1)) / max(1, int(m.group(2))))
                        if m else 0.0)
            cap = f"⏳ {state['msg']}"
            if reset_at:
                cap += (f" · GitHub budget resets in "
                        f"{humanize_wait(max(0, reset_at - int(time.time())))}")
            st.caption(cap)
            if hint:
                st.caption(hint)
        time.sleep(0.2)
    worker.join()
    status_ph.empty()
    if state["error"]:
        status_ph.error(f"Failed: {state['error']}")
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
        status_ph.warning(msg)
        st.stop()
    return state["result"], time.time() - started, state["label"]


# Size-bounded LRU across ALL scans this session, so revisiting an earlier
# user/options is instant (not just tweaking N of the latest scan).
if "results" not in st.session_state:
    st.session_state["results"] = SizeBoundedLRU(_CACHE_MB * 1024 * 1024)
cache = st.session_state["results"]

# Placeholders at fixed positions (status above results). Creating them BEFORE the
# scan lets a new scan CLEAR the previous results immediately — otherwise Streamlit
# shows the stale results greyed-out for the whole (long) scan.
status_box = st.empty()
results_box = st.empty()

def _pick_candidate(login):
    """A name-search candidate was chosen — scan that login next run."""
    st.session_state["scan_login"] = login.lower()
    st.session_state.pop("name_candidates", None)


# Decide what to scan this run: a candidate the user just picked, or a fresh
# submit. A submit whose input looks like a NAME (has a space — usernames can't)
# is resolved to a login first; ambiguous names show a picker instead of scanning.
scan_target = st.session_state.pop("scan_login", None)   # from a candidate pick
_resolved_from = None   # the name a single-match resolved from (for transparency)
if scan_target is None and submitted:
    raw = username.strip()
    if not raw:
        status_box.warning("Enter a username.")
        st.stop()
    if service.looks_like_name(raw):
        st.session_state.pop("name_candidates", None)
        if forge != "github":
            status_box.warning(
                f"Looking someone up by name is GitHub-only for now. Enter the "
                f"exact **{forge}** username, or switch Forge to GitHub.")
            st.stop()
        try:
            cands = service.search_people(raw, forge=forge, token=USER_TOKEN)
        except RateLimitError as exc:
            wait = humanize_wait(exc.reset_in) if exc.reset_in else "a little while"
            status_box.warning(
                f"⏳ GitHub's search rate limit was reached — the name lookup "
                f"couldn't run. Try again in {wait}"
                + ("" if USER_TOKEN else ", or sign in with GitHub (sidebar) to use "
                   "your own limit")
                + ". Or enter the person's exact username to scan directly.")
            st.stop()
        if not cands:
            status_box.warning(
                f"No GitHub account found for **{raw}**. Try the person's exact "
                "username, add more of their name, or find their handle on their "
                "GitHub profile, personal site, or Wikidata.")
            st.stop()
        if len(cands) == 1 and service.name_matches(raw, cands[0].name):
            # A single hit whose profile name really matches → safe to auto-scan.
            scan_target = cands[0].login.lower()
            _resolved_from = raw     # surfaced in the finished-scan message
        else:
            # Ambiguous, or a single loose hit (GitHub ranks a wrong person first
            # when the real name isn't in their profile) — let the user confirm
            # rather than guessing (issue #142). Don't st.stop(): the picker renders.
            st.session_state["name_candidates"] = (
                raw, [(c.login, c.name, c.bio) for c in cands])
    else:
        # A handle: forge usernames are case-insensitive, so canonicalize to lower
        # case — else "Pearu" (phone autocapitalization) and "pearu" would be two
        # scans / cache entries / "Recent scans" items for the same person.
        scan_target = raw.lower()

# A scan runs ONLY with a resolved target. It stores the result and marks it
# "active"; the render block below then shows it with the CURRENT view/N — so a
# later display-control change reruns and re-renders instantly (no re-scan).
if scan_target is not None:
    # Drop the previous result up front: it's re-set only on success/cache below,
    # so a scan that fails (rate limit) leaves no stale result from another user
    # to be shown (and no display controls) on a later rerun.
    st.session_state.pop("active", None)
    uname = scan_target
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
        status_box.success("✅ Showing cached results — change the username, forge, "
                           "or a scan option to re-scan (or tick Refresh to force "
                           "one).")
    else:
        results_box.empty()   # clear stale results while the new scan runs
        # A "this may take a while" hint, shown under the progress bar during the
        # scan (in status_box) and gone once it finishes.
        hint = (
            "🔄 Refreshing — re-scanning the repos you're connected to; "
            "org-membership repos ride the cache."
            if refresh else
            "⏳ A first-time scan can take ~30 seconds to a few minutes — praiser "
            "queries the forge across many repositories (longer with cross-forge "
            "or LLM discovery on).")
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
        result, elapsed, label = _run_scan(
            uname, data_opts, token_options, exhausted, status_box, hint,
            reset_at=_budget_reset_at())
        # The scan consumed quota — drop the cached budget so the sidebar note
        # re-fetches the now-lower figure on the next render.
        st.session_state.pop("rate_budget", None)
        if result.partial_reset_in is not None:
            # Every token hit its limit mid-scan → incomplete. Don't cache it;
            # show what we have with a clear warning + wait time.
            status_box.warning(
                "⚠️ Partial results — the API rate limit was reached mid-scan on "
                "all available tokens, so some projects may be missing. Re-scan in "
                f"{humanize_wait(result.partial_reset_in)} for the full record"
                + ("" if USER_TOKEN else " (or sign in with GitHub for your own limit)")
                + f". Or run praiser locally (`pip install praiser`; see {REPO_URL})."
            )
            # Partial isn't cached (so it can't become "active"); show it once with
            # default view/N and no controls (they'd break on the next rerun).
            with results_box.container():
                _show(result, uname, controls_shown=False)
            st.stop()
        cache.put(key, result)
        _via = f" (via {label})" if label and USER_TOKEN else ""
        _from = f" — resolved “{_resolved_from}” → @{uname}" if _resolved_from else ""
        status_box.success(
            f"✅ Scan finished in {elapsed:.1f} seconds{_via}{_from}.")
        # Reflect this scan in the recent-picker immediately (shared index is
        # updated inside service.collect()).
        entry = {"forge": forge, "username": uname}
        st.session_state["recent"] = [entry] + [
            r for r in st.session_state.get("recent", []) if r != entry][:49]
    st.session_state["active"] = (key, uname, forge, data_opts)

# Ambiguous name → a pick-one list (issue #142) in place of results, so we never
# silently scan the wrong person.
pending = st.session_state.get("name_candidates")
if pending is not None:
    raw_name, cands = pending
    with results_box.container():
        st.markdown(f"**GitHub accounts matching “{raw_name}” — "
                    "pick the right person:**")
        for login, name, bio in cands:
            label = f"@{login}" + (f" — {name}" if name else "")
            st.button(label, key=f"cand_{login}", on_click=_pick_candidate,
                      args=(login,), use_container_width=True)
            st.caption((bio + "  ·  " if bio else "")
                       + f"[github.com/{login}](https://github.com/{login})")
        st.caption("_Not the right person? A GitHub profile name often differs "
                   "from someone's real name, so search may miss them — enter their "
                   "exact username above (find it via a web search or their site)._")
# Otherwise render the active result (from a fresh submit OR a display-only
# rerun) into the results placeholder, so a subsequent scan can clear it cleanly.
else:
    active = st.session_state.get("active")
    if active is not None:
        key, uname, a_forge, a_opts = active
        result = cache.get(key)
        if result is None:  # evicted from the LRU — ask for a re-scan
            status_box.info("Previous results expired — click Praise to scan again.")
        else:
            _render_controls()   # controls appear only now that results are shown
            with results_box.container():
                _show(result, uname)
                _feedback_buttons(result, uname, a_forge, a_opts)

# --- External data-source diagnostics (?diag) ---------------------------------
# Open `praiser.streamlit.app/?diag` to see, FROM THIS HOST, whether the external
# data sources praiser depends on are reachable. Founder/creator roles come from
# Wikidata → Wikipedia, which throttle shared cloud IPs (Streamlit Community
# Cloud) harder than local ones — so this makes a "missing role" visibly a
# reachability problem rather than a guess. Read-only, safe to expose.
if "diag" in st.query_params:
    st.subheader("🩺 External data-source reachability")
    diag = service.diagnose_external_sources()
    st.caption(f"Probed from this host via praiser's own client (UA "
               f"`{diag['user_agent']}`). Wikidata/Wikipedia feed the Author/"
               "founder roles and throttle cloud IPs; GitHub is the baseline. "
               "❌ on Wikidata/Wikipedia here means founder roles are computed "
               "from cache only until reachability recovers.")
    for c in diag["checks"]:
        st.write(f"{'✅' if c['ok'] else '❌'} **{c['name']}** — {c['detail']}")


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
    # The form lives in a placeholder so it can be CLEARED while seeding runs —
    # otherwise Streamlit shows the form greyed-out for the whole (long) operation.
    seed_form = st.empty()
    seed_status = st.empty()
    with seed_form.container():
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
            go = st.button("Seed", key="seed_go")
    if go:
        if not a_name.strip():
            seed_status.warning("Enter an org or owner/repo.")
        else:
            seed_form.empty()   # hide the form while seeding runs
            with seed_status.container():
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
                        f"{res['stopped']}. Re-run to continue (resumes where it "
                        "left off).")

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
    "PRAISER_ADMIN_USERS",  # comma-separated GitHub logins that get the admin frame
)
for _k in _SECRET_KEYS:
    if _k in st.secrets and not os.environ.get(_k):
        os.environ[_k] = str(st.secrets[_k])

# GitHub logins allowed into the admin frame. Admin is only reachable when the
# OAuth app is configured AND the signed-in user is listed here (the login comes
# from the OAuth token, so it's not spoofable).
_ADMIN_USERS = {u.strip().lower() for u in
                os.environ.get("PRAISER_ADMIN_USERS", "").split(",") if u.strip()}


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


def _public_stats():
    """Non-identifying usage totals for the public line, read once per session
    (a couple of Redis reads), refreshed after a scan (see the scan-complete path)."""
    if "pub_stats" not in st.session_state:
        st.session_state["pub_stats"] = service.public_stats()
    return st.session_state["pub_stats"]


def _render_public_stats(slot):
    """Paint the discreet usage line into ``slot`` (a placeholder), so it can be
    repainted in place after a scan without waiting for the next rerun. Hidden
    until there's something to show."""
    ps = _public_stats()
    if ps.get("people"):
        line = (f"👤 {ps['people']:,} people scanned · "
                f"📦 {ps['repos']:,} elevated-role projects")
        line += (f" across 🏢 {ps['orgs']:,} organizations"
                 if ps.get("orgs") else " covered")
        slot.caption(line)
    else:
        slot.empty()


def _bg_seed_once():
    """Run the background seeder in its own thread (touches only Redis + GitHub,
    never st.*): chains through the pending orgs while quota is healthy. Triggered
    explicitly by the admin "Save & seed now" button — not on visitor traffic —
    and still self-limited by the Redis lease + REST watermark."""
    try:
        from web import seed as webseed
        webseed.run_queue()
    except Exception:
        pass


def _ratio(rem, lim):
    return rem / lim if lim else 1.0


def _render_budget_note(slot, token):
    """Render a remaining-GitHub-quota note into ``slot`` (a sidebar placeholder):
    a snapshot warning when low, else a caption, with the tightest bucket's reset
    time. It's a point-in-time reading (updates on interaction and after each scan,
    and is CLEARED while a scan runs — the live figure is then in the progress bar);
    the reset epoch that's rolled over triggers one corrective re-fetch."""
    b = _rate_budget(token)
    present = [(lbl, *b[res]) for res, lbl in _BUDGET_BUCKETS if res in b]
    if not present:
        slot.empty()
        return
    now = int(time.time())
    lbl, rem, lim, reset = min(present, key=lambda p: _ratio(p[1], p[2]))
    if reset and now >= reset:
        # The tightest bucket's window rolled over → cached figures are stale; one
        # corrective query for fresh numbers.
        st.session_state.pop("rate_budget", None)
        b = _rate_budget(token)
        present = [(x, *b[r]) for r, x in _BUDGET_BUCKETS if r in b]
        if not present:
            slot.empty()
            return
        now = int(time.time())
        lbl, rem, lim, reset = min(present, key=lambda p: _ratio(p[1], p[2]))
    signed_in = bool(token)
    summary = " · ".join(f"{x} {rm}/{lm}" for x, rm, lm, _ in present)
    resets = f", resets in {humanize_wait(max(0, reset - now))}" if reset else ""
    who = "Your GitHub budget" if signed_in else "Shared demo budget"
    if lim and rem < 0.15 * lim:
        slot.warning(f"⚠️ {who} running low: {summary}{resets}."
                     + ("" if signed_in else " Sign in above to scan on your own limit."))
    else:
        slot.caption(f"{who}: {summary}{resets}."
                     + ("" if signed_in else " A few scans can use it up — sign in "
                        "above for your own limit."))


st.set_page_config(page_title="praiser", page_icon="🌟")
st.title("🌟 praiser")
st.caption("Find the open-source projects where someone holds an elevated role — "
           "with evidence for every claim.")
# Discreet usage line (approximate, monotonic; hidden until there's something to
# show). In a placeholder so a completed scan can repaint it in place (below).
stats_slot = st.empty()
_render_public_stats(stats_slot)
# One "About praiser" dropdown at the top holds both the intro and the role
# glossary — so the meaning of every role is one obvious place to look (the result
# cards point here). Kept out of the main flow so the landing screen stays lean.
with st.expander("About praiser & role definitions"):
    st.markdown(
        "**praiser** records the projects where a person is an **author, "
        "maintainer, code owner, steering-council member, standards author, "
        "release manager, or core contributor** — each with a clickable "
        "**evidence link** and a **confidence** score. **GitHub is the primary, "
        "best-supported target**; it also scans GitLab, Codeberg, Gitee, Bitbucket "
        "and cgit, though non-GitHub support is less complete (thinner discovery, "
        "and ranking on hosts without a stars metric) and still improving.\n\n"
        f"ℹ️ More: [{REPO_URL.split('//', 1)[1]}]({REPO_URL}) · "
        f"praiser v{PRAISER_VERSION}")
    st.markdown("---")
    st.markdown("#### What do these roles mean?")
    st.markdown(render_role_glossary())

# Forges usable from just a username (cgit needs an instance URL + --add-repo,
# which this demo doesn't expose; the core library still supports it via CLI).
DEMO_FORGES = [f for f in service.FORGES if f != "cgit"]

# An LLM key must be configured for founder/role discovery to do anything; the
# option is admin-only regardless (it spends the deployment's shared LLM budget).
_LLM_KEY = bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

# Options live in the sidebar (not the main form): they're rarely changed and have
# good defaults, so the main area stays a clean username + Praise. They're plain
# widgets read at submit time — toggling one just reruns (re-rendering the cached
# result); a scan still runs only on the Praise button. Sign-in renders FIRST so
# the admin-only options below can gate on it. Advanced/resource-spending knobs
# (refresh, cross-forge, LLM) are admin-only; regular users get sensible defaults.
with st.sidebar:
    st.markdown("### Options")
    # GitHub sign-in — scan on your own rate limit instead of the shared demo one
    # (no-op unless the OAuth app is configured). Also establishes admin status.
    USER_LOGIN, USER_TOKEN = github_account()
    IS_ADMIN = bool(USER_LOGIN) and USER_LOGIN.lower() in _ADMIN_USERS
    if "GITHUB_OAUTH_CLIENT_ID" in st.secrets:
        st.divider()
    forge = st.selectbox("Forge", DEMO_FORGES, key="forge_sel",
                         help="GitHub by default; switch to scan another host.")
    package_registries = st.checkbox(
        "Package registries", value=True,
        help="Also check PyPI / npm / crates.io: credit the person as author "
             "(PyPI) or maintainer (npm/crates) of a package — but only when "
             "the package's metadata links back to a repo praiser found.")
    # Refresh is for signed-in users only: they scan on their OWN GitHub quota, so
    # a forced re-fetch doesn't burn the shared demo budget. (Admins are signed in,
    # so they get it too.)
    if USER_TOKEN:
        refresh = st.checkbox(
            "Refresh (ignore cache)", value=False,
            help="Force a fresh re-scan instead of a cached result (runs on your "
                 "own GitHub quota). Only repos you're actually connected to are "
                 "re-fetched.")
    else:
        refresh = False
    # Admin-only knobs: shared LLM budget / advanced, rarely-needed multi-forge.
    if IS_ADMIN:
        cross_forge = st.checkbox(
            "Cross-forge", value=False,
            help="Follow the person's own profile links to their accounts on "
                 "other forges and merge into one record. Rarely needed.")
        discover_roles = (st.checkbox(
            "LLM founder/role discovery", value=False,
            help="Use an LLM to infer founders/roles in hard cases. Slower, and "
                 "spends the deployment's shared LLM budget.")
            if _LLM_KEY else False)
    else:
        cross_forge = discover_roles = False
    budget_slot = st.empty()            # a scan clears this, then repaints it
_render_budget_note(budget_slot, USER_TOKEN)
forge_url = ""       # self-hosted instance URL is a CLI/library feature
wikidata = True      # always on — a cheap, broadly-useful role source

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
            format_func=lambda n: (f"{n / 1000:g}k" if n >= 1000 else str(n)),
            help="Adoption bar (stars = how widely used/valued). Projects below "
                 "it but with real developer engagement (forks) + maintenance "
                 "still appear as a secondary group.")


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
# A hoverable ⓘ after the badges points to the glossary in the About expander.
_ROLE_HINT = ('&nbsp;<span title="See “About praiser & role definitions” (top of '
              'page)" style="cursor:help;opacity:0.5">ⓘ</span>')


def _role_badges(rec) -> str:
    """A row of inline colored badges for a record's roles, each carrying its
    rank/qualifier suffix (reuses render.role_display so labels never diverge)."""
    return " ".join(
        f":{_ROLE_BADGE_COLOR.get(role, 'gray')}-badge[{role_display(rec, role)}]"
        for role in rec.roles)


def _below_bar_note(result, uname):
    """A reassurance line when praiser saw contributions that earned no elevated
    role — so a short/empty result reads as 'seen, below the bar', not a bug
    (#172). '' when there are none."""
    n = getattr(result, "below_bar_count", 0) or 0
    if not n:
        return ""
    return (f"praiser also saw **{n}** other project(s) {uname} contributed to "
            "where the number of commits attributed to them is below praiser's "
            "threshold for an elevated role, so none is listed — this is a commit-"
            "count threshold, not a judgment of the work (attributed commits can "
            "undercount real contributions).")


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
        if (note := _below_bar_note(result, uname)):
            st.caption(note)
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
                   "measurable from the contributor data. A commit's real value "
                   "varies enormously (a rewrite vs a typo fix), so this is a "
                   "rough scale only — never a way to compare people.")
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
    if (note := _below_bar_note(result, uname)):
        st.caption(note)


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


def _trash_cache_entry(cache_id, username, forge="github"):
    """Admin Trash callback: drop a user's shared cached result so the next scan
    of that user is fresh (doesn't touch other users' cache)."""
    ok = service.trash_cache_entry(cache_id)
    # Also evict this user from THIS admin's in-session LRU — otherwise the shared
    # entry is gone but re-typing the name serves the stale in-memory copy (the
    # session cache is checked before collect(), so no re-scan would fire).
    lru = st.session_state.get("results")
    if lru is not None:
        lru.discard_where(
            lambda k: isinstance(k, tuple) and k
            and str(k[0]).lower() == str(username).lower()
            and (len(k) < 2 or k[1] == forge))
    st.session_state["admin_flash"] = (
        f"🗑 Cleared cached scan for {username} — the next scan will be fresh."
        if ok else f"Couldn't clear the cached scan for {username}.")


def _clear_session_lru():
    """Wipe this admin's in-session result cache so bulk clears actually re-scan."""
    lru = st.session_state.get("results")
    if lru is not None:
        lru.clear()


def _clear_tracked_scans():
    n = service.clear_tracked_scans()
    _clear_session_lru()
    st.session_state["admin_confirm"] = False
    st.session_state["admin_flash"] = (
        f"Cleared {n} tracked cached scan(s) — each user's next scan re-fetches live; "
        "founder cache + reverse-index kept.")


def _wipe_all_cache():
    n = service.wipe_all_cache()
    _clear_session_lru()
    st.session_state["admin_confirm"] = False
    st.session_state["admin_flash"] = (
        f"Wiped {n} cache key(s) — clean slate (founder cache + reverse-index too); "
        "previously-scanned users re-fetch live on their next scan. Usage stats kept.")


def _save_seed_targets():
    saved = service.set_seed_targets(st.session_state.get("seed_targets_text", ""))
    service.set_seed_budget(st.session_state.get("seed_budget_bg", service.SEED_CHUNK_BUDGET))
    st.session_state["seed_msg"] = ("ok",
        f"Saved {len(saved)} org(s). Click “Save & seed now” to start seeding "
        "them (it chains through while GitHub quota is healthy).")


def _reset_usage_stats():
    n = service.reset_usage_stats()
    st.session_state["admin_confirm"] = False
    st.session_state.pop("pub_stats", None)      # public line re-reads (now 0)
    st.session_state["admin_flash"] = (
        f"Reset usage stats ({n} key(s)) — people/projects/organizations counts "
        "start fresh. (History is not recoverable.)")


def _render_admin_frame():
    """The unified admin/debug frame (end of page, admins only), gated by
    _ADMIN_USERS + a non-spoofable GitHub sign-in: cache summary, external-source
    diagnostics, cached-scan list (per-row Trash), reverse-index seeding + status,
    and the cache danger zone."""
    st.divider()
    st.subheader("🔧 Admin")
    st.caption(f"Signed in as @{USER_LOGIN} · admin.")
    if (flash := st.session_state.pop("admin_flash", None)):
        st.success(flash)
    _render_admin_summary()
    _render_admin_diag()
    st.markdown("**Cached scans** — Trash an entry to force a fresh scan on the "
                "shared cache (affects only that user).")
    rows = service.cache_catalog()
    if not rows:
        st.caption("No cached scans recorded yet.")
    else:
        now = time.time()
        for r in rows:
            c1, c2, c3 = st.columns([3, 2, 1], vertical_alignment="center")
            c1.markdown(f"{r['forge']} · **{r['username']}**")
            age = humanize_wait(int(now - r["created"])) if r.get("created") else "?"
            c2.caption(f"updated {age} ago")
            c3.button("🗑 Trash", key=f"trash_{r['cache_id']}",
                      on_click=_trash_cache_entry,
                      args=(r["cache_id"], r["username"], r["forge"]),
                      use_container_width=True)
    _render_admin_seed()
    _render_admin_danger_zone()


def _render_admin_diag():
    """External data-source reachability, probed from THIS host (was public ?diag).
    Founder/creator roles come from Wikidata → Wikipedia, which throttle shared
    cloud IPs harder than local ones, so a ❌ makes a 'missing role' visibly a
    reachability problem rather than a guess."""
    with st.expander("🩺 External data-source reachability"):
        diag = service.diagnose_external_sources()
        st.caption(f"Probed from this host via praiser's own client (UA "
                   f"`{diag['user_agent']}`). Wikidata/Wikipedia feed the Author/"
                   "founder roles and throttle cloud IPs; GitHub is the baseline. "
                   "❌ on Wikidata/Wikipedia means founder roles are computed from "
                   "cache only until reachability recovers.")
        for c in diag["checks"]:
            st.write(f"{'✅' if c['ok'] else '❌'} **{c['name']}** — {c['detail']}")


def _render_admin_seed():
    """Seed the shared contributor reverse-index (#65) + show what's been seeded.
    Admin-login-gated (replaces the old ?seed + SEED_ENABLED gate). Seeding is
    bounded, idempotent (30-day per-repo markers), additive, and spends the
    deployment's bot quota."""
    from web import seed as webseed
    st.markdown("**Reverse-index seeding** — index an org's (or a repo's) "
                "contributors so the app can discover them.")
    if (m := st.session_state.pop("seed_msg", None)):
        (st.success if m[0] == "ok" else st.error)(m[1])
    # Background seeding: an admin-managed org list, seeded on demand via
    # "Save & seed now" (a background thread; no cron, no traffic-triggering).
    with st.expander("🌱 Background seeding (org list)"):
        st.caption(
            "One org per line. Click “Save & seed now” to seed them: it chains "
            f"through the list in the background, one org at a time, while GitHub "
            f"REST quota is above {service.SEED_REST_START:,}, backing off below "
            f"{service.SEED_REST_FLOOR:,} (click again after quota recovers to "
            "resume). Re-seeds after 30 days. Spends the deployment's bot quota.")
        st.text_area("Orgs to seed (one per line)", key="seed_targets_text",
                     value="\n".join(service.get_seed_targets()), height=140,
                     placeholder="numpy\nscipy\npandas-dev\npytorch")
        st.number_input(
            "Repos per org, per chunk", 1, 500, value=service.get_seed_budget(),
            key="seed_budget_bg",
            help="How many repos to seed for an org each chunk. Orgs with more "
                 "repos fill in over repeated chunks (re-seeded oldest-first).")
        seed_now = st.button(
            "Save & seed now", use_container_width=True,
            help="Save the list/budget and start background seeding now. It chains "
                 "through the pending orgs while GitHub quota is healthy, then backs "
                 "off — click again once quota recovers to resume. For a specific "
                 "one-off, use “Seed a target” below.")
        active = service.seeder_status()
        status = service.seed_targets_status()
        now = time.time()
        done = sum(1 for s in status if s["seeded"])
        total = len(status)
        if active and active.get("started"):
            ago = humanize_wait(int(now - active["started"]))
            if active.get("org"):
                st.caption(f"🔄 **Running** — seeding **{active['org']}** ({active.get('done', 0)} "
                           f"org(s) done this run) · started {ago} ago. (Refresh to update.)")
            else:
                st.caption(f"🔄 **Running** — a seeder is active (started {ago} ago).")
        elif total:
            last = service.last_seed_run()
            if last and last.get("finished"):
                fin = humanize_wait(int(now - last["finished"]))
                reason = last.get("reason", "")
                if reason == "all due targets seeded":
                    st.caption(f"✅ **Idle — up to date.** Last run covered the whole "
                               f"list ({done}/{total} seeded) {fin} ago; no seeder running.")
                elif reason.startswith("REST"):
                    st.caption(f"⏸️ **Idle — paused (rate limit).** Last run stopped at "
                               f"{reason} {fin} ago; {done}/{total} seeded. Click “Save "
                               "& seed now” once quota recovers to resume.")
                else:
                    st.caption(f"⏹️ **Idle.** Last run: {reason} {fin} ago; "
                               f"{done}/{total} seeded.")
            else:
                st.caption(f"⏹️ **Idle** — {done}/{total} seeded; no run recorded yet.")
        if status:
            for s in status:
                if s["seeded"]:
                    age = humanize_wait(int(now - s["updated"])) if s["updated"] else "?"
                    st.caption(f"✅ **{s['org']}** — {s['repos']} repo(s), "
                               f"{s['contributors']} contributor(s) · {age} ago")
                else:
                    st.caption(f"⏳ **{s['org']}** — pending")
    if seed_now:
        _save_seed_targets()                          # persist current list + budget
        active = service.seeder_status()              # is a run already in progress?
        if active and active.get("started"):
            ago = humanize_wait(int(time.time() - active["started"]))
            where = f" (on {active['org']} now)" if active.get("org") else ""
            st.session_state["seed_msg"] = ("ok",
                f"Saved. A seeder is already running{where}, started {ago} ago — it "
                "reads the list fresh each org, so it'll pick up your changes. Watch "
                "the status below.")
        else:
            threading.Thread(target=_bg_seed_once, daemon=True).start()
            st.session_state["seed_msg"] = ("ok",
                "Saved — background seeding started. It chains through the pending "
                "orgs while GitHub quota stays healthy, then backs off. Refresh to "
                "watch the status below.")
        st.rerun()
    seeded = service.seed_catalog()
    if seeded:
        now = time.time()
        with st.expander(f"Seeded targets ({len(seeded)})"):
            st.caption("Cumulative distinct coverage per target (contributors with "
                       "≥15 commits — the discoverable ones). A re-run showing 0 "
                       "new means those repos are already indexed (not lost).")
            for r in seeded:
                ts = r.get("updated") or r.get("created")
                age = humanize_wait(int(now - ts)) if ts else "?"
                st.caption(
                    f"{r['forge']} · **{r['target']}** ({r['kind']}) — "
                    f"{r['repos']} repo(s), {r['contributors']} distinct "
                    f"contributor(s) · last run {age} ago")
    # The form lives in a placeholder so it can be CLEARED while seeding runs —
    # otherwise Streamlit greys it out for the whole (long) operation.
    seed_form = st.empty()
    seed_status = st.empty()
    with seed_form.container():
        with st.expander("🌱 Seed a target"):
            a_forge = st.selectbox("Forge", service.FORGES, key="seed_forge",
                                   help="Only GitHub is functional today.")
            a_kind = st.radio("Seed", ["org", "repo"], horizontal=True,
                              key="seed_kind",
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
                        st.session_state["seed_msg"] = ("err", f"Seed failed: {exc}")
                        res = None
                if res:
                    st.session_state["seed_msg"] = ("ok",
                        f"Seeded {res['seeded']} new repo(s) this run. Coverage now: "
                        f"{res.get('repos_distinct', 0)} repo(s), "
                        f"{res.get('contributors_distinct', 0)} distinct "
                        f"contributors — {res['stopped']}. Re-run to continue "
                        "(resumes where it left off).")
            # Re-render: refresh the seeded-targets list with the just-added row and
            # bring the (emptied) form back so an org seed can be continued without
            # re-login. The outcome shows via seed_msg above on this rerun.
            st.rerun()


def _render_admin_summary():
    """Cheap cache/usage stats (a few O(1) reads; nothing that runs per scan)."""
    s = service.usage_summary()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Scans (total)", s["scans_total"] if s["scans_total"] is not None else "—")
    m2.metric("Scans (today)", s["scans_today"] if s["scans_today"] is not None else "0")
    m3.metric("Scans (this hr)", s["scans_hour"] if s["scans_hour"] is not None else "0")
    m4.metric("Tracked scans", s["tracked_scans"])
    m5.metric("Cache keys", s["keys"] if s["keys"] is not None else "—")
    now = time.time()
    span = ""
    if s["newest"]:
        span = f" · newest {humanize_wait(int(now - s['newest']))} ago"
    if s["oldest"] and s["oldest"] != s["newest"]:
        span += f", oldest {humanize_wait(int(now - s['oldest']))} ago"
    rb = _rate_budget(USER_TOKEN)
    present = [(lbl, *rb[res]) for res, lbl in _BUDGET_BUCKETS if res in rb]
    rl = ""
    if present:
        rl = " · rate budget: " + ", ".join(
            f"{lbl} {rem}/{lim}" for lbl, rem, lim, _ in present)
    st.caption(
        f"'Scans' count actual data-collection runs (cache hits don't count); "
        f"'today'/'this hr' are UTC calendar buckets — a recent-load gauge. "
        f"'Cache keys' is the whole cache DB — results, per-repo founder cache and "
        f"the contributor reverse-index share one opaque namespace, so per-category "
        f"memory isn't broken out.{span}{rl}")


def _render_admin_danger_zone():
    """Global cache-reset controls, gated behind a confirm checkbox (destructive,
    affect ALL users): Clear tracked scans vs Wipe the whole namespace."""
    st.markdown("---")
    st.markdown("**Danger zone** — these affect every user, not just one.")
    confirm = st.checkbox("Yes, I'm sure (enables the buttons below)",
                          key="admin_confirm")
    b1, b2 = st.columns(2)
    b1.button("Clear cached scans", disabled=not confirm, on_click=_clear_tracked_scans,
              use_container_width=True,
              help="Trash every tracked user — each one's next scan re-fetches live "
                   "(same as per-row Trash, in bulk); keeps the founder cache + "
                   "reverse-index.")
    b2.button("Wipe ALL cache", disabled=not confirm, on_click=_wipe_all_cache,
              use_container_width=True, type="primary",
              help="Wipe the entire praiser cache namespace — a clean slate, "
                   "including the founder cache + reverse-index (rebuilt on next "
                   "scans; founder re-resolution is WDQS-throttled). Previously-"
                   "scanned users are re-marked to re-fetch live on next scan. "
                   "Usage stats are kept (reset them separately below).")
    st.button("Reset usage stats", disabled=not confirm, on_click=_reset_usage_stats,
              use_container_width=True,
              help="Zero the public usage counts (people / projects / organizations "
                   "scanned). Separate from the cache wipe because these are "
                   "monotonic metrics and can't be recovered once cleared.")


def _run_scan(username, data_opts, token_options, exhausted, status_ph, hint=""):
    """Scan in a worker thread (live progress bar on the main thread), trying the
    token options in order and falling back on rate limits (signed-in user's
    quota first, shared bot behind it). Returns (result, elapsed, label): result
    may be complete, partial (with .partial_reset_in), or None (all limited).
    The worker never touches st.* — it only mutates the plain `state`/`exhausted`.
    All progress/terminal output renders into ``status_ph`` (a placeholder) so the
    caller can keep the results area cleared while a scan runs. The pipeline's
    progress message already carries the live rate summary (REST/GraphQL/search)."""
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
            st.caption(f"⏳ {state['msg']}")
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
            # The GraphQL API budget is spent (shared across demo users); it refills
            # on GitHub's hourly window. Don't suggest scanning by handle — a scan
            # hits the same limit. Signing in gives an independent budget.
            status_box.warning(
                f"⏳ GitHub's API rate limit is exhausted, so the name lookup "
                f"couldn't run. It resets in {wait}"
                + (" — or sign in with GitHub (sidebar) to use your own limit"
                   if not USER_TOKEN else "")
                + ".")
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
        # Refresh is a signed-in-only control (own-quota), so only mention it then.
        _force = " (or tick Refresh to force one)" if USER_TOKEN else ""
        status_box.success("✅ Showing cached results — change the username, forge, "
                           f"or a scan option to re-scan{_force}.")
    else:
        results_box.empty()   # clear stale results while the new scan runs
        budget_slot.empty()   # the sidebar budget snapshot is stale during a scan;
                              # the live figure shows in the progress caption below
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
            uname, data_opts, token_options, exhausted, status_box, hint)
        # The scan consumed quota — drop the cached budget so the sidebar note
        # re-fetches the now-lower figure on the next render. Also refresh the
        # public usage line so it reflects this scan.
        st.session_state.pop("rate_budget", None)
        st.session_state.pop("pub_stats", None)
        _render_public_stats(stats_slot)   # repaint the usage line in place
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

# --- Admin frame (end of main page; admins only) ------------------------------
# Everything admin/debug lives here behind a (non-spoofable) GitHub-login gate:
# cache summary + list, external-source diagnostics, reverse-index seeding + status,
# and the cache danger zone. No ?diag / ?seed / ?debug URL gates any more.
if IS_ADMIN:
    _render_admin_frame()

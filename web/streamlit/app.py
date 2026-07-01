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

from web.core import service  # noqa: E402
from web.core.resultcache import SizeBoundedLRU  # noqa: E402

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


st.set_page_config(page_title="praiser", page_icon="🌟")
st.title("🌟 praiser")
st.caption("The open-source projects where a person holds an elevated role — "
           "author, maintainer, steering council, standards author — with "
           "evidence links, across GitHub, GitLab, Codeberg, Gitee, Bitbucket "
           "and cgit hosts.")
st.caption(f"ℹ️ More information: [{REPO_URL.split('//', 1)[1]}]({REPO_URL})")

# Data-collection controls live in a form: they only take effect on "Praise",
# so a scan runs only on an explicit submit.
# Forges usable from just a username (cgit needs an instance URL + --add-repo,
# which this demo doesn't expose; the core library still supports it via CLI).
DEMO_FORGES = [f for f in service.FORGES if f != "cgit"]

with st.form("q"):
    username = st.text_input("Username / login", placeholder="e.g. certik")
    forge = st.selectbox("Forge", DEMO_FORGES, index=0)
    forge_url = ""  # self-hosted instance URL is a CLI/library feature, not the demo
    c3, c4 = st.columns(2)
    wikidata = c3.checkbox("Wikidata roles", value=True)
    package_registries = c4.checkbox("Package registries", value=True)
    cross_forge = c3.checkbox("Cross-forge (follow profile links)", value=False)
    discover_roles = c4.checkbox("LLM founder/role discovery (slower, costs)",
                                 value=False)
    submitted = st.form_submit_button(
        "🌟 Praise", type="primary", use_container_width=True)

# Display controls live OUTSIDE the form: changing them reruns immediately and
# re-renders the already-collected result from cache — no button, no re-scan.
# (min_stars is a display filter — the scan collects the full superset.)
d1, d2, d3 = st.columns(3)
view = d1.selectbox("View", service.VIEWS, index=0)
highlights = d2.slider("Highlights (top N)", 3, 20, 8)
min_stars = d3.slider("Min stars", 0, 1000, 50, step=10)


def _run_scan(username, data_opts):
    """Scan in a worker thread while the main thread shows a live progress bar.
    Returns (RunResult, elapsed_seconds). The worker never touches st.*."""
    state = {"msg": "starting…", "result": None, "error": None, "done": False}

    def _work():
        try:
            state["result"] = service.collect(
                username, progress=lambda m: state.update(msg=m), **data_opts)
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
    return state["result"], time.time() - started


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
    }
    # Cache key excludes the display options (view/highlights) on purpose.
    key = (uname, *(data_opts[k] for k in service.DATA_OPTIONS))
    if cache.get(key) is not None:
        st.success("✅ Showing cached results — change the username, forge, or a "
                   "scan option to re-scan.")
    else:
        st.info(
            "⏳ A first-time scan can take ~30 seconds to a few minutes — praiser "
            "queries the forge across many repositories (longer with cross-forge "
            "or LLM discovery on). Changing the view, top-N or min-stars is instant."
        )
        result, elapsed = _run_scan(uname, data_opts)
        cache.put(key, result)
        st.success(f"✅ Scan finished in {elapsed:.1f} seconds.")
    st.session_state["active"] = (key, uname)

# Render the active result (from a fresh submit OR a display-only rerun).
active = st.session_state.get("active")
if active is not None:
    key, uname = active
    result = cache.get(key)
    if result is None:  # evicted from the LRU — ask for a re-scan
        st.info("Previous results expired — click Praise to scan again.")
    else:
        out = service.render_result(result, uname, view=view,
                                    highlights=highlights, min_stars=min_stars)
        if view == "json":
            st.json(json.loads(out))
        elif view == "markdown":
            st.markdown(out)
        else:
            st.text(out)

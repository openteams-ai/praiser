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

with st.form("q"):
    username = st.text_input("Username / login", placeholder="e.g. certik")
    c1, c2 = st.columns(2)
    forge = c1.selectbox("Forge", service.FORGES, index=0)
    view = c2.selectbox("View", service.VIEWS, index=0)
    forge_url = st.text_input(
        "Instance URL (self-hosted GitLab/Gitea/cgit — optional)",
        placeholder="https://gitlab.gnome.org")
    min_stars = st.slider("Min stars", 0, 1000, 50, step=10)
    highlights = st.slider("Highlights (top N)", 3, 20, 8)
    c3, c4 = st.columns(2)
    wikidata = c3.checkbox("Wikidata roles", value=True)
    package_registries = c4.checkbox("Package registries", value=True)
    cross_forge = c3.checkbox("Cross-forge (follow profile links)", value=False)
    discover_roles = c4.checkbox("LLM founder/role discovery (slower, costs)",
                                 value=False)
    submitted = st.form_submit_button("Praise")

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


if submitted:
    if not username.strip():
        st.warning("Enter a username.")
        st.stop()
    uname = username.strip()

    data_opts = {
        "forge": forge, "forge_url": forge_url.strip(), "min_stars": min_stars,
        "discover_roles": discover_roles, "wikidata": wikidata,
        "package_registries": package_registries, "cross_forge": cross_forge,
    }
    # Data-collection cache key (display options view/highlights excluded on
    # purpose): changing only N or the view re-renders the SAME collected result
    # instead of re-scanning. A change to username or any data option rescans.
    key = (uname, *(data_opts[k] for k in service.DATA_OPTIONS))

    # A size-bounded LRU across ALL scans this session, so revisiting an earlier
    # user/options is instant (not just tweaking N of the latest scan).
    if "results" not in st.session_state:
        st.session_state["results"] = SizeBoundedLRU(_CACHE_MB * 1024 * 1024)
    cache = st.session_state["results"]

    result = cache.get(key)
    if result is not None:
        st.success("✅ Showing cached results — change the username, forge, or a "
                   "scan option to re-scan.")
    else:
        st.info(
            "⏳ A first-time scan can take ~30 seconds to a few minutes — praiser "
            "queries the forge across many repositories (longer with cross-forge "
            "or LLM discovery on). Changing only the view or top-N is instant."
        )
        result, elapsed = _run_scan(uname, data_opts)
        cache.put(key, result)
        st.success(f"✅ Scan finished in {elapsed:.1f} seconds.")

    out = service.render_result(result, uname, view=view, highlights=highlights)
    if view == "json":
        st.json(json.loads(out))
    elif view == "markdown":
        st.markdown(out)
    else:
        st.text(out)

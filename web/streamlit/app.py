"""Streamlit UI for praiser — a thin frontend over web.core.

Deploy on Streamlit Community Cloud: main file = web/streamlit/app.py, and set
the token secrets (see web/README.md). All queries and tokens stay server-side.
"""

import json
import os
import sys
from pathlib import Path

# Streamlit runs this file directly, so only its own directory is on sys.path.
# Add the repo root so `web.core` and `praiser` import regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from web.core import service  # noqa: E402

# --- secrets -> env (server-side only; never sent to the browser) ----------
_SECRET_KEYS = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN", "FORGEJO_TOKEN",
    "GITEE_TOKEN", "BITBUCKET_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
)
for _k in _SECRET_KEYS:
    if _k in st.secrets and not os.environ.get(_k):
        os.environ[_k] = str(st.secrets[_k])


@st.cache_data(ttl=3600, show_spinner=False)
def _praise(username, forge, forge_url, min_stars, discover_roles, wikidata,
            package_registries, cross_forge, view, highlights):
    # Per-instance memo on top of the shared cache in service (which spans hosts).
    return service.praise(
        username, forge=forge, forge_url=forge_url, min_stars=min_stars,
        discover_roles=discover_roles, wikidata=wikidata,
        package_registries=package_registries, cross_forge=cross_forge,
        view=view, highlights=highlights,
    )


st.set_page_config(page_title="praiser", page_icon="🌟")
st.title("🌟 praiser")
st.caption("The open-source projects where a person holds an elevated role — "
           "author, maintainer, steering council, standards author — with "
           "evidence links, across GitHub, GitLab, Codeberg, Gitee, Bitbucket "
           "and cgit hosts.")

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

if submitted:
    if not username.strip():
        st.warning("Enter a username.")
        st.stop()
    with st.spinner(f"Scanning {username} on {forge}…"):
        try:
            out = _praise(username.strip(), forge, forge_url.strip(), min_stars,
                          discover_roles, wikidata, package_registries,
                          cross_forge, view, highlights)
        except Exception as exc:  # never dump a traceback at the user
            st.error(f"Failed: {exc}")
            st.stop()
    if view == "json":
        st.json(json.loads(out))
    elif view == "markdown":
        st.markdown(out)
    else:
        st.text(out)

# praiser web UI

A web frontend for [praiser](../). Layered so the frontend is swappable:

- **`core/`** — framework-agnostic. `service.praise(username, **options)` wraps
  `praiser.run` + render; `cache.make_cache()` picks a **shared Redis** cache
  (Upstash) when configured, else a local file cache.
- **`streamlit/`** — the Streamlit UI only (a thin form over `core`). To add a
  different frontend later (FastAPI, Gradio), add a sibling dir that reuses
  `web.core` — no changes to `core` needed.

## Deploy on Streamlit Community Cloud

1. Push this repo to GitHub (done) and go to <https://share.streamlit.io>.
2. **New app** → pick this repo, set **Main file path** to
   `web/streamlit/app.py`. It installs `web/streamlit/requirements.txt`; praiser
   is imported from the repo.
3. In the app's **Settings → Secrets**, add what you need (TOML):

   ```toml
   # Forge tokens (only the ones you'll query; public data works without, but
   # rate limits are low — GitHub 60/hr and Bitbucket 60/hr unauthenticated).
   GITHUB_TOKEN = "ghp_…"
   GITLAB_TOKEN = "glpat-…"
   CODEBERG_TOKEN = "…"      # or FORGEJO_TOKEN
   GITEE_TOKEN = "…"
   BITBUCKET_TOKEN = "…"     # app password / access token

   # LLM founder/role discovery (only if you enable the toggle) — one of:
   ANTHROPIC_API_KEY = "sk-ant-…"
   # CLAUDE_CODE_OAUTH_TOKEN = "…"

   # Shared, durable cache across hosts/restarts (recommended). Free serverless
   # Redis from https://upstash.com — create a DB, copy its REST URL + token.
   UPSTASH_REDIS_REST_URL = "https://….upstash.io"
   UPSTASH_REDIS_REST_TOKEN = "…"
   ```

   Without the Upstash secrets it falls back to a local cache (works, but
   Streamlit Cloud's disk is ephemeral, so it's lost on restart and not shared).

## Notes

- **Shared cache = the point:** praiser's data collection is expensive and
  option-independent, so the Redis cache makes the *second* query for a user (any
  options, any host) fast. `@st.cache_data` adds a per-instance memo on top.
- **Cost/quota:** the LLM toggle (founder/role discovery) costs money and runs
  per popular candidate — left **off** by default. Tokens are shared service-wide,
  so watch rate limits; consider an allowlist if the app is public.
- **Security:** tokens live only in server-side secrets/env — never sent to the
  browser.

## Run locally

```bash
pip install -e '.[http,llm,yaml]' && pip install -r web/streamlit/requirements.txt
export GITHUB_TOKEN=…            # and any other forge tokens
streamlit run web/streamlit/app.py
```

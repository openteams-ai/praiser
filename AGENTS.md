# AGENTS.md — conventions for praiser

Guidance for AI agents (and humans) contributing to **praiser**. Follow these so
contributions stay uniform with what's here. This file is the source of truth;
`CLAUDE.md` just points here.

## What praiser is

A CLI that, given a person's username, produces an **evidence-backed record of
the open-source projects where they hold an *elevated role*** — author/creator,
maintainer, code owner, steering-council member, standards (PEP/RFC) author, or
core contributor. Plain drive-by contributors are intentionally excluded. Every
claim carries a clickable evidence URL and a confidence score.

## Environment & workflow

- **Python ≥ 3.11** (uses stdlib `tomllib`; PEP 604 `X | None`). Do not add
  features that need 3.12+ without bumping `requires-python`.
- Dev setup: `conda activate praiser` (or any venv), then
  `pip install -e '.[dev,http,yaml,llm]'`.
- **Run `python -m pytest` before every commit; keep it green.** Tests are
  **offline** — never hit the network in a test (fake/monkeypatch the client).
- **Lint with `ruff check .` before pushing** (config in `pyproject.toml`; rules
  are pyflakes + a pragmatic pycodestyle subset, `E501` line-length intentionally
  off). `ruff check --fix` auto-fixes the safe ones. CI runs it as a separate job.
- Branch off `main`; open a PR. Commit messages: short imperative subject + a
  body explaining *why*. Keep diffs focused.
- CI (GitHub Actions) runs ruff + pytest on Python 3.11/3.12/3.13/3.14.

## Architecture (where things live)

Pipeline phases (`praiser/pipeline.py` orchestrates):

1. **identity** (`identity.py`) — build `{logins, names, emails}`.
2. **discovery** (`discovery.py`) — wide net of candidate repos (owned/org/
   contributed, full-history contributions, commit & name search, profile
   links, registry seeds, `--add-repo`). Drops forks/private; force-keeps manual.
3. **attribution** — run the extractor registry over candidates **in parallel**.
4. **popularity** (`popularity.py`) — split into primary / secondary buckets.
5. **render** (`render.py`) — highlights (default), or full md/json.

Supporting modules: `github_client.py` (all network + caching + rate limits),
`cache.py` (file cache), `config.py`, `registry.py` (the known-projects
meta-file), `llm.py` (optional, gated), `models.py` (roles + dataclasses).

## Core conventions (the important ones)

- **Extractors are the unit of extensibility.** One module per convention in
  `praiser/extractors/`. Each must:
  - keep its parsing logic in a **pure module-level function** (no network) so it
    can be unit-tested offline (e.g. `parse_codeowners`, `parse_proposal_header`);
  - expose an `Extractor` subclass whose `extract()` does the I/O via
    `ctx.client`;
  - call `register(MyExtractor())` at import and be listed in
    `_BUILTIN_MODULES` in `extractors/__init__.py` — a completeness guard test
    (`tests/test_extractors_registry.py`) fails CI if a `register()`-ing module
    is missing from that list. (Target: replace the manual list with
    auto-discovery so presence of the file *is* the wiring — see the
    extension-points convention below.)
- **Extension points must be auto-discovered or guarded, never a
  prose-synced list.** When you add a registry / plugin list / dispatch table,
  the enabled set must come from auto-discovery (the code is the source of
  truth) or, failing that, be backed by a test that fails loudly when an entry
  is missing. A manually maintained list whose only safeguard is documentation
  is a latent bug: the "register `wikipedia`/`releases`" step was documented in
  three places and still silently skipped (extractors never ran in production;
  #124). Make the wrong thing impossible or loud — don't rely on the extender
  reading the docs.
- **Roles live in `models.py`** as constants with `ROLE_WEIGHTS`. The headline
  role is the highest weight, ties broken by confidence. Add new roles there;
  pick the weight relative to neighbours deliberately (whole-repo roles should
  outrank subcomponent roles).
- **Evidence always has a clickable `url`.** `Evidence(source, role, url,
  confidence, detail)`. No claim without a link a human can verify.
- **Confidence calibration:** handle/email match ≈ 0.85–0.9; name-only ≈
  0.4–0.55; corroboration from multiple distinct sources bumps it. Be honest —
  low confidence is fine and informative.
- **Copy-resistance (critical).** Signals that a fork or vendored copy inherits —
  `CODEOWNERS`, `AUTHORS`, and *commit history* (a copied repo carries the
  user's commits!) — must be corroborated via `ExtractContext.trust_role_file`
  (own/org repo, or the canonical popular project). Never trust a
  copy-vulnerable signal on a small unaffiliated repo.
- **All network goes through `github_client`**, which caches every request and
  raises `RateLimitError` when exhausted. Don't bypass it. Gate expensive calls
  (contributor pagination, merged-PR search, LLM/web-search) behind popularity
  or `--add-repo`.
- **Parallelism:** attribution runs candidates in a thread pool. Worker bodies
  (`_scan_one`) do **network only**; the progress display, the records list, and
  the rate-limit stop are updated **on the main thread**. Cache writes are
  thread-safe (unique temp file per writer).
- **Registry hygiene:** `praiser/data/known_projects.json` is the **curated
  seed** — keep it generic (real popular/important projects, not one user's
  results). Runtime-learned data (popularity, discovered role sources) is written
  to `~/.local/share/praiser/known_projects.json`, never the seed.
- **Privacy/safety:** private repos are skipped by default; the GitHub token is
  **never** sent to non-GitHub URLs (`get_url` is unauthenticated).
- **LLM is optional and gated** — heuristics/regex first, Claude only as a
  fallback or for `--discover-roles`. Supports an API key *or* a Claude
  subscription OAuth token. Code must degrade silently when no LLM is available.
- **Contribution size** is measured by commits + contributor rank + merged-PR
  count + path-scoped commits. A LOC-diff axis is **intentionally deferred** —
  don't add it without a concrete need.

## Recipe: add a new role-extractor

1. Create `praiser/extractors/<name>.py` with a pure parser fn + an `Extractor`
   subclass; `register()` it.
2. Add `"<name>"` to `_BUILTIN_MODULES` in `extractors/__init__.py`.
3. If it needs a new role, add the constant + weight in `models.py` and a label
   in `render.py`.
4. Add offline tests in `tests/test_<name>.py` (pure parser + a fake-client
   extract test). Mirror the style of existing tests.
5. `python -m pytest`; update `README.md` (the extractor list + How it works).

## Testing conventions

- pytest, offline, fast. Prefer testing **pure functions** directly.
- For extractor `extract()` tests, pass a tiny fake client object exposing only
  the methods used (`get_files`, `repo_contributors`, `merged_pr_count`,
  `path_commit_count`, `get_url`, …).
- Every new behaviour gets a test. Don't lower coverage of the parsers.
- **Test through the wiring seam, not only the unit.** Unit-testing internals in
  isolation is good, but a test that imports the unit directly can't tell you the
  system actually reaches it (that's how #124 hid — every test imported the
  extractor, none went through `all_extractors()`). For any component behind a
  registry/dispatch, add one cheap test that exercises it via the **public entry
  point**. Cheap is the operative word — assert it appears in `all_extractors()`,
  don't spin up the whole app.

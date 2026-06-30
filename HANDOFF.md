# praiser — Session Handoff

A complete spec + design to continue building in a fresh Claude Code session.
Read this top-to-bottom; it contains every decision and the full scaffold design.

---

## 1. Goal

Build a CLI tool that, given a GitHub username, produces a **record of the
popular projects where that user holds an elevated role** —
maintainer / code owner / steering-council member / standards author.
**Explicitly skip plain contributors** (the record would get too large).

Different projects record roles differently (a `CODEOWNERS` file, a
`MAINTAINERS` file, a `GOVERNANCE.md`, a steering-council web page, enhancement
proposals with `Author:` headers, package-manifest author fields, or nothing).
The tool must **figure out which convention each project uses** rather than
assuming one.

## 2. Confirmed decisions (from the user)

1. **Unstructured-doc parsing** (`GOVERNANCE.md`, council pages): **heuristics
   first, LLM fallback only on ambiguous prose.** Keep the LLM gated behind a
   regex/keyword pass to control cost. LLM = Claude API (`anthropic` SDK,
   optional dependency).
2. **Form factor**: **CLI, single user.** One command takes a username, emits
   JSON (source of truth) + Markdown.
3. **Standards sources**: **Python PEPs, IETF RFCs, TC39/W3C, AND NEPs and other
   project-defined enhancement-proposal series.** This is the key
   generalization — see §4.

## 3. Pipeline (4 phases + identity resolution)

**Phase 0 — Identity resolution.** Build an identity set `{logins, names,
emails}` from the GitHub profile + harvested `.mailmap`/commit authors.
Handle/email matches = high confidence; name-only matches = low confidence
(guards against common-name false positives).

**Phase 1 — Candidate discovery (wide net).** Sources, strongest first:
- Org memberships (public) → org repos.
- Owned repos + repos with `admin`/`maintain` permission.
- `contributionsCollection` / `repositoriesContributedTo` (GraphQL) — over-
  collects contributors on purpose; Phase 2 filters them.
- Code search for the handle in `CODEOWNERS` / `MAINTAINERS` / `OWNERS` /
  `GOVERNANCE`.
- Targeted standards-repo lookups (PEP/NEP/RFC/TC39 — see §4).
Dedupe; drop non-elevated forks.

**Phase 2 — Role attribution (the heart).** A registry of **pluggable
extractors**, each returning `(role, evidence_url, confidence)`:
- `CODEOWNERS` (resolve `@org/team` → team members).
- `MAINTAINERS` / `OWNERS` (k8s YAML) / `GOVERNANCE.md` / `STEERING.md` /
  council web pages.
- Package manifests: `pyproject.toml`, `package.json`, `Cargo.toml`,
  `composer.json` author/maintainer fields.
- **Enhancement-proposal extractor (generalized)** — see §4.
- External standards: IETF RFC author index, TC39 champions, W3C.
- Org/team role via API.
Structured files → deterministic parse. Unstructured prose → keyword/regex,
then LLM fallback only when ambiguous. Drop contributor-only signals unless
they're the sole corroboration for a stronger claim. Multiple corroborating
extractors raise per-project confidence.

**Phase 3 — Popularity filter.** Stars/forks/dependents + package downloads
(PyPI/npm). Apply `--min-stars`, with an override so high-signal roles on
smaller-but-notable standards projects survive.

**Phase 4 — Render.** Rank by popularity × role weight. Per project: name, URL,
role, evidence link(s) (file+line or page URL), popularity, confidence.

## 4. The enhancement-proposal generalization (important)

PEP / NEP / Scientific-Python SPEC / JEP / etc. nearly all share ONE pattern:
**a folder of numbered proposal documents with a metadata header**
(`Author:` / `:Author:` in RST, or YAML front-matter in Markdown). So this is
**one extractor parameterized by `(repo, path, header-format)`**, NOT N
hand-written ones. The tool should also **auto-detect** the pattern: when a
candidate repo has a top-level dir of numbered `*.rst`/`*.md` files with author
metadata, treat it as a proposal series and parse author headers.

Known seeds to ship with: `python/peps` (PEP), `numpy/numpy` doc/neps (NEP),
`scientific-python/specs` (SPEC), `jupyter/enhancement-proposals` (JEP).

## 5. Stack / engineering notes

- **Python 3.13.** Modern syntax throughout — native `list[str]` / `X | None`,
  no `from __future__ import annotations` needed. `tomllib` is in the stdlib
  (no `tomli` dependency).
- **HTTP**: prefer **`httpx`** in the new (proper) environment; the original env
  lacked it, so stdlib `urllib` is an acceptable zero-dep fallback. GitHub
  **GraphQL** for discovery (batch to beat rate limits) + REST contents API for
  files. Needs a PAT (`--token` / `GITHUB_TOKEN`).
- **Cache**: simple file/JSON cache keyed by request hash, so re-runs and LLM
  steps don't re-fetch.
- **Extractors as a registry** — one module per convention; new conventions are
  cheap. Keep each extractor's **parsing logic as a pure function** so tests run
  offline with no network.
- **LLM**: `anthropic` SDK, optional extra. Use latest Claude (e.g.
  `claude-opus-4-8` or a cheaper tier for extraction). Gated behind heuristics.
- **TOML**: use stdlib `tomllib` (Python 3.11+) for `pyproject.toml` parsing —
  no third-party dep.
- **pytest**: dev dependency for offline parser tests.

## 6. Proposed layout

```
praiser/
├── README.md
├── pyproject.toml
├── .gitignore
├── praiser/
│   ├── __init__.py
│   ├── __main__.py          # python -m praiser
│   ├── cli.py               # argparse entry, praiser console script
│   ├── config.py            # token, thresholds, cache dir
│   ├── models.py            # Identity, Candidate, Evidence, Role, ProjectRecord
│   ├── github_client.py     # urllib GraphQL + REST client, cached
│   ├── cache.py             # file-based JSON cache
│   ├── identity.py          # Phase 0
│   ├── discovery.py         # Phase 1 (GraphQL queries below)
│   ├── popularity.py        # Phase 3
│   ├── render.py            # Phase 4 (md/json)
│   ├── pipeline.py          # orchestrates phases
│   └── extractors/
│       ├── __init__.py      # registry: register()/all_extractors()
│       ├── base.py          # Extractor ABC: applicable(), extract()
│       ├── codeowners.py    # parse_codeowners() pure fn + extractor
│       └── enhancement_proposals.py  # parse_proposal_header() + auto-detect
└── tests/
    ├── test_codeowners.py
    └── test_enhancement_proposals.py
```

### Extractor interface (target)

```python
class Extractor(ABC):
    name: str
    def applicable(self, candidate) -> bool: ...
    def extract(self, candidate, identity, client) -> list[Evidence]: ...
```

`Evidence(source, role, url, confidence, detail)`. Role constants +
weights live in `models.py`: maintainer, code_owner, steering_council,
standards_author, org_owner, org_member.

### Discovery GraphQL (target)

```graphql
query($login:String!) {
  user(login:$login) {
    login name email company
    organizations(first:100){nodes{login}}
    repositories(first:100, ownerAffiliations:[OWNER],
        orderBy:{field:STARGAZERS, direction:DESC}){
      nodes{ nameWithOwner stargazerCount isFork }
    }
    repositoriesContributedTo(first:100,
        contributionTypes:[COMMIT,PULL_REQUEST],
        orderBy:{field:STARGAZERS, direction:DESC}){
      nodes{ nameWithOwner stargazerCount isFork }
    }
  }
}
```
(`RepositoryOrder.field` is `STARGAZERS`, not `STARGAZER_COUNT`.)
File contents via REST: `GET /repos/{owner}/{repo}/contents/{path}` with
`Accept: application/vnd.github.raw`.

### Confidence guide
handle/email match in CODEOWNERS ≈ 0.9; `@org/team` match → 0.7, then 0.9 after
confirming team membership; proposal author handle/name match ≈ 0.85;
name-only prose match ≈ 0.4 (bump via LLM confirmation).

## 7. CLI shape

```
praiser <username> [--min-stars N] [--format md|json]
                     [--token TOKEN] [--cache-dir DIR] [--no-llm]
```

## 8. What was about to be written (pyproject.toml)

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "praiser"
version = "0.1.0"
description = "Generate a record of the popular projects a GitHub user maintains, steers, or authors standards for."
readme = "README.md"
requires-python = ">=3.13"
dependencies = []   # core runs on the stdlib; tomllib is built in on 3.11+

[project.optional-dependencies]
http = ["httpx>=0.27"]
llm = ["anthropic>=0.40"]
dev = ["pytest>=7"]

[project.scripts]
praiser = "praiser.cli:main"

[tool.setuptools.packages.find]
include = ["praiser*"]
```

## 9. Build order for the next session

1. `pyproject.toml`, `.gitignore`, `README.md`, package `__init__`.
2. `models.py` (Role constants/weights, dataclasses).
3. `cache.py` + `github_client.py` (urllib GraphQL+REST, cached).
4. `extractors/base.py` + `__init__.py` (registry).
5. `extractors/codeowners.py` and `extractors/enhancement_proposals.py` —
   pure parser fns first, then wire fetch.
6. `tests/` for both pure parsers (offline).
7. `identity.py`, `discovery.py`, `popularity.py`, `render.py`, `pipeline.py`,
   `cli.py`.
8. Smoke-test end-to-end against a real username once a PAT is set.

Start by initializing git in the new directory.

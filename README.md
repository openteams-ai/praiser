# praiser

[![CI](https://github.com/openteams-ai/praiser/actions/workflows/ci.yml/badge.svg)](https://github.com/openteams-ai/praiser/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/praiser.svg)](https://pypi.org/project/praiser/)
[![Python versions](https://img.shields.io/pypi/pyversions/praiser.svg)](https://pypi.org/project/praiser/)

**🌟 Try the web demo: <https://praiser.streamlit.app/>**

Given a username, **praiser** records the popular open-source projects where
that person holds an **elevated role** — author/creator, maintainer, code owner,
steering-council member, standards (PEP/RFC) author, or core contributor — with
a clickable **evidence link** and a **confidence** score for every claim. Plain
drive-by contributors are intentionally excluded (the record would otherwise be
enormous and low-signal). By default it prints a compact **highlights** summary;
`--format md|json` gives the full per-project report.

Projects record roles in many different ways — a `CODEOWNERS` file, a
`MAINTAINERS` list, Kubernetes `OWNERS` YAML, a `GOVERNANCE.md` page, a package
manifest's author field, a numbered enhancement-proposal series with `Author:`
headers, a team page on the project's website, the commit history, package
registries (PyPI/npm/crates), **Wikidata** creator/developer claims (matched by
GitHub handle), or the **Wikipedia** infobox's original-author(s) field (which
often outlives the project's own AUTHORS file). `praiser` figures out **which
convention each project uses** rather than assuming one, and corroborates signals
that a fork or vendored copy could fake.

It scans **GitHub** by default, plus **GitLab** (`--forge gitlab`), **Codeberg**
/ any Gitea/Forgejo host (`--forge codeberg`), **Gitee** (`--forge gitee`),
**Bitbucket** (`--forge bitbucket`), and API-less **cgit** hosts like kernel.org
or Savannah (`--forge cgit`) — including **self-hosted instances** with
`--forge-url` (e.g. `--forge gitlab --forge-url https://gitlab.gnome.org`). The
pipeline talks to a neutral `Forge` interface, so adding another host is a
self-contained addition.

People use different usernames on different forges, so `--cross-forge` follows
the links a person publishes on their own profile — and on the personal site
those profiles point to — to their accounts elsewhere, keeping only links
confirmed either **bidirectionally** (the other profile links back) or through
an **owned personal-site hub** (a site reached from, and linking back to, a
confirmed account, that also lists the other account with a matching
handle/name), and merges everything into one record. Because the links are owner-published and
mutually confirmed, it never falsely merges two different people (it may
under-merge someone who hasn't cross-linked, which is safe). `--also-forge
FORGE:LOGIN` adds an identity explicitly when you'd rather not rely on links.

## Roles

<!-- ROLE-GLOSSARY:START — generated from praiser.render.render_role_glossary; keep verbatim (tests/test_role_glossary.py enforces it stays in sync). -->
praiser reports **software-engineering authorship and stewardship** — a factual, evidence-linked relation between a person and a project, on the engineering axis and independent of academic authorship. Every claim carries an evidence link and a confidence score. The roles, most elevated first:

- **Steering council** — Holds a named seat in the project's formal governance body. _Evidence:_ Listed in a governance document under steering-council / committee keywords, or on the project's governance page (handle-required).
- **Author** — Originated the project, or a named self-contained component of it (the creator). Not a copyright claim, not a claim of *sole* authorship, and its absence does not mean a person didn't create the project — many projects attribute authorship collectively by design. _Evidence:_ Wikidata / Wikipedia founder attribution, or a manifest / ownership author claim corroborated by commit history.
- **Maintainer** — Ongoing project-level stewardship and merge authority. _Evidence:_ MAINTAINERS or OWNERS approvers, a package-registry maintainer, a Wikidata developer claim, the maintainers/team page, or a commit-corroborated manifest maintainer field.
- **Standards author** — Authored a formal enhancement proposal or standard for the project. _Evidence:_ Named in the Author field of a proposal document header (PEP, NEP, RFC, XEP, …).
- **Code owner** — Designated required-reviewer over specific code paths. Shown scoped to the owned path(s), e.g. "Code owner (compiler/, docs/)"; a whole-repo (`*`) owner is shown bare. _Evidence:_ CODEOWNERS entries or OWNERS reviewers.
- **Release manager** — Ships the project's releases — trusted to publish. _Evidence:_ Authored a dominant share of the recent releases.
- **Core contributor** — A substantial builder of a widely-used project. Being listed in an AUTHORS / all-contributors file lands here, not under Author. _Evidence:_ High commit volume or a genuine top-of-project rank; substantial commits to a named subcomponent; or an AUTHORS / all-contributors listing.
<!-- ROLE-GLOSSARY:END -->

Roles display in project-lifecycle order (origination → governance → building → maintenance), a person may hold several on one project, and the headline uses the strongest-evidenced role (`popularity × role weight × confidence`).

## Install

```bash
pip install praiser                 # core (stdlib only)
pip install 'praiser[http]'         # + httpx (faster, pooled HTTP; recommended)
pip install 'praiser[http,llm,yaml]'  # + Claude fallback + YAML role files
```

The core has **no dependencies** (it runs on the stdlib), so `pip install praiser`
is enough to get the `praiser` command. The extras add: `http` (httpx, falls back
to urllib), `llm` (Claude fallback for ambiguous governance prose), `yaml`
(Kubernetes `OWNERS` / YAML-front-matter proposals).

From a checkout, for development:

```bash
pip install -e '.[http,llm,yaml,dev]'   # editable + all extras + tests
```

Requires Python 3.11+ (for the stdlib `tomllib`).

## Usage

```bash
export GITHUB_TOKEN=ghp_...        # a PAT; raises rate limits and enables search
praiser torvalds                 # default: the highlights summary (below)
praiser gvanrossum --format md   # the full report (Markdown)
praiser gvanrossum --format json -o gvanrossum.json   # full report as JSON
praiser someuser --no-discover-roles --no-llm         # skip the LLM/web features
```

By default `praiser <username>` prints a compact **highlights** summary — the
top roles, one line each, plus breadth stats. Use `--format md|json` for the
full per-project report with evidence links, or `--highlights N` to change the
count.

```
pearu — top 8 highlights:
- pytorch/pytorch (101k★) — Core contributor, Code owner, Maintainer (#88/~6700)
- numpy/numpy (32k★) — Author (f2py), Core contributor, Maintainer (#8/~2100)
- scipy/scipy (15k★) — Core contributor, Maintainer (#14/~1900)
- heavyai/heavydb (3k★) — Core contributor (#19/80)
- sympy/sympy (15k★) — Core contributor (#162/~1500)
- pearu/pylibtiff (140★) — Author, Core contributor (#2/24)
- numba/numba (11k★) — Core contributor (#29/~440)
- rapidsai/cudf (10k★) — Core contributor (#78/~340)
…plus 5 more elevated-role project(s); 15 smaller but widely-used project(s) with a notable role.
Reach: 28 project(s) across 9 communities (distinct orgs).
```

Each line reads `REPO (STARS★) — ROLES`. When the role rests on ranked
contribution, a `(#R/N)` suffix gives the user's rank `R` among the project's
`N` contributors (e.g. `#2/24`). `N` is **exact** when praiser read the whole
contributor list; **`~N`** (rounded, e.g. `~6700`) when the total is a
large-project estimate — GitHub's contributor API only resolves the top ~500
accounts, so praiser reads the true total (distinct commit-author identities) in
one extra request, or uses a curated snapshot; and **`N+`** only when even that
couldn't be resolved. Roles scoped to one part of a project are qualified, e.g.
`Author (f2py)`. The suffix is omitted when that standing isn't known.

The footer summarises breadth beyond the top roles: the smaller-but-widely-used
projects where the user also holds a notable role, and the **community reach**
(distinct organisations) — a proxy for the potential to introduce ideas widely.

`praiser <username>` is meant to be sufficient on its own: role auto-discovery
and registry persistence are **on by default** (auto-discovery activates only
when LLM credentials are present, and degrades silently otherwise).

### GitHub token

Without a token GitHub allows only ~60 requests/hour, which is not enough.
Provide one via `--token`, the `GITHUB_TOKEN`/`GH_TOKEN` env var, or simply be
logged into the [`gh` CLI](https://cli.github.com) (`gh auth login`) — the tool
falls back to `gh auth token` automatically.

For the optional LLM features (`--discover-roles`, and the governance prose
fallback) set an Anthropic API key — create one at
**https://console.anthropic.com/settings/keys**, then
`export ANTHROPIC_API_KEY=...` and install the extra (`pip install
'praiser[llm]'`).

Create a GitHub Personal Access Token at **https://github.com/settings/tokens**:

* *classic* — no scopes are needed for public data; add `repo` (private repos)
  and `read:org` (resolve `@org/team` membership in CODEOWNERS) for full
  coverage;
* *fine-grained* — read-only **Contents** + **Members** permissions.

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

### Rate limits & performance

A token is capped at 5,000 REST requests/hour and that ceiling can't be raised
for a PAT (only GitHub Apps / Enterprise Cloud go higher). To stay under it the
tool:

* **fetches file contents via GraphQL in batches** — GraphQL is a *separate*
  5,000-points/hour bucket, so the bulk of the work (reading CODEOWNERS,
  manifests, and possibly hundreds of proposal files) doesn't touch the REST
  limit, and many files come back in one request;
* **caches every request** so re-runs and resumed runs are nearly free;
* **drops forks** and only deep-scans plausible candidates.

If a run is rate-limited it stops early, tells you how long to wait, and the
cache preserves what already succeeded — so re-running finishes the job.

```
praiser <username>
    [--forge github|codeberg|gitlab|gitee|bitbucket|cgit]  code host (default: github)
    [--forge-url URL]      self-hosted instance for --forge gitlab|codeberg|cgit
    [--forge-name LABEL]   short label for the --forge-url instance
    [--cross-forge]        follow verified profile links to the person's other
                           forges and merge into one record
    [--also-forge FORGE:LOGIN]  also scan this identity on another forge (repeatable)
    [--min-stars N]        popularity threshold (default 50)
    [--highlights [N]]     top-N highlights summary (this is the DEFAULT view; N=8)
    [--format md|json]     full per-project report instead of the highlights
    [--token TOKEN]        or GITHUB_TOKEN / GH_TOKEN
    [--cache-dir DIR]      default ~/.cache/praiser
    [--registry FILE]      known-projects file (default: ~/.local/share/praiser/)
    [--no-save-registry]   don't persist popularity + discovered role sources
    [--no-discover-roles]  don't web-search for role pages (default: on w/ LLM)
    [--no-wikidata]        don't derive creator/developer roles from Wikidata
    [--no-package-registries] skip PyPI/npm/crates.io lookups (default: on)
    [--no-llm]             disable all Claude features
    [--add-repo OWNER/REPO] force-scan a repo discovery missed (repeatable)
    [--include-private]    also scan private repos (default: skip them)
    [--contributor-pages N] contributors API pages/repo, 100 each (default: 2)
    [-j N | --jobs N]     candidates scanned concurrently (default: 8)
    [-o FILE]              write output to a file instead of stdout
    [-v]                   detailed per-repo logging
    [-q]                   suppress the live progress display
```

On an interactive terminal the tool shows live progress on stderr
(`scanning repo 42/107 …`) so you can see it working; output (JSON/Markdown)
still goes to stdout. Progress is automatically suppressed when stderr is
redirected, with `-q`, or in `-v` mode (which prints detailed logs instead).

JSON is the source of truth; Markdown is a human-readable view. Every claim
carries an **evidence link** (file/page URL) and a **confidence** score.

## Web demo

A [Streamlit](https://streamlit.io) web UI wraps the same engine: type a
username, pick a forge, and get the ranked record with evidence links — with
instant **view / top-N / min-stars** controls, a live progress bar, and a
"recent scans" picker. Collected results are shared across sessions via a
durable cache, so repeat lookups are fast.

- **Hosted demo:** <https://praiser.streamlit.app/>
- **Run locally or deploy your own:** see [`web/README.md`](web/README.md).

The web layer is split into a framework-agnostic core (`web/core` — a
`praise()` service + cache) and the Streamlit frontend (`web/streamlit`), so a
different frontend (FastAPI, Gradio, …) can reuse the core unchanged.

## How it works

1. **Identity resolution** — assemble `{logins, names, emails}` from the
   profile. Handle/email matches are high-confidence; name-only matches are weak.
2. **Discovery (wide net)** — owned repos, org repos, contributed-to repos
   (over-collected on purpose), **commit search** (`author:`, catches old
   involvement the contribution graph has dropped), code search for the handle
   in role files, **name search** in `AUTHORS`/`THANKS`/`CONTRIBUTORS`,
   **package registries** (packages the user maintains on npm/crates.io, whose
   source repos are pulled in — catches projects where the role is "package
   maintainer" rather than "top committer"; `--no-package-registries` to skip),
   and curated registry seeds.
   Forks (which inherit upstream role files) and private repos are dropped
   here — a public "popular projects" record shouldn't surface or leak private
   repos. Use `--include-private` to scan them anyway. If the net still misses a
   project (e.g. a private-dev repo, or one whose history GitHub doesn't
   attribute), name it with `--add-repo OWNER/REPO` — it's force-scanned and
   force-included, with the role still detected automatically.
3. **Role attribution** — a registry of pluggable [extractors](praiser/extractors)
   (`ownership`, `codeowners`, `maintainers`, `manifests`, `enhancement_proposals`,
   `governance`, `contributors`, `subcomponents`, `authors`, `web_roles`,
   `packages`). The
   `contributors` signal measures size by commits **and** merged-PR count
   (robust to squash/ghstack one-commit-per-PR workflows and unlinked commit
   emails); `subcomponents` credits leading/authoring a *part* of a monorepo via
   commit-path analysis (e.g. f2py in NumPy, sparse tensors in PyTorch) — seeded
   in the registry and extendable with `--add-repo owner/repo:path`. `packages`
   credits **maintainer** of an npm/crates.io package (keyed on the user's login)
   and **author** of a PyPI distribution (matched on the author/maintainer name,
   so a popular package isn't mis-credited to a mere contributor) — only when
   the package itself names the repo as its source, which guards against
   registry-handle collisions.
   (A LOC-diff size axis is intentionally deferred — noisy with generated/vendored
   code and costly to compute — until a need justifies the extra dimension.)
   A repo under the user's
   own account is attributed as **author/creator**, and manifest `authors` vs
   `maintainers` fields map to the author vs maintainer roles — so a user's own
   projects read "Author", not merely "core contributor". Structured files are parsed
   deterministically; ambiguous prose falls back to Claude **only after** a
   keyword/regex pass. `contributors` records a **core-contributor** role for
   substantial committers to popular/widely-used repos (catches historical
   maintainers and authors of major components, e.g. f2py in NumPy). Role-file
   matches (`CODEOWNERS`/`AUTHORS`) are corroborated with **copy-resistant**
   signals — affiliation or being the canonical popular project — so a repo that
   *vendored* an upstream's history and role files isn't a false positive.
4. **Popularity filter** — `--min-stars`, with an override so high-signal roles
   on smaller-but-notable standards projects survive. Elevated-role projects
   that miss the bar but are **widely used and maintained** (real fork
   engagement + recently pushed) are reported as a secondary group with a count.
5. **Render** — ranked by popularity × role weight × confidence. Live
   rate-limit dynamics (REST/GraphQL remaining) are shown during the scan.

## The known-projects registry

[`praiser/data/known_projects.json`](praiser/data/known_projects.json) stores
popular/important projects together with:

* **`role_conventions`** — how that project records roles *in the repo* (which
  extractor + path + header format), so extractors can parse directly instead of
  re-detecting, and curated knowledge is reusable;
* **`role_sources`** — **authoritative web pages** that list role holders, with
  the role each confers. Many projects record maintainers/steering councils on a
  site, not in a repo file, and the format varies wildly — so you point at the
  exact URL rather than have the tool guess. The `web_roles` extractor fetches
  each page and matches the user by GitHub handle (a `github.com/<handle>` link)
  or full name. Example:
  ```json
  "numpy/numpy": {
    "role_sources": [
      {"url": "https://numpy.org/teams/", "role": "maintainer", "label": "NumPy team"},
      {"url": "https://numpy.org/about/", "role": "steering_council", "label": "Steering Council"}
    ]
  }
  ```
  This is more authoritative than commit-count heuristics — it reflects the
  project's own statement of who holds the role — and it's why a vendored *copy*
  of a project (which carries the upstream's commit history, making the user look
  like a heavy committer) is not mistaken for a real role: role-file and
  contributor signals are trusted only on the user's own/org repos or the
  canonical popular project, never on a small unaffiliated copy.
* **`popularity`** — cached/curated stars/forks plus `min_stars_override` for
  high-signal-but-small standards projects;
* **`importance`** — a human label (`critical`, `high`, ...).

Point `--registry mine.json` at your own file to extend or override the seed;
add `--save-registry` to have observed popularity **and any web-discovered role
sources** (`--discover-roles`) written back — so a one-off discovery becomes
reusable curated knowledge. Authoritative roles are conservative: high-authority
roles like steering council require a GitHub-**handle** match on the page (not
just a name, which is too easily a founder/credit mention).

Discovery results are also cached (the web-search call and fetched pages), so
re-runs don't re-search even without `--save-registry`.

### Enhancement-proposal generalization

PEP / NEP / SPEC / JEP and friends share one shape: a folder of numbered
documents with an `Author:` (or `:Author:`, or YAML front-matter) header. They
are handled by **one** extractor parameterized by `(path, header_format)`, which
also **auto-detects** the pattern when a repo has a directory of numbered
`*.rst`/`*.md` files with author metadata.

## Issues & feedback

Feedback is very welcome — the web demo's buttons and the issue tracker both open
GitHub issues. Since praiser is itself built with an AI agent, here's how
interactions work, so you know who you're talking to:

- **Issues are triaged with an AI agent** (Claude Code). When it comments it posts
  under the maintainer's account, always with a note saying which kind of message
  it is: _"generated without human review"_ (an agent draft the maintainer hasn't
  vetted yet) or _"reviewed & approved by @pearu"_ (the maintainer read it and
  stands behind it).
- **A human is in the loop.** Pearu reads the issues and reviews the agent's work
  — you're not shouting into a void.
- **No response-time promise.** This is a for-fun project maintained in spare
  time, so issues are handled best-effort — sometimes slowly, sometimes not at
  all. That's honesty, not a brush-off: genuine reports (especially false
  positives/negatives on your own handle) are exactly what make praiser better.

## Development

```bash
pip install -e '.[dev]'
pytest            # offline parser tests, no network
```

Each extractor keeps its parsing logic in a pure function (e.g.
`parse_codeowners`, `parse_proposal_header`, `parse_owners_yaml`) so tests run
fully offline.

### Releasing to PyPI

The version is single-sourced from `praiser.__version__`. To cut a release:

1. Bump `__version__` in `praiser/__init__.py`; commit.
2. Create a **GitHub Release** with tag `vX.Y.Z` (matching the version).
3. The `publish.yml` workflow builds the sdist + wheel and uploads them to PyPI
   via **Trusted Publishing** (OIDC — no API token stored).

**One-time PyPI setup** (before the first release): on PyPI, add a *trusted
publisher* for the project — owner `openteams-ai`, repo `praiser`, workflow
`publish.yml`, environment `pypi`. (For the very first upload you can instead
`python -m build && twine upload dist/*` with a PyPI token, then switch to
trusted publishing.)

## Author & license

Created by **Pearu Peterson** (pearu.peterson@gmail.com), with assistance from
**Claude** (Anthropic). Licensed under the **BSD 3-Clause** license — see
[LICENSE](LICENSE).

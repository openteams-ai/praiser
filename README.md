# praiser

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
headers, a team page on the project's website, or just the commit history.
`praiser` figures out **which convention each project uses** rather than assuming
one, and corroborates signals that a fork or vendored copy could fake.

## Install

```bash
pip install -e .            # core (stdlib only)
pip install -e '.[http,llm,yaml,dev]'   # httpx + Claude fallback + YAML + tests
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
- pytorch/pytorch — Maintainer (101k★, conf 0.90)
- numpy/numpy — Maintainer (32k★, conf 0.90)
- scipy/scipy — Maintainer (15k★, conf 0.90)
- heavyai/heavydb — Core contributor (3k★, conf 0.80)
…
…plus N more elevated-role project(s); M smaller but widely-used project(s) with a notable role.
Reach: T project(s) across C communities (distinct orgs).
```

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
    [--min-stars N]        popularity threshold (default 50)
    [--highlights [N]]     top-N highlights summary (this is the DEFAULT view; N=8)
    [--format md|json]     full per-project report instead of the highlights
    [--token TOKEN]        or GITHUB_TOKEN / GH_TOKEN
    [--cache-dir DIR]      default ~/.cache/praiser
    [--registry FILE]      known-projects file (default: ~/.local/share/praiser/)
    [--no-save-registry]   don't persist popularity + discovered role sources
    [--no-discover-roles]  don't web-search for role pages (default: on w/ LLM)
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

## How it works

1. **Identity resolution** — assemble `{logins, names, emails}` from the
   profile. Handle/email matches are high-confidence; name-only matches are weak.
2. **Discovery (wide net)** — owned repos, org repos, contributed-to repos
   (over-collected on purpose), **commit search** (`author:`, catches old
   involvement the contribution graph has dropped), code search for the handle
   in role files, **name search** in `AUTHORS`/`THANKS`/`CONTRIBUTORS`, and
   curated registry seeds.
   Forks (which inherit upstream role files) and private repos are dropped
   here — a public "popular projects" record shouldn't surface or leak private
   repos. Use `--include-private` to scan them anyway. If the net still misses a
   project (e.g. a private-dev repo, or one whose history GitHub doesn't
   attribute), name it with `--add-repo OWNER/REPO` — it's force-scanned and
   force-included, with the role still detected automatically.
3. **Role attribution** — a registry of pluggable [extractors](praiser/extractors)
   (`ownership`, `codeowners`, `maintainers`, `manifests`, `enhancement_proposals`,
   `governance`, `contributors`, `subcomponents`, `authors`, `web_roles`). The
   `contributors` signal measures size by commits **and** merged-PR count
   (robust to squash/ghstack one-commit-per-PR workflows and unlinked commit
   emails); `subcomponents` credits leading/authoring a *part* of a monorepo via
   commit-path analysis (e.g. f2py in NumPy, sparse tensors in PyTorch) — seeded
   in the registry and extendable with `--add-repo owner/repo:path`.
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

## Development

```bash
pip install -e '.[dev]'
pytest            # offline parser tests, no network
```

Each extractor keeps its parsing logic in a pure function (e.g.
`parse_codeowners`, `parse_proposal_header`, `parse_owners_yaml`) so tests run
fully offline.

## Author & license

Created by **Pearu Peterson** (pearu.peterson@gmail.com), with assistance from
**Claude** (Anthropic). Licensed under the **BSD 3-Clause** license — see
[LICENSE](LICENSE).

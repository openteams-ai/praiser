# gh-record

Generate a record of the **popular projects where a GitHub user holds an
elevated role** — maintainer, code owner, steering-council member, or standards
author. Plain contributors are intentionally excluded (the record would
otherwise be enormous and low-signal).

Projects record roles in many different ways — a `CODEOWNERS` file, a
`MAINTAINERS` list, Kubernetes `OWNERS` YAML, a `GOVERNANCE.md` page, a
package manifest's author field, or a numbered enhancement-proposal series with
`Author:` headers. `gh-record` figures out **which convention each project uses**
rather than assuming one.

## Install

```bash
pip install -e .            # core (stdlib only)
pip install -e '.[http,llm,yaml,dev]'   # httpx + Claude fallback + YAML + tests
```

Requires Python 3.13+.

## Usage

```bash
export GITHUB_TOKEN=ghp_...        # a PAT; raises rate limits and enables search
gh-record torvalds --min-stars 100
gh-record gvanrossum --format json -o gvanrossum.json
gh-record someuser --no-llm -v
```

### GitHub token

Without a token GitHub allows only ~60 requests/hour, which is not enough.
Provide one via `--token`, the `GITHUB_TOKEN`/`GH_TOKEN` env var, or simply be
logged into the [`gh` CLI](https://cli.github.com) (`gh auth login`) — the tool
falls back to `gh auth token` automatically.

Create a Personal Access Token at **https://github.com/settings/tokens**:

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
gh-record <username>
    [--min-stars N]        popularity threshold (default 50)
    [--format md|json]     output format (default md)
    [--token TOKEN]        or GITHUB_TOKEN / GH_TOKEN
    [--cache-dir DIR]      default ~/.cache/ghrecord
    [--registry FILE]      extra known-projects file (merged over the seed)
    [--save-registry]      write observed popularity back to --registry
    [--no-llm]             disable the Claude fallback
    [--include-private]    also scan private repos (default: skip them)
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
   repos. Use `--include-private` to scan them anyway.
3. **Role attribution** — a registry of pluggable [extractors](ghrecord/extractors)
   (`codeowners`, `maintainers`, `manifests`, `enhancement_proposals`,
   `governance`, `contributors`, `authors`). Structured files are parsed
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

[`ghrecord/data/known_projects.json`](ghrecord/data/known_projects.json) stores
popular/important projects together with:

* **`role_conventions`** — how that project records roles (which extractor +
  path + header format), so extractors can parse directly instead of
  re-detecting, and curated knowledge is reusable;
* **`popularity`** — cached/curated stars/forks plus `min_stars_override` for
  high-signal-but-small standards projects;
* **`importance`** — a human label (`critical`, `high`, ...).

Point `--registry mine.json` at your own file to extend or override the seed;
add `--save-registry` to have observed popularity written back.

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

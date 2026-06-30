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

```
gh-record <username>
    [--min-stars N]        popularity threshold (default 50)
    [--format md|json]     output format (default md)
    [--token TOKEN]        or GITHUB_TOKEN / GH_TOKEN
    [--cache-dir DIR]      default ~/.cache/ghrecord
    [--registry FILE]      extra known-projects file (merged over the seed)
    [--save-registry]      write observed popularity back to --registry
    [--no-llm]             disable the Claude fallback
    [-o FILE] [-v]
```

JSON is the source of truth; Markdown is a human-readable view. Every claim
carries an **evidence link** (file/page URL) and a **confidence** score.

## How it works

1. **Identity resolution** — assemble `{logins, names, emails}` from the
   profile. Handle/email matches are high-confidence; name-only matches are weak.
2. **Discovery (wide net)** — owned repos, org repos, contributed-to repos
   (over-collected on purpose), code search for the handle in role files, and
   curated registry seeds.
3. **Role attribution** — a registry of pluggable [extractors](ghrecord/extractors)
   (`codeowners`, `maintainers`, `manifests`, `enhancement_proposals`,
   `governance`). Structured files are parsed deterministically; ambiguous prose
   falls back to Claude **only after** a keyword/regex pass.
4. **Popularity filter** — `--min-stars`, with an override so high-signal roles
   on smaller-but-notable standards projects survive.
5. **Render** — ranked by popularity × role weight × confidence.

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

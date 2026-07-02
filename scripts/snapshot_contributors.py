#!/usr/bin/env python3
"""Report the true total contributor count for registry seeds.

For repos with more than ~500 contributors, GitHub's `contributors` API caps the
GitHub-account count near ~500, so praiser would otherwise show `N+`. This tool
reads the true total (distinct commit-author identities, uncapped) via the
one-request `Link`-header trick, so a maintainer can record it as
`popularity.contributors` in praiser/data/known_projects.json for the big seeds.

It prints a report only — the registry is a small curated file, so paste the
reported numbers in by hand (preserving the file's compact formatting) rather
than letting a serializer reflow the whole file. (Note: normal CLI scans already
self-populate a *user's* own registry via record_popularity; this is just for the
shipped seed list.)

Usage:
    python scripts/snapshot_contributors.py            # report all seeds
    python scripts/snapshot_contributors.py --min 500   # highlight repos over N

Needs a GitHub token (GITHUB_TOKEN/GH_TOKEN or `gh auth token`).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from praiser.cache import Cache  # noqa: E402
from praiser.forge import GitHubForge  # noqa: E402
from praiser.registry import KnownProjects  # noqa: E402


def _token() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=500,
                    help="highlight repos with more than this many contributors")
    args = ap.parse_args()

    reg = KnownProjects.load()
    forge = GitHubForge(_token(), Cache(Path("/tmp/praiser-snapshot-cache")))
    seeds = [p for p in reg.seeds() if "/" in p.name_with_owner]

    rows = []
    for p in seeds:
        owner, _, repo = p.name_with_owner.partition("/")
        try:
            n = forge.repo_contributor_count(owner, repo, anon=True)
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! {p.name_with_owner}: {exc}", file=sys.stderr)
            n = None
        rows.append((p.name_with_owner, n))
        print(f"{p.name_with_owner:45s} {n if n is not None else '?'}")

    over = [(nm, n) for nm, n in rows if n and n > args.min]
    print(f"\n{len(seeds)} GitHub seed(s); {len(over)} with > {args.min} contributors "
          "— record these as popularity.contributors in known_projects.json:")
    for nm, n in sorted(over, key=lambda r: -r[1]):
        print(f"  {nm:45s} \"popularity\": {{ ..., \"contributors\": {n} }}")
    forge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

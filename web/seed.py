"""Admin seeder for the web app's shared reverse-index (#65 / #59 Phase 2).

NOT exposed to normal app users. A knowledgeable operator triggers it — via the
token-gated admin panel in ``app.py``, or ``python -m web.seed ORG`` — to
populate the **shared** reverse-index (Upstash Redis when configured) from an
org's repos, so the deployed app can discover that org's contributors.

Layering: ``praiser`` (the library) must not import ``web``; so this web-side
runner is what marries ``praiser.seed.seed_org`` to the web's shared cache.
Roster fetches use an ephemeral local http cache; only the compact index +
per-repo markers go to the shared cache.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from praiser.contribindex import ContributorIndex  # noqa: E402
from praiser.pipeline import FORGES  # noqa: E402
from praiser.seed import seed_org  # noqa: E402
from web.core.cache import local_cache, make_result_cache  # noqa: E402

# Forge-aware interface so URLs like ?seed=github/numpy never need to change, but
# functional seeding is GitHub-only today: most forges don't implement
# organization_repositories + repo_contributors, and the pipeline consults the
# reverse-index only for GitHub scans. Non-github forges seed to 0 for now.
SEEDABLE_FORGES = ("github",)


def parse_seed_target(target: str, default_forge: str = "github") -> tuple[str, str]:
    """Parse a seed target into (forge, org). Accepts 'org' (uses default_forge)
    or 'forge/org' (e.g. 'github/numpy'), matching the ?seed= URL form."""
    target = (target or "").strip().strip("/")
    if "/" in target:
        forge, _, org = target.partition("/")
        return forge.strip().lower(), org.strip()
    return default_forge, target


def _token(forge: str) -> str | None:
    if forge != "github":
        return None
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def run_seed(org: str, forge: str = "github", budget: int = 30,
             log=lambda m: None) -> dict:
    """Seed the SHARED reverse-index from ``org``'s repos on ``forge``. Roster
    fetches use an ephemeral local http cache; the index + per-repo seed markers
    live in the shared cache (Redis when configured) that the app reads."""
    if forge not in FORGES:
        return {"org": org, "forge": forge, "seeded": 0,
                "contributors_indexed": 0, "repos_available": 0,
                "stopped": f"unknown forge '{forge}'"}
    shared = make_result_cache()                 # shared Redis (or local fallback)
    f = FORGES[forge](_token(forge), local_cache())  # ephemeral http cache
    index = ContributorIndex(shared)
    try:
        res = seed_org(org, forge=f, index=index, cache=shared,
                       budget=budget, log=log)
    finally:
        f.close()
    res["forge"] = forge
    if forge not in SEEDABLE_FORGES and res.get("seeded", 0) == 0:
        res["stopped"] = f"{forge} seeding not supported yet (GitHub-only)"
    return res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="web.seed",
        description="Seed the web app's SHARED reverse-index from an org's repos "
                    "(run with the deployment's Upstash + bot-token env).")
    p.add_argument("org", help="organization login (e.g. numpy), or forge/org "
                                "(e.g. github/numpy)")
    p.add_argument("--forge", default="github",
                   help="forge to seed from (default: github; only github is "
                        "functional today)")
    p.add_argument("--budget", type=int, default=30, metavar="N",
                   help="max repos to seed this run (default: 30)")
    args = p.parse_args(argv)
    forge, org = parse_seed_target(args.org, args.forge)
    res = run_seed(org, forge, args.budget, log=lambda m: print(f"[seed] {m}"))
    print(f"[seed] {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

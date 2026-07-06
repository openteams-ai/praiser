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
from praiser.seed import seed_one, seed_org  # noqa: E402
from web.core.cache import local_cache, make_result_cache  # noqa: E402

# Background seeder tuning. A "chunk" is one org, budget-capped, so a single
# opportunistic tick stays bounded. The lease TTL must exceed one chunk's runtime.
SEED_CHUNK_BUDGET = 30
SEED_LOCK_TTL = 300

# Forge-aware interface so URLs like ?seed=github/numpy never need to change, but
# functional seeding is GitHub-only today: most forges don't implement
# organization_repositories + repo_contributors, and the pipeline consults the
# reverse-index only for GitHub scans. Non-github forges seed to 0 for now.
SEEDABLE_FORGES = ("github",)


def parse_seed_target(target: str, default_forge: str = "github") -> tuple[str, str, str]:
    """Parse a ?seed= target into (forge, kind, name).

    An optional leading segment is the forge iff it's a known forge name; the
    remaining 1 segment is an org, 2 segments are a single owner/repo:
      numpy                     -> (github, "org",  "numpy")
      github/numpy              -> (github, "org",  "numpy")
      github/pytorch/pytorch    -> (github, "repo", "pytorch/pytorch")
      pytorch/pytorch           -> (github, "repo", "pytorch/pytorch")
    """
    parts = [p for p in (target or "").strip().strip("/").split("/") if p]
    forge = default_forge
    if parts and parts[0].lower() in FORGES:
        forge = parts.pop(0).lower()
    if len(parts) >= 2:
        return forge, "repo", "/".join(parts[:2])
    return forge, "org", (parts[0] if parts else "")


def _token(forge: str) -> str | None:
    if forge != "github":
        return None
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def run_seed(name: str, forge: str = "github", budget: int = 30,
             kind: str = "org", log=lambda m: None) -> dict:
    """Seed the SHARED reverse-index from a target on ``forge``. ``kind`` is
    "org" (seed the org's repos) or "repo" (seed a single owner/repo). Roster
    fetches use an ephemeral local http cache; the index + per-repo seed markers
    live in the shared cache (Redis when configured) that the app reads."""
    if forge not in FORGES:
        return {"target": name, "forge": forge, "seeded": 0,
                "contributors_indexed": 0, "repos_available": 0,
                "stopped": f"unknown forge '{forge}'"}
    shared = make_result_cache()                 # shared Redis (or local fallback)
    f = FORGES[forge](_token(forge), local_cache())  # ephemeral http cache
    index = ContributorIndex(shared)
    try:
        if kind == "repo":
            res = seed_one(name, forge=f, index=index, cache=shared, log=log)
        else:
            res = seed_org(name, forge=f, index=index, cache=shared,
                           budget=budget, log=log)
    finally:
        f.close()
    res["forge"] = forge
    if forge not in SEEDABLE_FORGES and res.get("seeded", 0) == 0:
        res["stopped"] = f"{forge} seeding not supported yet (GitHub-only)"
    from web.core import service       # log the run for the admin "seed status" view
    service.record_seed(res, forge=forge, kind=kind, target=name, result_cache=shared)
    return res


def run_queue(budget: int | None = None, log=lambda m: None) -> dict:
    """Opportunistic background chunk: seed the next org from the admin's list, if
    GitHub REST quota is healthy. Bounded to ONE org (``budget`` repos), guarded by
    a Redis lease so concurrent app sessions don't run it at once. Safe to call on
    every app run — it's a cheap no-op unless a chunk is actually due.

    Returns ``{"ran": bool, "reason"/"org"/…}``. Never raises (best-effort)."""
    from web.core import service
    shared = make_result_cache()
    if shared is None:
        return {"ran": False, "reason": "no shared cache"}
    # Lease: only one seeder at a time; TTL auto-releases if this process dies.
    if hasattr(shared, "acquire_lock") and not shared.acquire_lock(
            service._SEED_LOCK_KEY, SEED_LOCK_TTL):
        return {"ran": False, "reason": "another seeder is running"}
    try:
        org = service.next_seed_target(shared)
        if not org:
            return {"ran": False, "reason": "no targets"}
        if budget is None:
            budget = service.get_seed_budget(shared)
        # Start watermark: only begin a chunk when REST is comfortably high (free
        # /rate_limit read). The per-repo FLOOR (SEED_REST_FLOOR) is enforced
        # inside seed_org, so it backs off mid-chunk if quota drops.
        budget_info = service.rate_budget(_token("github")) or {}
        rest = (budget_info.get("core") or (None,))[0]
        if rest is not None and rest < service.SEED_REST_START:
            return {"ran": False, "reason": f"rest low ({rest})", "org": org}
        f = FORGES["github"](_token("github"), local_cache())
        try:
            res = seed_org(org, forge=f, index=ContributorIndex(shared), cache=shared,
                           budget=budget, min_rest=service.SEED_REST_FLOOR, log=log)
        finally:
            f.close()
        res["forge"] = "github"
        service.record_seed(res, forge="github", kind="org", target=org,
                            result_cache=shared)
        return {"ran": True, "org": org, "seeded": res.get("seeded"),
                "stopped": res.get("stopped"),
                "contributors": res.get("contributors_distinct")}
    except Exception as exc:      # background: never surface
        return {"ran": False, "reason": f"error: {exc}"}
    finally:
        if hasattr(shared, "release_lock"):
            shared.release_lock(service._SEED_LOCK_KEY)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="web.seed",
        description="Seed the web app's SHARED reverse-index from an org's repos "
                    "(run with the deployment's Upstash + bot-token env).")
    p.add_argument("target", help="an org (numpy | github/numpy) or a single "
                                   "repo (github/pytorch/pytorch | pytorch/pytorch)")
    p.add_argument("--forge", default="github",
                   help="default forge when the target omits one (default: "
                        "github; only github is functional today)")
    p.add_argument("--budget", type=int, default=30, metavar="N",
                   help="max repos to seed for an org target (default: 30)")
    args = p.parse_args(argv)
    forge, kind, name = parse_seed_target(args.target, args.forge)
    res = run_seed(name, forge, args.budget, kind, log=lambda m: print(f"[seed] {m}"))
    print(f"[seed] {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

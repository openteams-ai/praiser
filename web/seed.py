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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

from praiser.contribindex import ContributorIndex  # noqa: E402
from praiser.pipeline import FORGES  # noqa: E402
from praiser.seed import seed_one, seed_org  # noqa: E402
from web.core.cache import local_cache, make_result_cache  # noqa: E402

# Background seeder tuning. The lease is renewed by a per-repo heartbeat, so the
# TTL just bounds how long a *dead* run's lease lingers after a crash/reboot (a
# false "already running") — short is better; a live run heartbeats well inside it.
SEED_CHUNK_BUDGET = 30
SEED_LOCK_TTL = 90
SEED_HEARTBEAT_EVERY = 30       # renew the lease at most this often (< TTL)

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


def _rest_now(token) -> int | None:
    """Bot-token REST remaining via the free /rate_limit endpoint (no quota cost)."""
    from web.core import service
    info = service.rate_budget(token) or {}
    return (info.get("core") or (None,))[0]


def run_queue(budget: int | None = None, source: str = "background",
              max_orgs: int | None = None, log=lambda m: None) -> dict:
    """Seed orgs from the admin's list while GitHub REST quota stays healthy.

    Chains through the list — starts only when REST > SEED_REST_START, keeps
    seeding one org at a time while REST > SEED_REST_FLOOR, and stops when quota
    drops, the whole due-list is covered, or ``max_orgs`` is reached (``max_orgs=1``
    = the manual "Seed one now": exactly one org). Guarded by a Redis lease
    (renewed each iteration so a long run can't lapse and spawn a duplicate);
    ``source`` is recorded in the lease so a blocked caller can name the holder.
    Never raises. Returns ``{"ran", "count", "orgs", "results", "reason"}``."""
    from web.core import service
    shared = make_result_cache()
    if shared is None:
        return {"ran": False, "count": 0, "reason": "no shared cache"}
    now = time.time()
    lock_val = {"source": source, "started": now}
    if hasattr(shared, "acquire_lock") and not shared.acquire_lock(
            service._SEED_LOCK_KEY, SEED_LOCK_TTL, value=lock_val):
        held = {}
        try:
            held = shared.get(service._SEED_LOCK_KEY) or {}
        except Exception:
            pass
        if isinstance(held, dict) and held.get("started"):
            ago = max(0, int(now - held["started"]))
            who = held.get("source", "another")
            return {"ran": False, "count": 0,
                    "reason": f"a {who} seeder is already running (started {ago}s ago)"}
        return {"ran": False, "count": 0, "reason": "another seeder is running"}
    results: list[dict] = []
    reason = "done"
    token = _token("github")
    try:
        if budget is None:
            budget = service.get_seed_budget(shared)
        # Throttled lease heartbeat: renews at most every SEED_HEARTBEAT_EVERY,
        # called per-org and (via seed_org) per-repo — so a live run keeps the
        # lease fresh regardless of org size, while a dead run stops heartbeating
        # and the lease expires within TTL (bounding a stale "already running").
        beat_at = [now]

        def _beat():
            t = time.time()
            if t - beat_at[0] >= SEED_HEARTBEAT_EVERY and hasattr(shared, "renew_lock"):
                beat_at[0] = t
                shared.renew_lock(service._SEED_LOCK_KEY, SEED_LOCK_TTL, value=lock_val)

        f = FORGES["github"](token, local_cache())
        try:
            seen: list[str] = []
            while max_orgs is None or len(results) < max_orgs:
                # Hysteresis: require the high watermark to START, the low one to
                # CONTINUE — so one healthy window seeds as much as it can, then
                # backs off, and re-starts only once quota recovers well above it.
                threshold = service.SEED_REST_START if not results else service.SEED_REST_FLOOR
                rest = _rest_now(token)
                if rest is not None and rest < threshold:
                    reason = f"REST {rest} < {threshold}"
                    break
                org = service.next_seed_target(shared)
                if not org:
                    reason = "no targets"
                    break
                if org in seen:               # cycled the whole due-list
                    reason = "all due targets seeded"
                    break
                # Stamp the current org into the lease so the admin sees live
                # progress ("seeding numpy · N done"), renewing immediately.
                lock_val["org"] = org
                lock_val["done"] = len(results)
                if hasattr(shared, "renew_lock"):
                    shared.renew_lock(service._SEED_LOCK_KEY, SEED_LOCK_TTL, value=lock_val)
                    beat_at[0] = time.time()
                res = seed_org(org, forge=f, index=ContributorIndex(shared), cache=shared,
                               budget=budget, min_rest=service.SEED_REST_FLOOR,
                               heartbeat=_beat, log=log)
                res["forge"] = "github"
                service.record_seed(res, forge="github", kind="org", target=org,
                                    result_cache=shared)
                seen.append(org)
                results.append({"org": org, "seeded": res.get("seeded"),
                                "contributors": res.get("contributors_distinct"),
                                "stopped": res.get("stopped")})
        finally:
            f.close()
    except Exception as exc:      # background: never surface
        reason = f"error: {exc}"
    finally:
        try:      # persist the outcome so the idle UI can say completed-vs-paused
            shared.set(service._SEED_LASTRUN_KEY,
                       {"finished": time.time(), "reason": reason, "count": len(results)})
        except Exception:
            pass
        if hasattr(shared, "release_lock"):
            shared.release_lock(service._SEED_LOCK_KEY)
    return {"ran": bool(results), "count": len(results),
            "orgs": [r["org"] for r in results], "results": results, "reason": reason}


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

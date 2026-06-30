"""Orchestrates the phases: identity -> discovery -> attribution -> popularity."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .cache import Cache
from .config import Config
from .discovery import discover, org_logins
from .extractors import ExtractContext, all_extractors
from .github_client import GitHubClient, RateLimitError
from .identity import resolve_identity
from .llm import LLM
from .models import WEAK_ROLES, Evidence, ProjectRecord
from .popularity import enrich_stars, filter_records
from .progress import Progress
from .registry import KnownProjects


@dataclass
class RunResult:
    records: list[ProjectRecord] = field(default_factory=list)
    # Below-threshold but widely-used-and-maintained projects with a real role.
    secondary: list[ProjectRecord] = field(default_factory=list)
    # Seconds until the rate limit resets if the run was cut short, else None.
    partial_reset_in: int | None = None


def _log(config: Config, msg: str) -> None:
    if config.verbose:
        print(f"[ghrecord] {msg}", file=sys.stderr)


def _humanize(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return "shortly"
    if seconds < 90:
        return f"~{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"~{hours}h {mins}min" if mins else f"~{hours}h"


def run(config: Config) -> RunResult:
    cache = Cache(config.cache_dir)
    client = GitHubClient(config.token, cache, verbose=config.verbose)
    registry = KnownProjects.load(config.registry_path)
    llm = LLM.maybe(cache, enabled=config.use_llm)
    _log(config, f"LLM fallback {'enabled' if llm else 'disabled'}")

    # Live progress only when interactive and not in verbose/quiet mode; verbose
    # uses the detailed per-repo _log lines instead.
    progress = Progress(
        enabled=not config.verbose and not config.quiet and sys.stderr.isatty()
    )

    try:
        progress.phase(f"resolving identity for {config.username}…")
        identity = resolve_identity(client, config.username)
        _log(config, f"identity: logins={identity.logins} names={identity.names}")

        progress.phase("discovering candidate repositories…")
        candidates = discover(
            client, identity, registry, include_private=config.include_private
        )
        _log(config, f"discovered {len(candidates)} candidate repos")
        rate0 = client.rate_summary()
        progress.phase(
            f"discovered {len(candidates)} candidate repos"
            + (f" · rate: {rate0}" if rate0 else "")
        )

        ctx = ExtractContext(
            identity=identity, client=client, registry=registry, llm=llm,
            org_logins=org_logins(client, identity.primary_login),
            popularity_floor=config.min_stars,
            contributor_pages=config.contributor_pages,
            auto_discover_roles=config.discover_roles and llm is not None,
        )
        records, reset_in = _attribute(config, candidates, ctx, progress)
        progress.done()
        _log(config, f"{len(records)} repos with elevated-role evidence")

        progress.phase("checking popularity…")
        try:
            enrich_stars(client, records)
        except RateLimitError as exc:
            _log(config, f"rate limit during popularity enrichment: {exc}")
            reset_in = exc.reset_in if reset_in is None else reset_in
        records, secondary = filter_records(
            records, min_stars=config.min_stars, registry=registry
        )
        _log(config, f"{len(records)} primary + {len(secondary)} secondary repos")
        rate1 = client.rate_summary()
        progress.phase(
            f"done — {len(records)} primary project(s)"
            + (f", {len(secondary)} more widely-used" if secondary else "")
            + (f" · rate: {rate1}" if rate1 else "")
        )
        if config.verbose and rate1:
            _log(config, f"rate limit remaining: {rate1}")

        for rec in (*records, *secondary):
            registry.record_popularity(
                rec.name_with_owner, stars=rec.stars, forks=rec.forks
            )
        # Promote web-discovered role sources into the registry so a later
        # --save-registry persists them as curated knowledge.
        discovered = ctx.discovered_sources()
        for name, sources in discovered.items():
            registry.add_role_sources(name, sources)
        if discovered:
            _log(config, f"discovered role sources for {len(discovered)} repo(s)")
        if config.save_registry and config.registry_path:
            registry.save(config.registry_path)
            _log(config, f"saved registry to {config.registry_path}")

        records.sort(key=lambda r: r.score, reverse=True)
        secondary.sort(key=lambda r: r.score, reverse=True)
        return RunResult(
            records=records, secondary=secondary, partial_reset_in=reset_in
        )
    finally:
        client.close()


def _scan_one(extractors, cand, ctx, config) -> list[Evidence]:
    """Run all extractors on one candidate (worker-thread body).

    Network-bound only; touches no shared mutable progress/record state.
    RateLimitError propagates so the orchestrator can stop and report partial.
    """
    evidence: list[Evidence] = []
    for ext in extractors:
        try:
            if ext.applicable(cand, ctx):
                evidence.extend(ext.extract(cand, ctx))
        except RateLimitError:
            raise  # stop the whole run, not just this extractor
        except Exception as exc:  # one extractor failing is non-fatal
            _log(config, f"{ext.name} failed on {cand.name_with_owner}: {exc}")
    return evidence


def _attribute(
    config, candidates, ctx, progress: Progress
) -> tuple[list[ProjectRecord], int | None]:
    """Attribute roles across candidates concurrently (I/O-bound network work).

    Returns (records, reset_in). ``reset_in`` is the seconds-until-reset when a
    rate limit cut the scan short (None otherwise). Workers only do network; the
    progress display and record list are updated solely on this thread.
    """
    extractors = all_extractors()
    records: list[ProjectRecord] = []
    reset_in: int | None = None
    total = len(candidates)
    done = 0

    with ThreadPoolExecutor(max_workers=max(1, config.jobs)) as pool:
        futures = {
            pool.submit(_scan_one, extractors, cand, ctx, config): cand
            for cand in candidates
        }
        try:
            for fut in as_completed(futures):
                cand = futures[fut]
                done += 1
                rate = ctx.client.rate_summary()
                rate_str = f" | {rate}" if rate else ""
                progress.status(
                    f"scanned {done}/{total} ({len(records)} found){rate_str}: "
                    f"{cand.name_with_owner}"
                )
                try:
                    evidence = fut.result()
                except RateLimitError as exc:
                    _log(config, f"rate limit reached, stopping early: {exc}")
                    reset_in = exc.reset_in if exc.reset_in is not None else 0
                    break
                # Keep the repo only if it has at least one non-weak role.
                if any(e.role not in WEAK_ROLES for e in evidence):
                    records.append(ProjectRecord(
                        name_with_owner=cand.name_with_owner,
                        url=cand.url,
                        stars=cand.stars,
                        forks=cand.forks,
                        pushed_at=cand.pushed_at,
                        evidence=evidence,
                    ))
                    _log(config, f"  + {cand.name_with_owner}: "
                                 f"{[f'{e.source}:{e.role}' for e in evidence]}")
        finally:
            for f in futures:  # don't start work we no longer need
                f.cancel()
    return records, reset_in

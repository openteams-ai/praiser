"""Orchestrates the phases: identity -> discovery -> attribution -> popularity."""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial

from .cache import Cache
from .contribindex import ContributorIndex
from .config import Config
from .crossforge import resolve_cross_forge
from .discovery import discover, org_logins
from .extractors import ExtractContext, all_extractors
from .forge import (
    BitbucketForge,
    CgitForge,
    GiteaForge,
    GiteeForge,
    GitHubForge,
    GitLabForge,
)
from .github_client import RateLimitError

# Selectable code hosts. Each value builds a Forge from (token, cache, verbose).
FORGES = {
    "github": GitHubForge,
    "codeberg": GiteaForge,
    "gitlab": GitLabForge,
    "gitee": GiteeForge,
    "cgit": CgitForge,
    "bitbucket": BitbucketForge,
}
# Forges that take a self-hosted instance URL (--forge-url).
INSTANCE_FORGES = {"gitlab", "codeberg", "cgit"}
from .identity import resolve_identity
from .llm import LLM
from .models import WEAK_ROLES, Evidence, Identity, ProjectRecord
from .popularity import enrich_stars, filter_records
from .progress import Progress
from .registries import JSON_ACCEPT, discover_packages, index_by_repo
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
        print(f"[praiser] {msg}", file=sys.stderr)


def humanize_wait(seconds: int | None) -> str:
    """A rate-limit reset delay as a short human string, e.g. "~5 min"."""
    if not seconds or seconds < 0:
        return "shortly"
    if seconds < 90:
        return f"~{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes} min"
    hours, mins = divmod(minutes, 60)
    return f"~{hours}h {mins}min" if mins else f"~{hours}h"


_humanize = humanize_wait  # back-compat alias (CLI, tests)


def _build_forge(forge_name: str, config: Config, cache: Cache, *, is_anchor: bool):
    """Construct a Forge. The anchor gets the user's token + any --forge-url;
    secondary forges (cross-forge) run public/best-effort with no token."""
    cls = FORGES.get(forge_name, GitHubForge)
    kwargs: dict = {"verbose": config.verbose}
    token = config.token if is_anchor else None
    if is_anchor and config.forge_url and forge_name in INSTANCE_FORGES:
        kwargs["base_url"] = config.forge_url
        if config.forge_name:
            kwargs["name"] = config.forge_name
    return cls(token, cache, **kwargs)


def _dedupe(records: list[ProjectRecord]) -> list[ProjectRecord]:
    """Drop duplicate records, keyed on the (forge-specific) URL."""
    seen: set[str] = set()
    out: list[ProjectRecord] = []
    for rec in records:
        if rec.url in seen:
            continue
        seen.add(rec.url)
        out.append(rec)
    return out


def _scan_forge(
    forge, forge_login, base_identity, config, registry, llm, progress,
    *, is_anchor, label="", index=None,
) -> tuple[list[ProjectRecord], list[ProjectRecord], int | None]:
    """Discover + attribute + popularity-filter one forge for one login, using
    the shared (possibly cross-forge-merged) identity for handle/name matching."""
    identity = Identity(
        primary_login=forge_login,
        logins=set(base_identity.logins),
        names=set(base_identity.names),
    )
    # Contributor reverse-index (#59): repos where this user was recorded as a
    # substantial contributor by a prior scan — recovers direct committers with
    # no person-side signal. GitHub-only (rosters are github logins).
    index_repos = []
    if index is not None and forge.name == "github":
        seen = set()
        for lg in identity.logins:
            for r in index.repos_for(lg):
                if r not in seen:
                    seen.add(r)
                    index_repos.append(r)
        if index_repos:
            _log(config, f"{label}reverse-index: {len(index_repos)} candidate repo(s)")
    # Package registries are GitHub-anchored (github_nwo only reads github.com
    # source URLs), so only meaningful on a GitHub anchor scan.
    package_refs = []
    if config.use_package_registries and is_anchor and forge.name == "github":
        pkg_fetch = partial(forge.get_url, accept=JSON_ACCEPT)
        package_refs = discover_packages(pkg_fetch, identity)
        _log(config, f"package registries: {len(package_refs)} package(s) "
                     f"({sum(1 for r in package_refs if r.repo)} on GitHub)")

    progress.phase(f"{label}discovering candidate repositories…")
    candidates = discover(
        forge, identity, registry,
        include_private=config.include_private,
        extra_repos=config.extra_repos if is_anchor else [],
        package_refs=package_refs,
        index_repos=index_repos,
    )
    _log(config, f"{label}discovered {len(candidates)} candidate repos")
    rate0 = forge.rate_summary()
    progress.phase(f"{label}discovered {len(candidates)} candidate repos"
                   + (f" · rate: {rate0}" if rate0 else ""))

    ctx = ExtractContext(
        identity=identity, forge=forge, registry=registry, llm=llm,
        org_logins=org_logins(forge, identity.primary_login),
        popularity_floor=config.min_stars,
        contributor_pages=config.contributor_pages,
        auto_discover_roles=config.discover_roles and llm is not None,
        use_wikidata=config.use_wikidata,
        manual_repos=set(config.extra_repos) if is_anchor else set(),
        manual_subcomponents=config.extra_subcomponents if is_anchor else {},
        package_index=index_by_repo(package_refs),
    )
    records, reset_in = _attribute(config, candidates, ctx, progress)
    _log(config, f"{label}{len(records)} repos with elevated-role evidence")

    # Feed the reverse-index with the rosters we just fetched, so future scans
    # of any substantial contributor to these repos can discover them (#59).
    if index is not None and forge.name == "github":
        try:
            index.record_rosters(ctx.fetched_rosters())
        except Exception:
            pass  # index is best-effort; never break a scan

    progress.phase(f"{label}checking popularity…")
    try:
        enrich_stars(forge, records)
    except RateLimitError as exc:
        _log(config, f"{label}rate limit during popularity enrichment: {exc}")
        reset_in = exc.reset_in if reset_in is None else reset_in
    records, secondary = filter_records(
        records, min_stars=config.min_stars, registry=registry,
        force_primary=set(config.extra_repos) if is_anchor else set(),
    )
    for rec in (*records, *secondary):
        registry.record_popularity(rec.name_with_owner, stars=rec.stars, forks=rec.forks)
    for name, sources in ctx.discovered_sources().items():
        registry.add_role_sources(name, sources)
    return records, secondary, reset_in


def run(config: Config, cache=None, progress_cb=None, index_cache=None) -> RunResult:
    # ``cache`` lets a caller inject a shared/durable backend (e.g. the web UI's
    # Redis cache) so the expensive, option-independent data collection is
    # reused across processes/hosts. Defaults to the local file cache.
    # ``progress_cb(msg)`` receives each phase/status line (e.g. for a web UI),
    # independent of the terminal display.
    cache = cache if cache is not None else Cache(
        config.cache_dir, ttl=config.cache_ttl, refresh=config.refresh
    )
    # Contributor reverse-index (#59) rides the (possibly shared) cache. A caller
    # can inject a different backend (e.g. the web app's shared Redis) via
    # index_cache; defaults to the run cache.
    index = ContributorIndex(index_cache if index_cache is not None else cache)
    registry = KnownProjects.load(config.registry_path)
    llm = LLM.maybe(cache, enabled=config.use_llm)
    _log(config, f"LLM fallback {'enabled' if llm else 'disabled'}")
    progress = Progress(
        enabled=not config.verbose and not config.quiet and sys.stderr.isatty(),
        callback=progress_cb,
    )

    anchor = _build_forge(config.forge, config, cache, is_anchor=True)
    open_forges = {config.forge: anchor}

    def factory(name):
        if name not in open_forges:
            open_forges[name] = _build_forge(name, config, cache, is_anchor=False)
        return open_forges[name]

    try:
        # -- resolve identity/identities ------------------------------------
        if config.cross_forge:
            progress.phase(f"resolving identity across forges for {config.username}…")
            identity, ids = resolve_cross_forge(anchor, config.username, factory)
        else:
            identity = resolve_identity(anchor, config.username)
            ids = [(config.forge, config.username)]
        # Manual additions (forge:login), merged in.
        for spec in config.also_forge:
            fname, _, login = spec.partition(":")
            if fname in FORGES and login and (fname, login) not in ids:
                ids.append((fname, login))
                identity.logins.add(login.lower())
        _log(config, f"identity: logins={identity.logins} names={identity.names}")
        if len(ids) > 1:
            _log(config, f"multi-forge scan across {len(ids)}: {ids}")

        # -- scan each forge, merge -----------------------------------------
        all_records: list[ProjectRecord] = []
        all_secondary: list[ProjectRecord] = []
        reset_in: int | None = None
        multi = len(ids) > 1
        for fname, login in ids:
            forge = factory(fname)
            label = f"[{fname}] " if multi else ""
            recs, sec, r_in = _scan_forge(
                forge, login, identity, config, registry, llm, progress,
                is_anchor=(fname == config.forge and login == config.username),
                label=label, index=index,
            )
            all_records += recs
            all_secondary += sec
            if r_in is not None and reset_in is None:
                reset_in = r_in
        progress.done()

        records = _dedupe(all_records)
        secondary = _dedupe(all_secondary)
        rate1 = anchor.rate_summary()
        progress.phase(
            f"done — {len(records)} primary project(s)"
            + (f", {len(secondary)} more widely-used" if secondary else "")
            + (f" · rate: {rate1}" if rate1 else "")
        )
        if config.save_registry and config.registry_path:
            registry.save(config.registry_path)
            _log(config, f"saved registry to {config.registry_path}")

        records.sort(key=lambda r: r.score, reverse=True)
        secondary.sort(key=lambda r: r.score, reverse=True)
        return RunResult(
            records=records, secondary=secondary, partial_reset_in=reset_in
        )
    finally:
        for f in open_forges.values():
            f.close()


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
                rate = ctx.forge.rate_summary()
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
                        forge_has_stars=ctx.forge.has_stars,
                        evidence=evidence,
                    ))
                    _log(config, f"  + {cand.name_with_owner}: "
                                 f"{[f'{e.source}:{e.role}' for e in evidence]}")
        finally:
            for f in futures:  # don't start work we no longer need
                f.cancel()
    return records, reset_in

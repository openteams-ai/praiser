"""Orchestrates the phases: identity -> discovery -> attribution -> popularity."""

import sys

from .cache import Cache
from .config import Config
from .discovery import discover
from .extractors import ExtractContext, all_extractors
from .github_client import GitHubClient
from .identity import resolve_identity
from .llm import LLM
from .models import WEAK_ROLES, Evidence, ProjectRecord
from .popularity import enrich_stars, filter_records
from .registry import KnownProjects


def _log(config: Config, msg: str) -> None:
    if config.verbose:
        print(f"[ghrecord] {msg}", file=sys.stderr)


def run(config: Config) -> list[ProjectRecord]:
    cache = Cache(config.cache_dir)
    client = GitHubClient(config.token, cache, verbose=config.verbose)
    registry = KnownProjects.load(config.registry_path)
    llm = LLM.maybe(cache, enabled=config.use_llm)
    _log(config, f"LLM fallback {'enabled' if llm else 'disabled'}")

    try:
        identity = resolve_identity(client, config.username)
        _log(config, f"identity: logins={identity.logins} names={identity.names}")

        candidates = discover(client, identity, registry)
        _log(config, f"discovered {len(candidates)} candidate repos")

        ctx = ExtractContext(
            identity=identity, client=client, registry=registry, llm=llm
        )
        records = _attribute(config, candidates, ctx)
        _log(config, f"{len(records)} repos with elevated-role evidence")

        enrich_stars(client, records)
        records = filter_records(
            records, min_stars=config.min_stars, registry=registry
        )
        _log(config, f"{len(records)} repos after popularity filter")

        for rec in records:
            registry.record_popularity(
                rec.name_with_owner, stars=rec.stars, forks=rec.forks
            )
        if config.save_registry and config.registry_path:
            registry.save(config.registry_path)
            _log(config, f"saved registry to {config.registry_path}")

        records.sort(key=lambda r: r.score, reverse=True)
        return records
    finally:
        client.close()


def _attribute(config, candidates, ctx) -> list[ProjectRecord]:
    extractors = all_extractors()
    records: list[ProjectRecord] = []
    for cand in candidates:
        evidence: list[Evidence] = []
        for ext in extractors:
            try:
                if ext.applicable(cand, ctx):
                    evidence.extend(ext.extract(cand, ctx))
            except Exception as exc:  # one extractor failing must not abort the run
                _log(config, f"{ext.name} failed on {cand.name_with_owner}: {exc}")
        # Keep the repo only if it has at least one non-weak role.
        if any(e.role not in WEAK_ROLES for e in evidence):
            records.append(ProjectRecord(
                name_with_owner=cand.name_with_owner,
                url=cand.url,
                stars=cand.stars,
                forks=cand.forks,
                evidence=evidence,
            ))
            _log(config, f"  + {cand.name_with_owner}: "
                         f"{[f'{e.source}:{e.role}' for e in evidence]}")
    return records

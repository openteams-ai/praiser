"""Phase 3 — popularity filter.

Enriches records that lack star counts (registry seeds, code-search hits), then
keeps a project if it is popular enough OR carries a high-signal role that we
don't want to lose on a small-but-notable standards project.
"""

from .github_client import GitHubClient
from .models import (
    MAINTAINER,
    STANDARDS_AUTHOR,
    STEERING_COUNCIL,
    ProjectRecord,
)
from .registry import KnownProjects

# Roles strong enough to survive the star threshold on their own.
HIGH_SIGNAL_ROLES = frozenset({STEERING_COUNCIL, STANDARDS_AUTHOR, MAINTAINER})


def enrich_stars(
    client: GitHubClient, records: list[ProjectRecord]
) -> None:
    """Fill stars/forks for records that don't have them yet (REST, cached)."""
    for rec in records:
        if rec.stars > 0:
            continue
        owner, _, repo = rec.name_with_owner.partition("/")
        data = client.rest_json(f"/repos/{owner}/{repo}")
        if isinstance(data, dict):
            rec.stars = data.get("stargazers_count", 0) or 0
            rec.forks = data.get("forks_count", 0) or 0


def filter_records(
    records: list[ProjectRecord],
    *,
    min_stars: int,
    registry: KnownProjects,
) -> list[ProjectRecord]:
    # A high-signal role lets a project survive at a *reduced* threshold (so a
    # smaller-but-notable standards repo isn't lost) — but NOT at zero stars,
    # which would admit forks/copies that inherit an upstream MAINTAINERS or
    # CODEOWNERS file. Curated small-but-notable projects bypass entirely via
    # the registry's ``min_stars_override``.
    high_signal_floor = max(10, min_stars // 5)
    kept: list[ProjectRecord] = []
    for rec in records:
        known = registry.get(rec.name_with_owner)
        override = bool(known and known.min_stars_override)
        high_signal = (
            rec.role in HIGH_SIGNAL_ROLES
            and rec.confidence >= 0.7
            and rec.stars >= high_signal_floor
        )
        if rec.stars >= min_stars or override or high_signal:
            if known and known.importance:
                rec.importance = known.importance
            kept.append(rec)
    return kept

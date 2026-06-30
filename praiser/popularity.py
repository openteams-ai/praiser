"""Phase 3 — popularity filter.

Enriches records that lack star counts (registry seeds, code-search hits), then
splits the elevated-role records into:

* **primary** — popular enough for the headline record, and
* **secondary** — below the popularity bar but still *widely used and
  maintained* (real fork engagement + recently pushed). These are summarised
  (at minimum a count) so the report doesn't silently drop projects where the
  user holds a real role on a smaller-but-active library.
"""

from datetime import datetime, timezone

from .forge import Forge
from .models import (
    AUTHOR,
    MAINTAINER,
    STANDARDS_AUTHOR,
    STEERING_COUNCIL,
    ProjectRecord,
)
from .registry import KnownProjects

# Roles strong enough to survive the star threshold on their own.
HIGH_SIGNAL_ROLES = frozenset({STEERING_COUNCIL, STANDARDS_AUTHOR, MAINTAINER})

# A secondary project must show real use (forks) and recent maintenance.
SECONDARY_MIN_FORKS = 5
MAINTAINED_MONTHS = 24


def _is_maintained(pushed_at: str | None) -> bool:
    """True if pushed within MAINTAINED_MONTHS. Unknown dates are treated as True."""
    if not pushed_at:
        return True
    try:
        when = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    age_days = (datetime.now(timezone.utc) - when).days
    return age_days <= MAINTAINED_MONTHS * 30


def is_widely_used_and_maintained(rec: ProjectRecord, min_stars: int) -> bool:
    """A below-threshold project that still looks worth recording."""
    used = rec.forks >= SECONDARY_MIN_FORKS or rec.stars >= max(5, min_stars // 5)
    return used and _is_maintained(rec.pushed_at)


def is_notable_authored(rec: ProjectRecord) -> bool:
    """Keep a project the user *authored* even if small/dormant.

    Authorship is a lasting credential, so (unlike the generic secondary check)
    it doesn't require recent maintenance — only some minimal traction, to skip
    throwaway personal repos and personal sites.
    """
    return rec.role == AUTHOR and (rec.stars >= 5 or rec.forks >= 3)


def enrich_stars(forge: Forge, records: list[ProjectRecord]) -> None:
    """Fill stars/forks for records that don't have them yet (cached)."""
    for rec in records:
        if rec.stars > 0:
            continue
        owner, _, repo = rec.name_with_owner.partition("/")
        meta = forge.repository(owner, repo)
        if meta is not None:
            rec.stars = meta.stars
            rec.forks = meta.forks
            rec.pushed_at = meta.pushed_at or rec.pushed_at


def filter_records(
    records: list[ProjectRecord],
    *,
    min_stars: int,
    registry: KnownProjects,
    force_primary: set[str] | None = None,
) -> tuple[list[ProjectRecord], list[ProjectRecord]]:
    """Split records into (primary, secondary).

    A high-signal role lets a project survive at a *reduced* threshold (so a
    smaller-but-notable standards repo isn't lost) — but NOT at zero stars,
    which would admit forks/copies that inherit an upstream MAINTAINERS or
    CODEOWNERS file. Curated small-but-notable projects bypass entirely via the
    registry's ``min_stars_override``. Anything that misses the primary bar but
    is widely-used-and-maintained becomes a secondary record.
    """
    high_signal_floor = max(10, min_stars // 5)
    force_primary = force_primary or set()
    primary: list[ProjectRecord] = []
    secondary: list[ProjectRecord] = []
    for rec in records:
        known = registry.get(rec.name_with_owner)
        if known and known.importance:
            rec.importance = known.importance
        override = bool(known and known.min_stars_override)
        high_signal = (
            rec.role in HIGH_SIGNAL_ROLES
            and rec.confidence >= 0.7
            and rec.stars >= high_signal_floor
        )
        if (rec.name_with_owner in force_primary
                or rec.stars >= min_stars or override or high_signal):
            primary.append(rec)
        elif is_widely_used_and_maintained(rec, min_stars) or is_notable_authored(rec):
            secondary.append(rec)
    return primary, secondary

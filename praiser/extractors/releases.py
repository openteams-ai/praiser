"""Release-manager extractor (issue #79).

Cutting the releases of a popular project is an elevated, high-trust role — the
person the community trusts to ship. GitHub records who published each release
(the release ``author``), so we tally recent release authors and credit the
scanned identity (matched by handle — no name ambiguity) when they authored a
dominant share.

Two automation realities are handled by design:
* **Bot-published releases** (CI: ``github-actions[bot]``, release bots) carry no
  human credit — filtered out — so a fully-automated project simply yields no
  release-manager signal (no false positive).
* **Release-per-merge / continuous deployment** inflates raw release counts, so we
  credit by *share* of human-authored releases, not an absolute count (100 CD
  releases ≠ 100 curated ones).

Gated on popularity (``role_discovery_floor`` — "large project", per the request)
so it's one cached API call for notable candidates only.
"""

import re

from ..models import RELEASE_MANAGER, Evidence
from . import register
from .base import Extractor, ExtractContext

# GitHub renders every GitHub App identity as "name[bot]" — a reliable marker for
# automated (CI/release-bot) publishers, which shouldn't count as a human role.
_BOT_RE = re.compile(r"\[bot\]$", re.I)

# Cutting even a couple of releases is a real, completed act of trust worth
# crediting — so we don't gate on a dominant share. We DO require more than a
# single release, to filter incidental one-offs (and the "every merge is a
# release" CD pattern where many people each author one). Magnitude is reported
# as "(N/M)" rather than used as a cutoff, so a viewer sees 88/100 vs 6/100 (#79).
MIN_RELEASES = 2


def release_standing(
    authors: list[str], identity_logins: set[str]
) -> tuple[int, int] | None:
    """``(mine, total_human)`` release counts, or None if there are no human
    releases. Bot publishers are excluded from both numerator and denominator."""
    humans = [a for a in authors if a and not _BOT_RE.search(a)]
    total = len(humans)
    if total == 0:
        return None
    mine = sum(1 for a in humans if a.lower() in identity_logins)
    return mine, total


def classify(mine: int, total: int) -> float | None:
    """Confidence, or None below the minimal floor. Scales with share so a
    dominant release manager ranks above an occasional one — the count is also
    shown, so magnitude stays visible rather than hidden behind a cutoff."""
    if mine < MIN_RELEASES:
        return None
    share = mine / total
    return round(min(0.9, 0.55 + 0.35 * share), 2)   # 2 releases → ~0.55, all → 0.90


class ReleaseManagerExtractor(Extractor):
    name = "release_manager"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Notable projects only (bounds the extra call; matches "large project").
        return (candidate.stars >= ctx.role_discovery_floor
                and bool(ctx.identity.logins))

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        authors = ctx.forge.repo_release_authors(candidate.owner, candidate.repo)
        if not authors:
            return []
        standing = release_standing(authors, ctx.identity.logins)
        if standing is None:
            return []
        mine, total = standing
        confidence = classify(mine, total)
        if confidence is None:
            return []
        return [Evidence(
            source=self.name, role=RELEASE_MANAGER,
            url=f"{candidate.url}/releases", confidence=confidence,
            detail=f"published {mine} of the last {total} releases",
            releases_authored=mine, releases_total=total,
        )]


register(ReleaseManagerExtractor())

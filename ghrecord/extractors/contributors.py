"""Core-contributor extractor (commit-volume signal).

Being a substantial committer to a *popular* project is an elevated role even
when the person isn't named in any governance/role file — this is how we catch
historical maintainers and authors of major components (e.g. f2py in NumPy)
whose involvement lives in the commit history, not a CODEOWNERS line.

Gated on popularity (stars >= floor) so it both matches the "popular software"
goal and bounds the extra API calls. Uses the cached contributors list, so it
also corroborates other extractors for free.
"""

from ..models import CORE_CONTRIBUTOR, Evidence
from . import register
from .base import Extractor, ExtractContext


# Also scan repos that aren't star-popular but are widely forked/used.
WIDELY_USED_FORKS = 25


def classify(count: int, rank: int) -> float | None:
    """Confidence for a contributor, or None if too minor to count as elevated."""
    if count >= 100 or rank <= 10:
        return 0.8
    if count >= 25 or rank <= 30:
        return 0.6
    return None  # a handful of commits is a plain contributor — skip


class ContributorsExtractor(Extractor):
    name = "contributors"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Worthwhile for star-popular repos and for widely-forked (used) ones,
        # so we also catch core roles on less-popular-but-widely-used projects.
        # A user-vouched (manual) repo is always checked, whatever its size.
        return (
            candidate.name_with_owner in ctx.manual_repos
            or candidate.stars >= max(1, ctx.popularity_floor)
            or candidate.forks >= WIDELY_USED_FORKS
        )

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        # Commit history is copy-vulnerable: a repo that vendored an upstream's
        # history makes the user look like a heavy committer everywhere. Only
        # trust the contributor signal on the user's own/org repos or the
        # canonical (popular/widely-forked) project — never on a small copy.
        if not ctx.trust_role_file(candidate):
            return []
        contribs = ctx.contributors(candidate)
        if not contribs:
            return []
        count = max((contribs.get(h, 0) for h in ctx.identity.logins), default=0)
        if count <= 0:
            return []
        rank = 1 + sum(1 for v in contribs.values() if v > count)
        confidence = classify(count, rank)
        if confidence is None:
            return []
        return [Evidence(
            source=self.name, role=CORE_CONTRIBUTOR,
            url=f"{candidate.url}/graphs/contributors",
            confidence=confidence,
            detail=f"{count} commits (~#{rank} contributor)",
        )]


register(ContributorsExtractor())

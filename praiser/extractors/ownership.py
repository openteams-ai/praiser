"""Ownership extractor — the user authored/created the project.

A non-fork repository under the user's own account is, in the overwhelming
common case, a project they authored and own. That is a stronger and more
accurate role than "core contributor" (which is all the commit-count signal can
say). Forks are excluded (they're someone else's project), and the popularity
filter still decides whether a given owned repo is notable enough to report.
"""

from ..models import AUTHOR, Evidence
from . import register
from .base import Extractor, ExtractContext


class OwnershipExtractor(Extractor):
    name = "ownership"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        if candidate.is_fork:
            return []
        if not ctx.identity.matches_handle(candidate.owner):
            return []
        return [Evidence(
            source=self.name, role=AUTHOR, url=candidate.url, confidence=0.9,
            detail="owns the repository (author/creator)",
        )]


register(OwnershipExtractor())

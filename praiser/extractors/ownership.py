"""Ownership extractor — the user authored/created the project.

Owning a repo's namespace is orthogonal to authorship: you can own a repo you
didn't write (imported/transferred code, a repo a collaborator authored). So
ownership alone is NOT authorship — we require committer corroboration: a
non-fork repo under the user's account that they also commit to. Owning + writing
it → author/creator (a stronger, more accurate role than "core contributor").
Forks are excluded, and the popularity filter decides whether an owned repo is
notable enough to report. (#123/#124 corroboration principle.)
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
        # Ownership ⟂ authorship — require non-fakeable committer attribution.
        try:
            contribs = ctx.contributors(candidate) or {}
        except Exception:
            contribs = {}
        if not any(h in contribs for h in ctx.identity.logins):
            return []
        return [Evidence(
            source=self.name, role=AUTHOR, url=candidate.url, confidence=0.9,
            detail="owns the repository and commits to it (author/creator)",
        )]


register(OwnershipExtractor())

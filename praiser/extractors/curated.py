"""Curated per-person roles from the registry.

Some elevated roles are facts, not conventions — "founded/created this
project", "emeritus BDFL" — that no file or heuristic reliably yields, and that
a page-based ``role_source`` can't express without over-crediting everyone
named on the page. The registry's ``curated_roles`` assert such a role for one
specific ``login``; this extractor emits it only for that person (handle match),
with the curator's citation as evidence.
"""

from ..models import Evidence
from . import register
from .base import Extractor, ExtractContext


class CuratedRolesExtractor(Extractor):
    name = "curated"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        known = ctx.known(candidate.name_with_owner)
        return bool(known and known.curated_roles)

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        known = ctx.known(candidate.name_with_owner)
        if not known:
            return []
        out: list[Evidence] = []
        for cr in known.curated_roles:
            if not ctx.identity.matches_handle(cr.login):
                continue  # scoped to the named person only — no over-attribution
            out.append(Evidence(
                source=self.name,
                role=cr.role,
                url=cr.url or candidate.url,
                confidence=0.95,  # human-curated with a citation
                detail=cr.label or f"curated: {cr.role}",
            ))
        return out


register(CuratedRolesExtractor())

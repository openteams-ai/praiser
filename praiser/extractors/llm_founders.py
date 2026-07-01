"""LLM-based founder/creator detection (issue #23).

The fallback for founders that structured sources miss — e.g. SciPy's Wikidata
item has the repo link but no creator claim, and the 2001 founders aren't the
current top committers, so neither Wikidata (#24) nor commit/file signals surface
them. An LLM with web search can read the project's history and name the
original author(s).

Only credits the *scanned* identity (matched by handle, or by full name as a
weaker fallback) — so it never over-attributes to others named alongside. Gated
on ``--discover-roles`` + an available LLM + popularity, and cached, like the
web_roles auto discovery.
"""

from ..models import AUTHOR, Evidence
from . import register
from .base import Extractor, ExtractContext


class LlmFoundersExtractor(Extractor):
    name = "llm_founders"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        return (
            ctx.auto_discover_roles
            and ctx.llm is not None
            and candidate.stars >= ctx.role_discovery_floor
            and bool(ctx.identity.logins or ctx.identity.names)
        )

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        try:
            founders = ctx.llm.discover_founders(candidate.name_with_owner)
        except Exception:
            return []
        for f in founders or []:
            handle, name = f.get("handle"), f.get("name")
            url = f.get("url") or candidate.url
            if handle and ctx.identity.matches_handle(handle):
                return [Evidence(
                    source=self.name, role=AUTHOR, url=url, confidence=0.75,
                    detail="named as founder/creator (LLM + web search)")]
            if name and ctx.identity.matches_name(name):
                return [Evidence(
                    source=self.name, role=AUTHOR, url=url, confidence=0.6,
                    detail="named as founder/creator by name (LLM + web search)")]
        return []


register(LlmFoundersExtractor())

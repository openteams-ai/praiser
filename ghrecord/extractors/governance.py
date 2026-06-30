"""Governance-prose extractor (GOVERNANCE.md / STEERING.md / council pages).

Unstructured prose, so: a regex/keyword pass first, and the LLM (Claude) only
as a fallback when a person is mentioned but their *role* is ambiguous. The LLM
is gated behind the heuristic to keep cost down and is skipped entirely when
``ctx.llm`` is None.
"""

import re
from dataclasses import dataclass

from ..models import MAINTAINER, STEERING_COUNCIL, Evidence
from . import register
from .base import Extractor, ExtractContext

GOVERNANCE_PATHS = [
    "GOVERNANCE.md", "GOVERNANCE.rst", "docs/GOVERNANCE.md",
    "STEERING.md", "docs/source/governance.rst", ".github/GOVERNANCE.md",
    "governance.md",
]

_COUNCIL_KW = re.compile(
    r"steering council|steering committee|technical (?:steering )?committee|"
    r"\btsc\b|core team|council member|governing board|bdfl|project lead",
    re.I,
)
_MAINT_KW = re.compile(r"maintainer|core developer|core contributor|lead", re.I)
_HANDLE_RE = re.compile(r"@([A-Za-z0-9-]+)")


@dataclass
class GovMatch:
    role: str
    confidence: float
    detail: str
    ambiguous: bool  # True -> a good candidate for LLM confirmation


def governance_match(text: str, logins: set[str], names: set[str]) -> GovMatch | None:
    """Heuristic scan. Returns the best match or None.

    Strong: an @handle we own appears on a line with a role keyword.
    Weak/ambiguous: handle with no keyword, or a bare name near a keyword.
    """
    lines = text.splitlines()
    blocks = _windows(lines)
    best: GovMatch | None = None

    for window in blocks:
        joined = " ".join(window).strip()
        handles = {h.lower() for h in _HANDLE_RE.findall(joined)}
        owns_handle = bool(handles & logins)
        has_name = any(n in joined.lower() for n in names if len(n) > 3)
        if not (owns_handle or has_name):
            continue

        council = bool(_COUNCIL_KW.search(joined))
        maint = bool(_MAINT_KW.search(joined))

        if owns_handle and council:
            cand = GovMatch(STEERING_COUNCIL, 0.7, "handle near council keyword", False)
        elif owns_handle and maint:
            cand = GovMatch(MAINTAINER, 0.65, "handle near maintainer keyword", False)
        elif owns_handle:
            cand = GovMatch(STEERING_COUNCIL, 0.45, "handle in governance doc", True)
        elif has_name and council:
            cand = GovMatch(STEERING_COUNCIL, 0.45, "name near council keyword", True)
        elif has_name and maint:
            cand = GovMatch(MAINTAINER, 0.4, "name near maintainer keyword", True)
        else:
            continue

        if best is None or cand.confidence > best.confidence:
            best = cand
    return best


def _windows(lines: list[str], size: int = 3) -> list[list[str]]:
    """Overlapping line windows so a name and its role keyword can be a line apart."""
    if not lines:
        return []
    return [lines[i:i + size] for i in range(len(lines))]


class GovernanceExtractor(Extractor):
    name = "governance"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        for path in GOVERNANCE_PATHS:
            text = ctx.client.get_file(candidate.owner, candidate.repo, path)
            if text is None:
                continue
            match = governance_match(text, ctx.identity.logins, ctx.identity.names)
            if match is None:
                return []
            url = f"{candidate.url}/blob/HEAD/{path}"
            if match.ambiguous and ctx.llm is not None:
                match = self._llm_refine(ctx, text, path, match)
            return [Evidence(
                source=self.name, role=match.role, url=url,
                confidence=match.confidence, detail=f"{match.detail} ({path})",
            )]
        return []

    def _llm_refine(self, ctx, text, path, match: GovMatch) -> GovMatch:
        """Ask the LLM to confirm the role; bump or keep confidence."""
        try:
            verdict = ctx.llm.classify_governance_role(  # type: ignore[attr-defined]
                text=text,
                names=sorted(ctx.identity.names),
                logins=sorted(ctx.identity.logins),
            )
        except Exception:
            return match
        if not verdict or not verdict.get("has_role"):
            # LLM says no real role -> keep weak, do not promote.
            return match
        role = verdict.get("role") or match.role
        conf = max(match.confidence, float(verdict.get("confidence", 0.6)))
        return GovMatch(role=role, confidence=min(0.85, conf),
                        detail=match.detail + " [llm-confirmed]", ambiguous=False)


register(GovernanceExtractor())

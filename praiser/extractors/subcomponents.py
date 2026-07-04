"""Subcomponent-level contribution detection via commit-path analysis.

A person can be a substantial contributor to a *part* of a large monorepo
without ranking highly on the whole — e.g. f2py inside NumPy, sparse tensors in
PyTorch, pyarrow inside Apache Arrow. The whole-repo contributor signal dilutes
this. Here we count the user's commits touching a specific path and, if
substantial, credit them as a **core contributor** to that subcomponent.

Commit volume establishes *contribution*, not *authorship* or *maintainership*:
authoring is providing the first implementation, and stewardship is authority —
neither of which many commits proves (someone can commit heavily to f2py for
years without being its author, who is Pearu Peterson). So this extractor never
grants author/maintainer from commit count; those need an authorship/authority
signal (first commits, credits, governance pages) — see issue tracking.

Subcomponents come from the registry (curated, e.g. numpy -> f2py) or from the
CLI (`--add-repo owner/repo:path`).
"""

from ..models import CORE_CONTRIBUTOR, Evidence
from . import register
from .base import Extractor, ExtractContext

MIN_PATH_COMMITS = 5


def path_confidence(count: int) -> float | None:
    if count >= 50:
        return 0.85
    if count >= 15:
        return 0.7
    if count >= MIN_PATH_COMMITS:
        return 0.55
    return None


class SubcomponentsExtractor(Extractor):
    name = "subcomponents"

    def _targets(self, candidate, ctx):
        """(path, label) pairs from the registry + manual vouches. The registry's
        per-subcomponent ``role`` is intentionally NOT used to grant author/
        maintainer here — commit volume only supports a contribution role."""
        out: list[tuple[str, str]] = []
        known = ctx.known(candidate.name_with_owner)
        if known:
            for s in known.subcomponents:
                out.append((s.path, s.label or s.path))
        for path in ctx.manual_subcomponents.get(candidate.name_with_owner, []):
            out.append((path, path))
        return out

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        evidence: list[Evidence] = []
        for path, label in self._targets(candidate, ctx):
            count = max(
                (ctx.forge.path_commit_count(
                    candidate.owner, candidate.repo, path, h)
                 for h in ctx.identity.logins),
                default=0,
            )
            confidence = path_confidence(count)
            if confidence is None:
                continue
            # Commits to a path = contribution to that part, never authorship.
            evidence.append(Evidence(
                source=self.name, role=CORE_CONTRIBUTOR,
                url=f"{candidate.url}/commits/HEAD/{path}",
                confidence=confidence,
                detail=f"{count} commits to {label}",
                qualifier=label,  # scopes the contribution, e.g. "f2py"
                contributions=count,
            ))
        return evidence


register(SubcomponentsExtractor())

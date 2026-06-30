"""Subcomponent-level role detection via commit-path analysis.

A person can lead or author a *part* of a large monorepo without being a
top-ranked contributor to the whole — e.g. f2py inside NumPy, sparse tensors in
PyTorch, pyarrow inside Apache Arrow. The whole-repo contributor signal dilutes
this. Here we count the user's commits touching a specific path and, if
substantial, grant the configured role for that subcomponent.

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
        """(path, role, label) triples from the registry + manual vouches."""
        out: list[tuple[str, str, str]] = []
        known = ctx.known(candidate.name_with_owner)
        if known:
            for s in known.subcomponents:
                out.append((s.path, s.role, s.label or s.path))
        for path in ctx.manual_subcomponents.get(candidate.name_with_owner, []):
            out.append((path, CORE_CONTRIBUTOR, path))
        return out

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        evidence: list[Evidence] = []
        for path, role, label in self._targets(candidate, ctx):
            count = max(
                (ctx.client.path_commit_count(
                    candidate.owner, candidate.repo, path, h)
                 for h in ctx.identity.logins),
                default=0,
            )
            confidence = path_confidence(count)
            if confidence is None:
                continue
            evidence.append(Evidence(
                source=self.name, role=role,
                url=f"{candidate.url}/commits/HEAD/{path}",
                confidence=confidence,
                detail=f"{count} commits to {label}",
            ))
        return evidence


register(SubcomponentsExtractor())

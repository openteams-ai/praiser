"""Extractor interface + shared context.

Each extractor maps one role-recording convention (CODEOWNERS, MAINTAINERS,
package manifests, enhancement-proposal series, governance prose) to a list of
``Evidence``. The *parsing* logic of every extractor is kept in a module-level
pure function so it can be unit-tested offline with no network.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..github_client import GitHubClient
from ..models import Evidence, Identity
from ..registry import KnownProject, KnownProjects


@dataclass
class ExtractContext:
    """Everything an extractor needs at run time."""

    identity: Identity
    client: GitHubClient
    registry: KnownProjects
    llm: object | None = None  # ghrecord.llm.LLM or None when disabled

    def known(self, name_with_owner: str) -> KnownProject | None:
        return self.registry.get(name_with_owner)


class Extractor(ABC):
    name: str = "base"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        """Cheap pre-check; default True (extract decides definitively)."""
        return True

    @abstractmethod
    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        """Return evidence that ctx.identity holds a role in ``candidate``."""
        raise NotImplementedError

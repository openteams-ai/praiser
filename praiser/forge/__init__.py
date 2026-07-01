"""Forge layer: praiser's neutral interface to code-hosting platforms.

``Forge`` (in ``base``) is the interface; each platform is a subclass. The rest
of praiser depends only on these names, never on a concrete platform.
"""

from .base import (
    ContributorCount,
    DirEntry,
    FileHit,
    Forge,
    RepoMeta,
    UserRef,
)
from .gitea import GiteaForge
from .github import GitHubForge
from .gitlab import GitLabForge

__all__ = [
    "Forge",
    "GitHubForge",
    "GiteaForge",
    "GitLabForge",
    "RepoMeta",
    "UserRef",
    "DirEntry",
    "FileHit",
    "ContributorCount",
]

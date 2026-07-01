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
from .cgit import CgitForge
from .gitea import GiteaForge
from .gitee import GiteeForge
from .github import GitHubForge
from .gitlab import GitLabForge

__all__ = [
    "Forge",
    "GitHubForge",
    "GiteaForge",
    "GiteeForge",
    "GitLabForge",
    "CgitForge",
    "RepoMeta",
    "UserRef",
    "DirEntry",
    "FileHit",
    "ContributorCount",
]

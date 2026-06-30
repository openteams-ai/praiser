"""Runtime configuration: token, thresholds, paths."""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "praiser"


def default_registry_path() -> Path:
    # The learned/curated registry lives in a data dir (not the cache, which is
    # safe to wipe) so discovered role sources and popularity persist.
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "praiser" / "known_projects.json"


def resolve_token(explicit: str | None) -> tuple[str | None, str]:
    """Return (token, source). source is one of: flag, env, gh, none."""
    if explicit:
        return explicit, "flag"
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val, "env"
    gh = _gh_cli_token()
    if gh:
        return gh, "gh"
    return None, "none"


def _gh_cli_token() -> str | None:
    """Fall back to the GitHub CLI's token if `gh` is installed and logged in."""
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    token = out.stdout.strip()
    return token or None


@dataclass
class Config:
    username: str
    token: str | None = None
    min_stars: int = 50
    fmt: str = "md"                 # "md" | "json"
    highlights: int | None = None   # if set, print only the top-N highlights
    cache_dir: Path | None = None
    use_llm: bool = True
    registry_path: Path | None = None   # user known-projects file (defaults below)
    save_registry: bool = True          # persist learned popularity + role sources
    verbose: bool = False
    quiet: bool = False                  # suppress the default progress display
    include_private: bool = False        # scan private repos too (default: skip)
    contributor_pages: int = 2           # contributors API pages (100 each)
    jobs: int = 8                        # concurrent candidates during attribution
    discover_roles: bool = True          # find role pages via LLM + web search
    extra_repos: list[str] = field(default_factory=list)  # user-supplied owner/repo
    # user-supplied subcomponents: owner/repo -> [paths]
    extra_subcomponents: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cache_dir is None:
            self.cache_dir = default_cache_dir()
        self.cache_dir = Path(self.cache_dir)
        self.registry_path = (
            default_registry_path() if self.registry_path is None
            else Path(self.registry_path)
        )

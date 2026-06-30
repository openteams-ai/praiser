"""Runtime configuration: token, thresholds, paths."""

import os
from dataclasses import dataclass
from pathlib import Path


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "ghrecord"


def resolve_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val
    return None


@dataclass
class Config:
    username: str
    token: str | None = None
    min_stars: int = 50
    fmt: str = "md"                 # "md" | "json"
    cache_dir: Path | None = None
    use_llm: bool = True
    registry_path: Path | None = None   # extra/user known-projects file
    save_registry: bool = False         # write learned popularity back
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.cache_dir is None:
            self.cache_dir = default_cache_dir()
        self.cache_dir = Path(self.cache_dir)
        if self.registry_path is not None:
            self.registry_path = Path(self.registry_path)

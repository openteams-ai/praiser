"""Known-projects registry.

A persistent, human-editable JSON file of popular/important projects. For each
project it stores:

* **role_conventions** — heuristics describing *how this project records roles*
  (which extractor + path + header format defines a maintainer/owner/author).
  Extractors consult these so they can parse directly instead of re-detecting,
  and so curated knowledge (e.g. "python/peps uses RST author headers") is
  reusable.
* **popularity** — cached/curated metrics (stars, forks, downloads) plus an
  ``min_stars_override`` flag so high-signal but small standards projects
  survive the popularity filter.
* **importance** — a human label ("critical"/"high"/...).
* **aliases** — alternative ``owner/repo`` spellings (renames, mirrors).

The shipped seed lives in ``praiser/data/known_projects.json``. A user file
(``--registry``) is merged on top, and learned popularity can be written back
with ``--save-registry``.
"""

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class RoleConvention:
    """How one role is recorded in a project."""

    extractor: str                 # extractor name, e.g. "enhancement_proposals"
    role: str                      # role constant this convention establishes
    path: str | None = None        # file/dir the extractor should read
    header_format: str | None = None  # "rst" | "yaml" (proposal series)
    weight: float | None = None    # optional per-project role-weight override

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoleConvention":
        return cls(
            extractor=d["extractor"],
            role=d["role"],
            path=d.get("path"),
            header_format=d.get("header_format"),
            weight=d.get("weight"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"extractor": self.extractor, "role": self.role}
        if self.path is not None:
            out["path"] = self.path
        if self.header_format is not None:
            out["header_format"] = self.header_format
        if self.weight is not None:
            out["weight"] = self.weight
        return out


@dataclass
class RoleSource:
    """An authoritative web page that lists people holding a role.

    e.g. a project's team / governance / maintainers page. The web_roles
    extractor fetches the URL and matches the user by GitHub handle or name.
    """

    url: str
    role: str                  # role granted to people listed on the page
    label: str | None = None   # human label for the evidence, e.g. "NumPy team"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoleSource":
        return cls(url=d["url"], role=d["role"], label=d.get("label"))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.url, "role": self.role}
        if self.label is not None:
            out["label"] = self.label
        return out


@dataclass
class CuratedRole:
    """A hand-curated role for a *specific* person on a project.

    Unlike ``RoleSource`` (a web page matched by name/handle across everyone
    listed), this is scoped to one ``login`` — so it can assert a fact like
    "founded/created this project" without over-crediting others. ``url`` is a
    citation for the claim.
    """

    login: str
    role: str
    url: str | None = None
    label: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CuratedRole":
        return cls(login=d["login"], role=d["role"],
                   url=d.get("url"), label=d.get("label"))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"login": self.login, "role": self.role}
        if self.url is not None:
            out["url"] = self.url
        if self.label is not None:
            out["label"] = self.label
        return out


@dataclass
class Subcomponent:
    """A subdirectory/path of a repo that the user may lead or have authored.

    The subcomponents extractor counts the user's commits touching ``path`` and,
    if substantial, grants ``role`` (e.g. author of f2py inside numpy).
    """

    path: str
    role: str
    label: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Subcomponent":
        return cls(path=d["path"], role=d["role"], label=d.get("label"))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "role": self.role}
        if self.label is not None:
            out["label"] = self.label
        return out


@dataclass
class KnownProject:
    name_with_owner: str
    importance: str | None = None
    aliases: list[str] = field(default_factory=list)
    role_conventions: list[RoleConvention] = field(default_factory=list)
    role_sources: list[RoleSource] = field(default_factory=list)
    curated_roles: list[CuratedRole] = field(default_factory=list)
    subcomponents: list[Subcomponent] = field(default_factory=list)
    popularity: dict[str, Any] = field(default_factory=dict)
    # English Wikipedia article title, when known. Lets the wikipedia-authors
    # extractor skip the Wikidata Query Service (which throttles cloud IPs, #108)
    # for the repo→page mapping and read the infobox straight from the reachable
    # Wikipedia API. A stable, factual repo→page fact (not per-person data).
    wikipedia: str = ""
    notes: str = ""

    @property
    def min_stars_override(self) -> bool:
        return bool(self.popularity.get("min_stars_override"))

    @property
    def contributor_count(self) -> int | None:
        """Curated/cached total-contributors snapshot (``popularity['contributors']``)."""
        n = self.popularity.get("contributors")
        return int(n) if isinstance(n, (int, float)) and n > 0 else None

    def conventions_for(self, extractor: str) -> list[RoleConvention]:
        return [c for c in self.role_conventions if c.extractor == extractor]

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "KnownProject":
        return cls(
            name_with_owner=name,
            importance=d.get("importance"),
            aliases=list(d.get("aliases", [])),
            role_conventions=[
                RoleConvention.from_dict(c) for c in d.get("role_conventions", [])
            ],
            role_sources=[
                RoleSource.from_dict(s) for s in d.get("role_sources", [])
            ],
            curated_roles=[
                CuratedRole.from_dict(c) for c in d.get("curated_roles", [])
            ],
            subcomponents=[
                Subcomponent.from_dict(s) for s in d.get("subcomponents", [])
            ],
            popularity=dict(d.get("popularity", {})),
            wikipedia=d.get("wikipedia", ""),
            notes=d.get("notes", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.importance:
            out["importance"] = self.importance
        if self.aliases:
            out["aliases"] = self.aliases
        if self.role_conventions:
            out["role_conventions"] = [c.to_dict() for c in self.role_conventions]
        if self.role_sources:
            out["role_sources"] = [s.to_dict() for s in self.role_sources]
        if self.curated_roles:
            out["curated_roles"] = [c.to_dict() for c in self.curated_roles]
        if self.subcomponents:
            out["subcomponents"] = [s.to_dict() for s in self.subcomponents]
        if self.popularity:
            out["popularity"] = self.popularity
        if self.wikipedia:
            out["wikipedia"] = self.wikipedia
        if self.notes:
            out["notes"] = self.notes
        return out


class KnownProjects:
    """In-memory view over one or more registry files."""

    def __init__(self, projects: dict[str, KnownProject] | None = None) -> None:
        self.projects: dict[str, KnownProject] = projects or {}
        # alias -> canonical name_with_owner
        self._alias_index: dict[str, str] = {}
        self._reindex()

    def _reindex(self) -> None:
        self._alias_index = {}
        for name, proj in self.projects.items():
            self._alias_index[name.lower()] = name
            for alias in proj.aliases:
                self._alias_index[alias.lower()] = name

    # -- loading ------------------------------------------------------------
    @staticmethod
    def _parse(data: dict[str, Any]) -> dict[str, KnownProject]:
        return {
            name: KnownProject.from_dict(name, d)
            for name, d in (data.get("projects") or {}).items()
        }

    @classmethod
    def load(cls, extra_path: Path | str | None = None) -> "KnownProjects":
        """Load the shipped seed, then merge an optional user file on top."""
        text = resources.files("praiser.data").joinpath(
            "known_projects.json"
        ).read_text(encoding="utf-8")
        projects = cls._parse(json.loads(text))

        if extra_path is not None:
            p = Path(extra_path)
            if p.exists():
                user = cls._parse(json.loads(p.read_text(encoding="utf-8")))
                for name, uproj in user.items():
                    base = projects.get(name)
                    projects[name] = cls.merge_project(base, uproj) if base else uproj
        return cls(projects)

    @staticmethod
    def merge_project(base: KnownProject, overlay: KnownProject) -> KnownProject:
        """Field-level overlay of a user entry onto a seed entry.

        The learned/user file often carries only observed popularity, so a plain
        replace would wipe the seed's curated role_sources / curated_roles / etc.
        ``to_dict`` omits empty fields, so ``{**base, **overlay}`` keeps seed
        fields the overlay doesn't set and lets the overlay win where it does.
        """
        return KnownProject.from_dict(
            base.name_with_owner, {**base.to_dict(), **overlay.to_dict()}
        )

    # -- lookup -------------------------------------------------------------
    def get(self, name_with_owner: str) -> KnownProject | None:
        canon = self._alias_index.get(name_with_owner.lower())
        return self.projects.get(canon) if canon else None

    def __contains__(self, name_with_owner: str) -> bool:
        return name_with_owner.lower() in self._alias_index

    def seeds(self) -> list[KnownProject]:
        """All known projects, used by discovery to seed candidates."""
        return list(self.projects.values())

    # -- updating / persistence --------------------------------------------
    def record_popularity(
        self, name_with_owner: str, *, stars: int, forks: int,
        contributors: int | None = None,
    ) -> None:
        """Cache observed popularity for a project (creates an entry if new).

        ``contributors`` snapshots the total contributor count so a later scan
        can show the real ``N`` for a big project without re-deriving it (and so
        the shipped registry can carry curated counts for repos where the live
        API caps the number). Only stored when given (a resolved, non-capped
        total)."""
        proj = self.get(name_with_owner)
        if proj is None:
            proj = KnownProject(name_with_owner=name_with_owner)
            self.projects[name_with_owner] = proj
            self._reindex()
        proj.popularity["stars"] = stars
        proj.popularity["forks"] = forks
        if contributors:
            proj.popularity["contributors"] = int(contributors)

    def contributor_count(self, name_with_owner: str) -> int | None:
        """Curated/cached total-contributors snapshot for a project, if any."""
        proj = self.get(name_with_owner)
        return proj.contributor_count if proj else None

    def wikipedia_title(self, name_with_owner: str) -> str | None:
        """Curated English Wikipedia article title for a project, if any."""
        proj = self.get(name_with_owner)
        return (proj.wikipedia or None) if proj else None

    def add_role_sources(
        self, name_with_owner: str, sources: list[dict[str, Any]]
    ) -> None:
        """Merge web-discovered role sources into a project (dedup by URL)."""
        proj = self.get(name_with_owner)
        if proj is None:
            proj = KnownProject(name_with_owner=name_with_owner)
            self.projects[name_with_owner] = proj
            self._reindex()
        have = {s.url for s in proj.role_sources}
        for s in sources:
            url = s.get("url")
            if not url or url in have:
                continue
            proj.role_sources.append(RoleSource(
                url=url, role=s.get("role", "maintainer"), label=s.get("label")
            ))
            have.add(url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "_schema": f"praiser known-projects registry v{SCHEMA_VERSION}",
            "projects": {
                name: proj.to_dict() for name, proj in sorted(self.projects.items())
            },
        }

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

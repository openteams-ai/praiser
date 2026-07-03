"""The Forge interface — praiser's neutral view of a code-hosting platform.

A *forge* is a code host: GitHub, GitLab, Codeberg/Gitea, Bitbucket. They all
offer the same essentials (repos, files, contributors, role files) but each
speaks a different API dialect. This module defines the set of *operations*
praiser needs, in forge-neutral terms, as an abstract base class. Each real
platform is a subclass that implements these operations its own way (GitHub via
GraphQL+REST, Gitea via plain REST, …); the rest of praiser only ever talks to
this interface, so adding a platform never touches discovery or the extractors.

Design notes
------------
* **Semantic, not transport.** Methods describe *intent* ("this user's repos"),
  never how it's fetched. GitHub's GraphQL queries live inside ``GitHubForge``,
  not here.
* **Neutral data types.** Operations return the small dataclasses below
  (``RepoMeta``, ``UserRef``, …) instead of a platform's raw JSON, so callers
  don't depend on GitHub's field names.
* **Graceful degradation.** Only a handful of methods are abstract (every forge
  must provide them). The rest have safe defaults — a forge that lacks, say,
  code search just inherits the empty default and discovery finds a bit less
  there, rather than the whole platform being unsupported.
"""

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


# --- Neutral data types ----------------------------------------------------
@dataclass
class RepoMeta:
    """Platform-independent facts about one repository."""

    name_with_owner: str          # "owner/repo"
    stars: int = 0
    forks: int = 0
    is_fork: bool = False
    is_private: bool = False
    pushed_at: str | None = None  # ISO-8601 of last push (maintenance signal)
    parent: str | None = None     # if a fork, the canonical upstream "owner/repo"


@dataclass
class UserRef:
    """A resolved account: its canonical handle and display name."""

    login: str
    name: str | None = None


@dataclass
class DirEntry:
    """One entry in a directory listing."""

    name: str
    path: str
    is_dir: bool


@dataclass
class FileHit:
    """A search match: a repo and the file the match was found in."""

    name_with_owner: str
    path: str


@dataclass
class ContributorCount:
    """A contributor and how many contributions the forge attributes to them."""

    login: str
    contributions: int


class Forge(ABC):
    """The operations praiser needs from a code-hosting platform.

    Subclass this for each platform. Support comes in tiers, so a host is never
    all-or-nothing:

    * **Required core** (``@abstractmethod``): ``web_url``, ``get_file``,
      ``list_dir``, ``repository``, ``get_url`` — enough to fetch files and
      attribute file-based roles on repos named via ``--add-repo``.
    * **Discovery** (``resolve_user``, ``user_repositories``, orgs, history,
      search): override to find a user's repos automatically. Hosts without a
      user API inherit safe defaults and run ``--add-repo``-driven.
    * **Analytics** (``repo_contributors``, ``merged_pr_count``, …): override
      where the host exposes them cheaply; otherwise defaults mean "unknown".

    ``owner``/``repo`` are the two halves of an ``"owner/repo"`` slug (``repo``
    keeps everything after the first ``/``, so nested paths survive). Methods
    return ``None``/empty for "not found" rather than raising, except that a
    forge may raise its own rate-limit error to stop a run (the pipeline catches
    it and reports partial results).
    """

    #: Short identifier for this platform (e.g. "github"). Stamped onto every
    #: discovered repo so its web link is built for the right host. Must match a
    #: key in ``praiser.models.FORGE_WEB_HOSTS``.
    name: str = "forge"

    #: Whether this host exposes a star metric. When False (cgit, Gerrit, …),
    #: ``RepoMeta.stars`` is meaningless and ranking/filtering fall back to forks
    #: as the popularity signal (see ``ProjectRecord.popularity``).
    has_stars: bool = True

    #: This instance's web host (e.g. https://gitlab.gnome.org). Stamped onto
    #: candidates so record links point at the actual instance, not a static
    #: guess from ``name`` — essential for self-hosted instances. Set per
    #: instance by forges that accept a ``base_url``.
    web_base: str = ""

    # -- web identity -------------------------------------------------------
    @abstractmethod
    def web_url(self, name_with_owner: str) -> str:
        """The human-facing URL for a repo (e.g. https://github.com/owner/repo).

        This is how we stop hardcoding ``github.com`` — each forge knows its own
        web host, so a record's clickable link comes from here.
        """

    # -- files (the portable core) ------------------------------------------
    @abstractmethod
    def get_file(
        self, owner: str, repo: str, path: str, ref: str | None = None
    ) -> str | None:
        """Raw text of a file, or None if it doesn't exist. ``ref`` = branch/tag."""

    def get_files(
        self, owner: str, repo: str, paths: list[str], ref: str | None = None
    ) -> dict[str, str | None]:
        """{path: text-or-None} for several files.

        Default fetches concurrently (extractors probe many candidate paths, and
        forges without a batch API — Gitea, GitLab — would otherwise crawl
        through them one blocking request at a time). A forge with true batch
        fetch (e.g. GitHub GraphQL) should override for a single round-trip.
        """
        if not paths:
            return {}
        if len(paths) == 1:
            return {paths[0]: self.get_file(owner, repo, paths[0], ref)}
        workers = min(8, len(paths))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            texts = pool.map(lambda p: self.get_file(owner, repo, p, ref), paths)
            return dict(zip(paths, texts))

    @abstractmethod
    def list_dir(self, owner: str, repo: str, path: str) -> list[DirEntry]:
        """Entries directly under ``path`` (empty if missing or not a directory)."""

    # -- repository metadata ------------------------------------------------
    @abstractmethod
    def repository(self, owner: str, repo: str) -> RepoMeta | None:
        """Stars/forks/fork-flag/last-push for one repo, or None if inaccessible."""

    def repositories_metadata(
        self, names_with_owner: list[str]
    ) -> dict[str, RepoMeta]:
        """Metadata for many repos at once. Default loops ``repository``; a forge
        with batch queries should override (GitHub fetches ~50 per request)."""
        out: dict[str, RepoMeta] = {}
        for nwo in names_with_owner:
            owner, _, repo = nwo.partition("/")
            meta = self.repository(owner, repo)
            if meta is not None:
                out[nwo] = meta
        return out

    # -- people & projects (discovery) --------------------------------------
    def resolve_user(self, login: str) -> UserRef | None:
        """The account's canonical login + display name, or None if unknown.

        Default: echo the login (no display name). A host with no user API can
        rely on this and be driven by ``--add-repo`` — the display name only
        strengthens name-based credit matching, it isn't required."""
        return UserRef(login=login)

    def user_repositories(self, login: str) -> list[RepoMeta]:
        """Repos the user owns. Default: none — a host without user→repos
        discovery finds candidates via ``--add-repo`` + package registries
        instead."""
        return []

    def user_contributed_repositories(self, login: str) -> list[RepoMeta]:
        """Repos the user has contributed to (not owned). Default: none."""
        return []

    def user_organizations(self, login: str) -> list[str]:
        """Org/group logins the user belongs to. Default: none."""
        return []

    def profile_links(self, login: str) -> list[str]:
        """URLs the user publishes on their own profile (bio / website field /
        profile README). Used for cross-forge identity resolution: links to
        other-forge profiles that the account owner self-attests. Default: none
        (the forge exposes no profile, so it can't contribute outbound links —
        it can still be a verified *target* if some other forge links to it)."""
        return []

    def organization_repositories(self, org: str) -> list[RepoMeta]:
        """An org/group's repos. Default: none."""
        return []

    def user_commit_history(self, login: str) -> list[RepoMeta]:
        """Every repo the user has ever committed to, including long ago. GitHub
        answers this via ``contributionsCollection`` (and includes the repo
        metadata); forges without an equivalent return [] and rely on the other
        discovery signals."""
        return []

    def team_members(self, org: str, team: str) -> list[str]:
        """Logins in a team/subgroup (for CODEOWNERS ``@org/team`` refs). Default: none."""
        return []

    # -- search & contribution analytics ------------------------------------
    def search_file_mentions(self, text: str, filename: str) -> list[FileHit]:
        """Repos whose ``filename`` (e.g. CODEOWNERS, AUTHORS) mentions ``text``
        (a handle or a full name). Default: none (forge has no code search)."""
        return []

    def search_commits_by_author(self, login: str) -> list[str]:
        """Repos (``owner/repo``) the user has authored commits in, by login.
        Default: none. (Note: GitHub no longer allows a login-qualifier-only
        commit search — use ``search_commits_by_name`` there.)"""
        return []

    def search_commits_by_name(self, name: str) -> list[str]:
        """Repos the user has authored commits in, matched by *commit author
        name*. Default: none. This catches contributions authored under emails
        not linked to the account (which GitHub's contribution graph omits)."""
        return []

    def merged_pr_count(self, owner: str, repo: str, login: str) -> int:
        """How many merged PRs/MRs the user authored in a repo. Default: 0."""
        return 0

    def path_commit_count(
        self, owner: str, repo: str, path: str, login: str, max_pages: int = 5
    ) -> int:
        """Commits by ``login`` touching ``path`` (subcomponent ownership). Default: 0."""
        return 0

    def repo_contributors(
        self, owner: str, repo: str, max_pages: int = 2
    ) -> list[ContributorCount] | None:
        """Top contributors with counts (descending), or None if it can't be
        fetched (callers stay lenient on None — absence isn't evidence). Default
        None: not every forge exposes aggregate contributor counts cheaply."""
        return None

    def repo_contributor_count(
        self, owner: str, repo: str, anon: bool = True
    ) -> int | None:
        """Total number of contributors (distinct commit-author identities when
        ``anon``), or None if the forge can't answer cheaply. Used to replace a
        capped ``N+`` estimate with the real total. Default None."""
        return None

    def repo_release_authors(
        self, owner: str, repo: str, max_releases: int = 100
    ) -> list[str] | None:
        """Author logins of the most recent releases (who published each), or None
        if the forge doesn't expose releases cheaply. Used to credit release
        managers. Default None."""
        return None

    # -- generic HTTP + housekeeping ----------------------------------------
    @abstractmethod
    def get_url(self, url: str, accept: str = "text/html,application/xhtml+xml") -> str | None:
        """Fetch an arbitrary external URL as text (project team/governance pages,
        package registries), cached, with NO auth header. Not forge-specific, but
        every forge already has a cached HTTP client, so it lives here."""

    def rate_summary(self) -> str:
        """Human-readable remaining-quota string for progress output. Default: ''."""
        return ""

    def close(self) -> None:
        """Release any held resources (HTTP connections). Default: no-op."""

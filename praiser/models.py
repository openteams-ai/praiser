"""Domain types shared across the pipeline.

Role constants + weights live here so every extractor agrees on vocabulary.
"""

import math
from dataclasses import dataclass, field

# --- Role vocabulary -------------------------------------------------------
# Ordered loosely by how much "elevation" each role signals. Weights feed the
# final ranking (popularity x role weight x confidence) and the choice of a
# project's headline role when several pieces of evidence disagree.
MAINTAINER = "maintainer"
CODE_OWNER = "code_owner"
STEERING_COUNCIL = "steering_council"
STANDARDS_AUTHOR = "standards_author"
AUTHOR = "author"  # created/authored the project (owns the repo, or named author)
RELEASE_MANAGER = "release_manager"  # authors a dominant share of the releases
CORE_CONTRIBUTOR = "core_contributor"  # substantial committer to a popular repo
ORG_OWNER = "org_owner"
ORG_MEMBER = "org_member"
CONTRIBUTOR = "contributor"  # discovered-only; never a headline role on its own

ROLE_WEIGHTS: dict[str, float] = {
    STEERING_COUNCIL: 1.00,
    ORG_OWNER: 0.90,
    MAINTAINER: 0.85,
    AUTHOR: 0.84,
    STANDARDS_AUTHOR: 0.82,
    CODE_OWNER: 0.80,
    RELEASE_MANAGER: 0.78,   # trusted to ship — maintainer-adjacent
    CORE_CONTRIBUTOR: 0.70,
    ORG_MEMBER: 0.40,
    CONTRIBUTOR: 0.10,
}

# Roles that, on their own, are too weak to put a project in the record.
WEAK_ROLES = frozenset({CONTRIBUTOR, ORG_MEMBER})

# Order to *display* multiple roles in, following a project's lifecycle:
# origination (author/standards) → governance (steering/org owner) →
# contribution (building) → maintenance (owning/maintaining, the last, ongoing
# task once the project exists). So author/creator always precedes maintainer,
# and maintenance comes after contribution. (A logical ordering for display —
# not a claim about actual dates; real timelines are #30.)
ROLE_ORDER = [
    AUTHOR,
    STANDARDS_AUTHOR,
    STEERING_COUNCIL,
    ORG_OWNER,
    CORE_CONTRIBUTOR,
    CODE_OWNER,
    RELEASE_MANAGER,
    MAINTAINER,
    ORG_MEMBER,
    CONTRIBUTOR,
]
_ROLE_ORDER_INDEX = {r: i for i, r in enumerate(ROLE_ORDER)}


def role_weight(role: str) -> float:
    return ROLE_WEIGHTS.get(role, 0.1)


def role_order(role: str) -> int:
    """Lifecycle rank for display ordering (lower = earlier in a project's life)."""
    return _ROLE_ORDER_INDEX.get(role, len(ROLE_ORDER))


# --- Identity (Phase 0) ----------------------------------------------------
@dataclass
class Identity:
    """The set of handles/names/emails believed to belong to one person.

    Handle and email matches are high confidence; name-only matches are weak
    (common-name false positives), so callers should treat them differently.
    """

    primary_login: str
    logins: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.logins = {s.lower() for s in self.logins if s}
        self.logins.add(self.primary_login.lower())
        self.names = {s.strip().lower() for s in self.names if s and s.strip()}
        self.emails = {s.strip().lower() for s in self.emails if s and s.strip()}

    def matches_handle(self, handle: str | None) -> bool:
        if not handle:
            return False
        return handle.lstrip("@").lower() in self.logins

    def matches_email(self, email: str | None) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.emails

    def matches_name(self, name: str | None) -> bool:
        if not name:
            return False
        return name.strip().lower() in self.names


# Web host per forge, so a project's link reflects where it actually lives
# rather than assuming GitHub. Discovery stamps each candidate's ``forge``.
FORGE_WEB_HOSTS: dict[str, str] = {
    "github": "https://github.com",
    "gitlab": "https://gitlab.com",
    "codeberg": "https://codeberg.org",
    "gitee": "https://gitee.com",
    "bitbucket": "https://bitbucket.org",
}
DEFAULT_FORGE = "github"


def repo_web_url(forge: str, name_with_owner: str) -> str:
    host = FORGE_WEB_HOSTS.get(forge, FORGE_WEB_HOSTS[DEFAULT_FORGE])
    return f"{host}/{name_with_owner}"


# --- Candidate project (Phase 1) ------------------------------------------
@dataclass
class Candidate:
    """A repo that *might* warrant a record entry; Phase 2 decides."""

    name_with_owner: str
    stars: int = 0
    forks: int = 0
    is_fork: bool = False
    is_private: bool = False
    pushed_at: str | None = None  # ISO-8601 of last push (maintenance signal)
    sources: set[str] = field(default_factory=set)  # discovery sources
    forge: str = DEFAULT_FORGE    # which code host this repo lives on
    # The instance web host, stamped from the forge. Needed for self-hosted
    # instances whose host isn't in FORGE_WEB_HOSTS; falls back to the map.
    web_host: str | None = None

    @property
    def owner(self) -> str:
        return self.name_with_owner.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.name_with_owner.split("/", 1)[1]

    @property
    def url(self) -> str:
        if self.web_host:
            return f"{self.web_host}/{self.name_with_owner}"
        return repo_web_url(self.forge, self.name_with_owner)


# --- Package-registry signal (Phase 1) ------------------------------------
@dataclass
class PackageRef:
    """A published package a user holds a role on, and the repo it ships from.

    The ``repo`` link is what corroborates the role: a package is only credited
    to a candidate when the package itself names that repo as its source, which
    guards against registry-username collisions (a different person who happens
    to share the handle won't have packages pointing at *this* user's repos).
    """

    registry: str               # "pypi" | "npm" | "crates"
    name: str                   # package name
    url: str                    # registry page URL (clickable evidence)
    repo: str | None = None     # GitHub "owner/repo" if the source is on GitHub
    repo_url: str | None = None  # raw source URL (any forge), for reference
    author_match: bool = False  # registry author/email matched the identity


# --- Evidence + final record (Phase 2/4) ----------------------------------
@dataclass
class Evidence:
    """One concrete signal that a user holds a role in a project."""

    source: str        # extractor name, e.g. "codeowners"
    role: str          # one of the role constants
    url: str           # link a human can click to verify
    confidence: float  # 0..1
    detail: str = ""   # short human-readable explanation
    # If the role is for a *part* of the project (a subcomponent), the part's
    # name — so display can say "Author (f2py)" not a bare project-level "Author".
    qualifier: str | None = None
    # Contributor standing, when this signal is a contributor ranking: the user's
    # rank and the number of contributors considered — display can show "#6/200".
    # ``contributors_capped`` means the list hit the fetch cap and we couldn't
    # resolve the real total (more exist) → shown as "N+". ``contributors_approx``
    # means the total is a resolved-but-approximate figure (a curated snapshot, or
    # the uncapped identity count that drifts daily) → shown rounded as "~N".
    rank: int | None = None
    n_contributors: int | None = None
    contributors_capped: bool = False
    contributors_approx: bool = False

    @property
    def weight(self) -> float:
        return role_weight(self.role)


@dataclass
class ProjectRecord:
    name_with_owner: str
    url: str
    stars: int = 0
    forks: int = 0
    pushed_at: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    importance: str | None = None  # registry label, e.g. "critical"
    # False for hosts with no star metric (cgit, Gerrit, …); then forks stand in
    # as the popularity signal so ranking/filtering still work. See forge.has_stars.
    forge_has_stars: bool = True

    @property
    def popularity(self) -> int:
        """The popularity signal for ranking/filtering: stars where the host has
        them, else forks (the one universal proxy in ``RepoMeta``)."""
        return self.stars if self.forge_has_stars else self.forks

    @property
    def best_evidence(self) -> Evidence | None:
        """Evidence for the strongest role, breaking ties by confidence."""
        if not self.evidence:
            return None
        return max(self.evidence, key=lambda e: (e.weight, e.confidence))

    @property
    def role(self) -> str | None:
        be = self.best_evidence
        return be.role if be else None

    @property
    def contributor_standing(self) -> tuple[int, int, bool, bool] | None:
        """(rank, n_contributors, capped, approx) from the contributor signal, if
        present — for "#6/200" (exact), "#6/200+" (capped), or "#6/~6800"
        (approx) displays. None when no contributor ranking backs this record."""
        for e in self.evidence:
            if e.rank and e.n_contributors:
                return (e.rank, e.n_contributors,
                        e.contributors_capped, e.contributors_approx)
        return None

    @property
    def roles(self) -> list[str]:
        """Distinct elevated roles held here, in project-lifecycle order (max 3).

        A person can hold several — e.g. founder who later became a core
        contributor, or author *and* maintainer. Weak roles (plain contributor /
        org member) are dropped; the most significant (by weight) are kept, then
        shown in lifecycle order (``role_order``) so origination precedes
        maintenance — e.g. "Author, Maintainer", never "Maintainer, Author".
        """
        distinct = {e.role for e in self.evidence if e.role not in WEAK_ROLES}
        top = sorted(distinct, key=lambda r: -role_weight(r))[:3]  # most significant
        return sorted(top, key=role_order)                          # lifecycle order

    @property
    def confidence(self) -> float:
        """Top confidence, bumped slightly for each corroborating source."""
        if not self.evidence:
            return 0.0
        be = self.best_evidence
        assert be is not None
        corroborating = {e.source for e in self.evidence if e.role == be.role}
        bump = 0.05 * (len(corroborating) - 1)
        return min(0.99, be.confidence + bump)

    @property
    def score(self) -> float:
        """Ranking score: popularity factor x role weight x confidence."""
        be = self.best_evidence
        if be is None:
            return 0.0
        factor = math.log10(self.popularity + 10)  # 1.0 at 0, grows slowly
        return factor * be.weight * self.confidence

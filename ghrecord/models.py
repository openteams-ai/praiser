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
CORE_CONTRIBUTOR = "core_contributor"  # substantial committer to a popular repo
ORG_OWNER = "org_owner"
ORG_MEMBER = "org_member"
CONTRIBUTOR = "contributor"  # discovered-only; never a headline role on its own

ROLE_WEIGHTS: dict[str, float] = {
    STEERING_COUNCIL: 1.00,
    ORG_OWNER: 0.90,
    MAINTAINER: 0.85,
    STANDARDS_AUTHOR: 0.82,
    CODE_OWNER: 0.80,
    CORE_CONTRIBUTOR: 0.70,
    ORG_MEMBER: 0.40,
    CONTRIBUTOR: 0.10,
}

# Roles that, on their own, are too weak to put a project in the record.
WEAK_ROLES = frozenset({CONTRIBUTOR, ORG_MEMBER})


def role_weight(role: str) -> float:
    return ROLE_WEIGHTS.get(role, 0.1)


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

    @property
    def owner(self) -> str:
        return self.name_with_owner.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.name_with_owner.split("/", 1)[1]

    @property
    def url(self) -> str:
        return f"https://github.com/{self.name_with_owner}"


# --- Evidence + final record (Phase 2/4) ----------------------------------
@dataclass
class Evidence:
    """One concrete signal that a user holds a role in a project."""

    source: str        # extractor name, e.g. "codeowners"
    role: str          # one of the role constants
    url: str           # link a human can click to verify
    confidence: float  # 0..1
    detail: str = ""   # short human-readable explanation

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
        popularity = math.log10(self.stars + 10)  # 1.0 at 0 stars, grows slowly
        return popularity * be.weight * self.confidence

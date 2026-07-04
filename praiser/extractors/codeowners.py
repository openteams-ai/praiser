"""CODEOWNERS extractor.

Parses the GitHub CODEOWNERS format: lines of ``<pattern> <owner...>`` where an
owner is ``@user``, ``@org/team``, or an email. ``@org/team`` references are
resolved to member logins via the API.
"""

import re
from dataclasses import dataclass

from ..models import CODE_OWNER, Evidence
from . import register
from .base import Extractor, ExtractContext

# Standard locations GitHub honours, in priority order.
CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]

_TEAM_RE = re.compile(r"^@([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)$")
_USER_RE = re.compile(r"^@([A-Za-z0-9-]+)$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class CodeownerRule:
    pattern: str
    owners: list[str]
    section: str | None = None  # nearest preceding comment header, e.g. "Sparse Tensors"


def parse_codeowners(text: str) -> list[CodeownerRule]:
    """Pure parser: text -> list of (pattern, owners, section).

    Large CODEOWNERS files group entries under comment headers (e.g. a
    "# Sparse Tensors" line above the sparse paths). We attach the nearest
    preceding comment to each rule as ``section`` so display can name the
    sub-component ("Code owner (Sparse Tensors)") instead of listing raw path
    globs (#138). A blank line ends a section; a comment line (re)sets the pending
    header. Inline comments after a rule are still stripped.
    """
    rules: list[CodeownerRule] = []
    section: str | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            section = None                       # blank line separates sections
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip() or None   # header for what follows
            continue
        line = stripped.split("#", 1)[0].strip()  # drop any inline comment
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue  # a pattern with no owners assigns nobody
        pattern, owners = parts[0], parts[1:]
        rules.append(CodeownerRule(pattern=pattern, owners=owners, section=section))
    return rules


def all_owners(rules: list[CodeownerRule]) -> list[str]:
    seen: dict[str, None] = {}
    for rule in rules:
        for owner in rule.owners:
            seen.setdefault(owner, None)
    return list(seen)


def _rule_scope(rule: "CodeownerRule") -> str | None:
    """The concise scope label for a rule's Code-owner evidence: the section
    header if the file provides one (e.g. "Sparse Tensors"), else the raw path
    pattern. A whole-repo catch-all ("*") is project-wide → None (shown bare)."""
    if rule.section:
        return rule.section
    return None if rule.pattern == "*" else rule.pattern


def classify_owner(owner: str) -> tuple[str, tuple[str, ...]]:
    """Return ("user"|"team"|"email"|"unknown", parts)."""
    m = _TEAM_RE.match(owner)
    if m:
        return "team", (m.group(1), m.group(2))
    m = _USER_RE.match(owner)
    if m:
        return "user", (m.group(1),)
    if _EMAIL_RE.match(owner):
        return "email", (owner,)
    return "unknown", (owner,)


class CodeownersExtractor(Extractor):
    name = "codeowners"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        files = ctx.forge.get_files(
            candidate.owner, candidate.repo, CODEOWNERS_PATHS
        )
        for path in CODEOWNERS_PATHS:  # honour GitHub's location priority
            text = files.get(path)
            if text is not None:
                return self._evidence_from(candidate, ctx, path, text)
        return []

    def _evidence_from(self, candidate, ctx, path, text) -> list[Evidence]:
        rules = parse_codeowners(text)
        url = f"{candidate.url}/blob/HEAD/{path}"
        found: list[Evidence] = []
        seen_quals: set[str] = set()
        team_cache: dict[tuple[str, str], bool] = {}  # (org, team) -> user is a member
        # A CODEOWNERS entry only counts if it isn't an inherited/copied file
        # (a fork or a downstream repo that vendored an upstream CODEOWNERS along
        # with its git history) — see ExtractContext.trust_role_file.
        trusted = None  # computed lazily on first match

        # Code-ownership is intrinsically PATH-SCOPED (each rule is a glob), so we
        # iterate rules — not flattened owners — and record the owned scope as the
        # evidence qualifier, rendering "Code owner (Sparse Tensors)" the same way
        # subcomponent authorship renders "Author (f2py)". The scope is the section
        # header when the file groups paths under one (concise + meaningful, #138),
        # else the raw path pattern. Owning many paths under one section collapses
        # to a single qualifier. A catch-all "*" owner is whole-project (bare).
        for rule in rules:
            detail = self._rule_match_detail(ctx, path, rule, team_cache)
            if detail is None:
                continue
            if trusted is None:
                trusted = ctx.trust_role_file(candidate)
            if not trusted:
                return []  # inherited/copied CODEOWNERS — none of it counts
            qualifier = _rule_scope(rule)
            key = qualifier if qualifier is not None else ""  # "*" -> one bare entry
            if key in seen_quals:
                continue
            seen_quals.add(key)
            found.append(Evidence(
                source=self.name, role=CODE_OWNER, url=url, confidence=0.9,
                detail=detail, qualifier=qualifier,
            ))
        return found

    def _rule_match_detail(self, ctx, path, rule, team_cache) -> str | None:
        """How the scanned identity owns this rule (as an evidence detail), or None.
        Team membership is memoised per (org, team) so a team named in many rules
        costs one API call."""
        for owner in rule.owners:
            kind, parts = classify_owner(owner)
            if kind == "user" and ctx.identity.matches_handle(parts[0]):
                return f"listed as @{parts[0]} in {path}"
            if kind == "email" and ctx.identity.matches_email(parts[0]):
                return f"listed by email in {path}"
            if kind == "team":
                key = (parts[0], parts[1])
                if key not in team_cache:
                    team_cache[key] = self._user_in_team(ctx, *key)
                if team_cache[key]:
                    return f"member of @{parts[0]}/{parts[1]} (CODEOWNERS in {path})"
        return None

    def _user_in_team(self, ctx, org, team) -> bool:
        # Could not confirm membership (private team / no scope) -> False (skip),
        # same conservative stance as before.
        try:
            members = ctx.forge.team_members(org, team)
        except Exception:
            members = []
        return bool(members) and any(ctx.identity.matches_handle(m) for m in members)


register(CodeownersExtractor())

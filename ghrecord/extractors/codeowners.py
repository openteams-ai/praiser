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


def parse_codeowners(text: str) -> list[CodeownerRule]:
    """Pure parser: text -> list of (pattern, owners). Comments/blanks dropped."""
    rules: list[CodeownerRule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue  # a pattern with no owners assigns nobody
        pattern, owners = parts[0], parts[1:]
        rules.append(CodeownerRule(pattern=pattern, owners=owners))
    return rules


def all_owners(rules: list[CodeownerRule]) -> list[str]:
    seen: dict[str, None] = {}
    for rule in rules:
        for owner in rule.owners:
            seen.setdefault(owner, None)
    return list(seen)


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
        for path in CODEOWNERS_PATHS:
            text = ctx.client.get_file(candidate.owner, candidate.repo, path)
            if text is None:
                continue
            return self._evidence_from(candidate, ctx, path, text)
        return []

    def _evidence_from(self, candidate, ctx, path, text) -> list[Evidence]:
        rules = parse_codeowners(text)
        url = f"{candidate.url}/blob/HEAD/{path}"
        found: list[Evidence] = []
        seen_owners: set[str] = set()

        for owner in all_owners(rules):
            if owner in seen_owners:
                continue
            seen_owners.add(owner)
            kind, parts = classify_owner(owner)

            if kind == "user" and ctx.identity.matches_handle(parts[0]):
                found.append(Evidence(
                    source=self.name, role=CODE_OWNER, url=url, confidence=0.9,
                    detail=f"listed as @{parts[0]} in {path}",
                ))
            elif kind == "email" and ctx.identity.matches_email(parts[0]):
                found.append(Evidence(
                    source=self.name, role=CODE_OWNER, url=url, confidence=0.9,
                    detail=f"listed by email in {path}",
                ))
            elif kind == "team":
                ev = self._team_evidence(candidate, ctx, path, url, parts[0], parts[1])
                if ev:
                    found.append(ev)
        return found

    def _team_evidence(self, candidate, ctx, path, url, org, team) -> Evidence | None:
        try:
            members = ctx.client.team_members(org, team)
        except Exception:
            members = []
        if members:
            if any(ctx.identity.matches_handle(m) for m in members):
                return Evidence(
                    source=self.name, role=CODE_OWNER, url=url, confidence=0.9,
                    detail=f"member of @{org}/{team} (CODEOWNERS in {path})",
                )
            return None  # team known and user not in it
        # Could not confirm membership (private team / no scope): weak signal only
        # if the team name itself references the user — otherwise skip.
        return None


register(CodeownersExtractor())

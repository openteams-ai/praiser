"""MAINTAINERS / OWNERS extractor.

Handles two conventions:
* free-form ``MAINTAINERS`` files (one maintainer per line, any of
  ``Name <email> (@handle)``), and
* Kubernetes-style ``OWNERS`` YAML files with ``approvers``/``reviewers``.

``approvers`` -> maintainer; ``reviewers`` -> code_owner.
"""

import re
from dataclasses import dataclass, field

from ..models import CODE_OWNER, MAINTAINER, Evidence
from . import register
from .base import Extractor, ExtractContext

MAINTAINERS_PATHS = [
    "MAINTAINERS", "MAINTAINERS.md", "MAINTAINERS.txt", "MAINTAINERS.rst",
    "docs/MAINTAINERS.md", ".github/MAINTAINERS.md",
]
OWNERS_PATHS = ["OWNERS"]

_EMAIL_RE = re.compile(r"<([^>]+)>|([^\s<>()]+@[^\s<>()]+\.[^\s<>()]+)")
_HANDLE_RE = re.compile(r"\(?@([A-Za-z0-9-]+)\)?")


@dataclass
class Person:
    name: str | None = None
    email: str | None = None
    handle: str | None = None


def parse_maintainers(text: str) -> list[Person]:
    """Pure parser for free-form MAINTAINERS files."""
    people: list[Person] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "<!--", "-->", "=", "-")):
            # skip comments and markdown/rst rules; lone "-" bullets handled below
            if not re.match(r"^[-*]\s+\S", line):
                continue
            line = re.sub(r"^[-*]\s+", "", line)
        if line.startswith(("|", "+--")) or set(line) <= set("-=| "):
            continue  # table separators / underlines

        email = None
        m = _EMAIL_RE.search(line)
        if m:
            email = (m.group(1) or m.group(2)).strip()
            line = line[: m.start()] + " " + line[m.end():]
        handle = None
        m = _HANDLE_RE.search(line)
        if m:
            handle = m.group(1)
            line = _HANDLE_RE.sub(" ", line)
        name = re.sub(r"[|*`]", " ", line)
        name = re.sub(r"\s+", " ", name).strip(" -|*<>()")
        if handle or email or (name and " " in name):
            people.append(Person(name=name or None, email=email, handle=handle))
    return people


def parse_owners_yaml(text: str) -> dict[str, list[str]]:
    """Pure parser for k8s OWNERS YAML -> {'approvers': [...], 'reviewers': [...]}."""
    result: dict[str, list[str]] = {"approvers": [], "reviewers": []}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
    except Exception:
        data = _naive_owners(text)
    if not isinstance(data, dict):
        return result
    for key in ("approvers", "reviewers"):
        vals = data.get(key) or []
        if isinstance(vals, list):
            result[key] = [str(v).lstrip("@") for v in vals if v]
    return result


def _naive_owners(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    key: str | None = None
    for line in text.splitlines():
        if re.match(r"^(approvers|reviewers)\s*:", line):
            key = line.split(":", 1)[0].strip()
            out[key] = []
        elif key and re.match(r"^\s*-\s+", line):
            out[key].append(line.split("-", 1)[1].strip().lstrip("@"))
    return out


class MaintainersExtractor(Extractor):
    name = "maintainers"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        evidence: list[Evidence] = []
        evidence.extend(self._scan_owners(candidate, ctx))
        if evidence:
            return evidence
        evidence.extend(self._scan_maintainers(candidate, ctx))
        return evidence

    def _scan_maintainers(self, candidate, ctx) -> list[Evidence]:
        for path in MAINTAINERS_PATHS:
            text = ctx.client.get_file(candidate.owner, candidate.repo, path)
            if text is None:
                continue
            url = f"{candidate.url}/blob/HEAD/{path}"
            for person in parse_maintainers(text):
                strong = ctx.identity.matches_handle(person.handle) or \
                    ctx.identity.matches_email(person.email)
                if strong:
                    return [Evidence(
                        source=self.name, role=MAINTAINER, url=url, confidence=0.85,
                        detail=f"listed in {path}",
                    )]
                if ctx.identity.matches_name(person.name):
                    return [Evidence(
                        source=self.name, role=MAINTAINER, url=url, confidence=0.5,
                        detail=f"name listed in {path}",
                    )]
            return []  # file existed but no match
        return []

    def _scan_owners(self, candidate, ctx) -> list[Evidence]:
        # Registry may point at a specific OWNERS path; else try repo root.
        paths = list(OWNERS_PATHS)
        known = ctx.known(candidate.name_with_owner)
        if known:
            paths = [c.path for c in known.conventions_for(self.name) if c.path] or paths
        for path in paths:
            text = ctx.client.get_file(candidate.owner, candidate.repo, path)
            if text is None:
                continue
            owners = parse_owners_yaml(text)
            url = f"{candidate.url}/blob/HEAD/{path}"
            if any(ctx.identity.matches_handle(h) for h in owners["approvers"]):
                return [Evidence(
                    source=self.name, role=MAINTAINER, url=url, confidence=0.85,
                    detail=f"approver in {path}",
                )]
            if any(ctx.identity.matches_handle(h) for h in owners["reviewers"]):
                return [Evidence(
                    source=self.name, role=CODE_OWNER, url=url, confidence=0.8,
                    detail=f"reviewer in {path}",
                )]
        return []


register(MaintainersExtractor())

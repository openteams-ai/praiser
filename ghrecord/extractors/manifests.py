"""Package-manifest extractor.

Reads author/maintainer fields from ``pyproject.toml`` (PEP 621 and Poetry),
``package.json``, ``Cargo.toml`` and ``composer.json``. A manifest author is a
softer signal than CODEOWNERS/MAINTAINERS (it is often the original author, not
the current maintainer), so confidences are moderate.
"""

import json
import re
import tomllib

from ..models import MAINTAINER, Evidence
from . import register
from .base import Extractor, ExtractContext
from .maintainers import Person

_PERSON_STR_RE = re.compile(
    r"^(?P<name>[^<(]*?)\s*(?:<(?P<email>[^>]+)>)?\s*(?:\((?P<url>[^)]*)\))?\s*$"
)


def parse_person_string(s: str) -> Person:
    """'Jane Doe <jane@x.org> (https://x)' -> Person."""
    m = _PERSON_STR_RE.match(s.strip())
    if not m:
        return Person(name=s.strip() or None)
    return Person(name=(m.group("name") or "").strip() or None,
                  email=(m.group("email") or "").strip() or None)


def _person_from_obj(obj) -> Person:
    if isinstance(obj, str):
        return parse_person_string(obj)
    if isinstance(obj, dict):
        return Person(name=(obj.get("name") or "").strip() or None,
                      email=(obj.get("email") or "").strip() or None)
    return Person()


def authors_from_pyproject(text: str) -> list[Person]:
    data = tomllib.loads(text)
    people: list[Person] = []
    project = data.get("project", {})
    for key in ("authors", "maintainers"):
        for entry in project.get(key, []) or []:
            people.append(_person_from_obj(entry))
    poetry = data.get("tool", {}).get("poetry", {})
    for key in ("authors", "maintainers"):
        for entry in poetry.get(key, []) or []:
            people.append(_person_from_obj(entry))
    return [p for p in people if p.name or p.email]


def authors_from_package_json(text: str) -> list[Person]:
    data = json.loads(text)
    people: list[Person] = []
    if "author" in data:
        people.append(_person_from_obj(data["author"]))
    for key in ("maintainers", "contributors"):
        for entry in data.get(key, []) or []:
            people.append(_person_from_obj(entry))
    return [p for p in people if p.name or p.email]


def authors_from_cargo(text: str) -> list[Person]:
    data = tomllib.loads(text)
    pkg = data.get("package", {})
    return [parse_person_string(a) for a in pkg.get("authors", []) or []]


def authors_from_composer(text: str) -> list[Person]:
    data = json.loads(text)
    return [_person_from_obj(a) for a in data.get("authors", []) or []]


_MANIFESTS = [
    ("pyproject.toml", authors_from_pyproject),
    ("package.json", authors_from_package_json),
    ("Cargo.toml", authors_from_cargo),
    ("composer.json", authors_from_composer),
]


class ManifestsExtractor(Extractor):
    name = "manifests"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        evidence: list[Evidence] = []
        for path, parser in _MANIFESTS:
            text = ctx.client.get_file(candidate.owner, candidate.repo, path)
            if text is None:
                continue
            try:
                people = parser(text)
            except Exception:
                continue
            ev = self._match(candidate, ctx, path, people)
            if ev:
                evidence.append(ev)
        return evidence

    def _match(self, candidate, ctx, path, people) -> Evidence | None:
        url = f"{candidate.url}/blob/HEAD/{path}"
        for p in people:
            if ctx.identity.matches_email(p.email):
                return Evidence(
                    source=self.name, role=MAINTAINER, url=url, confidence=0.75,
                    detail=f"author/maintainer email in {path}",
                )
        for p in people:
            if ctx.identity.matches_name(p.name):
                return Evidence(
                    source=self.name, role=MAINTAINER, url=url, confidence=0.45,
                    detail=f"author/maintainer name in {path}",
                )
        return None


register(ManifestsExtractor())

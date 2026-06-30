"""Package-manifest extractor.

Reads author/maintainer fields from ``pyproject.toml`` (PEP 621 and Poetry),
``package.json``, ``Cargo.toml`` and ``composer.json``. A manifest author is a
softer signal than CODEOWNERS/MAINTAINERS (it is often the original author, not
the current maintainer), so confidences are moderate.
"""

import json
import re
import tomllib

from ..models import AUTHOR, MAINTAINER, Evidence
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


def _people(entries) -> list[Person]:
    return [_person_from_obj(e) for e in (entries or [])]


def authors_from_pyproject(text: str) -> list[Person]:
    data = tomllib.loads(text)
    out = _people(data.get("project", {}).get("authors"))
    out += _people(data.get("tool", {}).get("poetry", {}).get("authors"))
    return [p for p in out if p.name or p.email]


def maintainers_from_pyproject(text: str) -> list[Person]:
    data = tomllib.loads(text)
    out = _people(data.get("project", {}).get("maintainers"))
    out += _people(data.get("tool", {}).get("poetry", {}).get("maintainers"))
    return [p for p in out if p.name or p.email]


def authors_from_package_json(text: str) -> list[Person]:
    data = json.loads(text)
    out = [_person_from_obj(data["author"])] if "author" in data else []
    return [p for p in out if p.name or p.email]


def maintainers_from_package_json(text: str) -> list[Person]:
    data = json.loads(text)
    return [p for p in _people(data.get("maintainers")) if p.name or p.email]


def _people_from_name_email(name: str | None, email: str | None) -> list[Person]:
    names = [n.strip() for n in re.split(r"[;,]", name)] if name else []
    emails = [e.strip() for e in re.split(r"[;,]", email)] if email else []
    out: list[Person] = []
    if names:
        for i, n in enumerate(names):
            out.append(Person(name=n or None,
                              email=emails[i] if i < len(emails) else None))
    else:
        out = [Person(email=e) for e in emails]
    return [p for p in out if p.name or p.email]


def _setup_kw(text: str, key: str) -> str | None:
    """Value of a `key="..."` kwarg or assignment in setup.py (literal only)."""
    m = re.search(rf"(?<![\w.]){key}\s*=\s*(['\"])(.*?)\1", text)
    return m.group(2).strip() if m else None


def authors_from_setup_py(text: str) -> list[Person]:
    name = _setup_kw(text, "author") or _setup_kw(text, "__author__")
    email = _setup_kw(text, "author_email") or _setup_kw(text, "__author_email__")
    return _people_from_name_email(name, email)


def maintainers_from_setup_py(text: str) -> list[Person]:
    return _people_from_name_email(
        _setup_kw(text, "maintainer"), _setup_kw(text, "maintainer_email"))


def _cfg_metadata(text: str) -> dict[str, str]:
    import configparser
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text)
    except configparser.Error:
        return {}
    return dict(cp["metadata"]) if cp.has_section("metadata") else {}


def authors_from_setup_cfg(text: str) -> list[Person]:
    m = _cfg_metadata(text)
    return _people_from_name_email(m.get("author"), m.get("author_email"))


def maintainers_from_setup_cfg(text: str) -> list[Person]:
    m = _cfg_metadata(text)
    return _people_from_name_email(m.get("maintainer"), m.get("maintainer_email"))


def authors_from_cargo(text: str) -> list[Person]:
    data = tomllib.loads(text)
    pkg = data.get("package", {})
    return [parse_person_string(a) for a in pkg.get("authors", []) or []]


def authors_from_composer(text: str) -> list[Person]:
    data = json.loads(text)
    return [_person_from_obj(a) for a in data.get("authors", []) or []]


# (path, authors_parser, maintainers_parser | None)
_MANIFESTS = [
    ("pyproject.toml", authors_from_pyproject, maintainers_from_pyproject),
    ("setup.cfg", authors_from_setup_cfg, maintainers_from_setup_cfg),
    ("setup.py", authors_from_setup_py, maintainers_from_setup_py),
    ("package.json", authors_from_package_json, maintainers_from_package_json),
    ("Cargo.toml", authors_from_cargo, None),
    ("composer.json", authors_from_composer, None),
]


class ManifestsExtractor(Extractor):
    name = "manifests"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        files = ctx.client.get_files(
            candidate.owner, candidate.repo, [p for p, _, _ in _MANIFESTS]
        )
        evidence: list[Evidence] = []
        for path, aparse, mparse in _MANIFESTS:
            text = files.get(path)
            if text is None:
                continue
            for parser, role in ((aparse, AUTHOR), (mparse, MAINTAINER)):
                if parser is None:
                    continue
                try:
                    people = parser(text)
                except Exception:
                    continue
                ev = self._match(candidate, ctx, path, people, role)
                if ev:
                    evidence.append(ev)
        return evidence

    def _match(self, candidate, ctx, path, people, role) -> Evidence | None:
        url = f"{candidate.url}/blob/HEAD/{path}"
        label = "author" if role == AUTHOR else "maintainer"
        for p in people:
            if ctx.identity.matches_email(p.email):
                return Evidence(
                    source=self.name, role=role, url=url, confidence=0.75,
                    detail=f"{label} email in {path}",
                )
        for p in people:
            if ctx.identity.matches_name(p.name):
                return Evidence(
                    source=self.name, role=role, url=url, confidence=0.45,
                    detail=f"{label} name in {path}",
                )
        return None


register(ManifestsExtractor())

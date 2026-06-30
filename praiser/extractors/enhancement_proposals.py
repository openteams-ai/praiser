"""Enhancement-proposal extractor (generalised PEP / NEP / SPEC / JEP / ...).

These series share one shape: a folder of numbered proposal documents whose
header carries an ``Author`` field. So this is ONE extractor parameterised by
``(path, header_format)`` plus auto-detection of the pattern, rather than N
hand-written extractors.

Pure, offline-testable functions:
* ``parse_proposal_header(text, fmt)`` -> header dict
* ``parse_authors(value)`` -> list[Author]
* ``looks_like_proposal_dir(names)`` -> bool
"""

import re
from dataclasses import dataclass

from ..models import STANDARDS_AUTHOR, Evidence
from . import register
from .base import Extractor, ExtractContext

# Where proposal series commonly live when not specified by the registry.
AUTODETECT_PATHS = ["peps", "doc/neps", "neps", "proposals", "."]
MAX_DOCS = 800  # safety cap on how many proposal docs we will fetch per repo

_FIELD_RE = re.compile(r"^:?([A-Za-z][\w\- ]*?):\s*(.*)$")
_EMAIL_RE = re.compile(r"<([^>]+)>")
_HANDLE_RE = re.compile(r"\(?@([A-Za-z0-9-]+)\)?")
_NUMBERED_RE = re.compile(r"\d{3,4}")


@dataclass
class Author:
    name: str | None = None
    email: str | None = None
    handle: str | None = None


# --- header parsing --------------------------------------------------------
def parse_proposal_header(text: str, fmt: str = "rst") -> dict[str, object]:
    """Extract the leading metadata header as a dict (lowercased keys).

    ``fmt="rst"`` handles both RST field lists (``:Author:``) and RFC2822-style
    headers (``Author:``), including indented continuation lines.
    ``fmt="yaml"`` parses Markdown YAML front-matter.
    """
    if fmt == "yaml":
        return _parse_yaml_frontmatter(text)

    headers: dict[str, object] = {}
    current_key: str | None = None
    for line in text.splitlines()[:150]:
        if not line.strip():
            current_key = None
            continue
        if not line[0].isspace():
            m = _FIELD_RE.match(line.strip())
            if m:
                key = m.group(1).strip().lower()
                headers[key] = m.group(2).strip()
                current_key = key
                continue
            current_key = None  # a content line; stop extending fields
        elif current_key is not None:  # indented continuation
            headers[current_key] = f"{headers[current_key]} {line.strip()}".strip()
    return headers


def _parse_yaml_frontmatter(text: str) -> dict[str, object]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next(
        (i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")),
        None,
    )
    if end is None:
        return {}
    block = "\n".join(lines[1:end])
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block) or {}
        if isinstance(data, dict):
            return {str(k).lower(): v for k, v in data.items()}
        return {}
    except Exception:
        return _naive_yaml(block)


def _naive_yaml(block: str) -> dict[str, object]:
    """Tiny fallback for ``key: value`` and simple ``- item`` lists."""
    out: dict[str, object] = {}
    key: str | None = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and key is not None:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.split("-", 1)[1].strip())  # type: ignore[attr-defined]
            continue
        if ":" in line and not line[0].isspace():
            k, _, v = line.partition(":")
            key = k.strip().lower()
            out[key] = v.strip()
    return out


def parse_authors(value: object) -> list[Author]:
    """Split an Author field into individual authors with name/email/handle."""
    if value is None:
        return []
    tokens: list[str]
    if isinstance(value, list):
        tokens = [str(v) for v in value]
    else:
        tokens = re.split(r"[,;\n]+", str(value))

    authors: list[Author] = []
    for raw in tokens:
        tok = raw.strip()
        if not tok:
            continue
        email = None
        m = _EMAIL_RE.search(tok)
        if m:
            email = m.group(1).strip()
            tok = tok.replace(m.group(0), " ")
        handle = None
        m = _HANDLE_RE.search(tok)
        if m:
            handle = m.group(1)
            tok = _HANDLE_RE.sub(" ", tok)
        name = re.sub(r"\s+", " ", tok).strip(" ()<>")
        if name or email or handle:
            authors.append(Author(name=name or None, email=email, handle=handle))
    return authors


# --- auto-detection --------------------------------------------------------
def looks_like_proposal_dir(names: list[str]) -> bool:
    """True if a directory listing resembles a numbered proposal series."""
    numbered = [
        n for n in names
        if _NUMBERED_RE.search(n)
        and (n.lower().endswith((".rst", ".md")) or _NUMBERED_RE.fullmatch(
            re.sub(r"\D", "", n)) is not None and "." not in n)
    ]
    return len(numbered) >= 3


def guess_format(names: list[str]) -> str:
    rst = sum(1 for n in names if n.lower().endswith(".rst"))
    md = sum(1 for n in names if n.lower().endswith(".md"))
    return "rst" if rst >= md else "yaml" if md else "rst"


# --- the extractor ---------------------------------------------------------
class EnhancementProposalsExtractor(Extractor):
    name = "enhancement_proposals"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        plans = self._resolve_plans(candidate, ctx)
        evidence: list[Evidence] = []
        for path, fmt in plans:
            evidence.extend(self._scan(candidate, ctx, path, fmt))
            if evidence:  # one good series is enough for this repo
                break
        return evidence

    def _resolve_plans(self, candidate, ctx) -> list[tuple[str, str]]:
        """Return [(path, fmt)] from registry hints, else auto-detect."""
        known = ctx.known(candidate.name_with_owner)
        if known:
            convs = known.conventions_for(self.name)
            plans = [(c.path or ".", c.header_format or "rst") for c in convs]
            if plans:
                return plans

        plans = []
        for path in AUTODETECT_PATHS:
            entries = ctx.forge.list_dir(candidate.owner, candidate.repo, path)
            names = [e.name for e in entries]
            if names and looks_like_proposal_dir(names):
                plans.append((path, guess_format(names)))
        return plans

    def _iter_docs(self, candidate, ctx, path) -> list[str]:
        """Doc paths within a series: numbered files, or index.md in numbered dirs."""
        entries = ctx.forge.list_dir(candidate.owner, candidate.repo, path)
        docs: list[str] = []
        prefix = "" if path in ("", ".") else f"{path}/"
        for e in entries:
            name = e.name
            full = f"{prefix}{name}"
            if not e.is_dir and name.lower().endswith((".rst", ".md")) \
                    and _NUMBERED_RE.search(name):
                docs.append(full)
            elif e.is_dir and _NUMBERED_RE.search(name):
                for inner in ("index.md", "README.md", "index.rst", "readme.md"):
                    docs.append(f"{full}/{inner}")  # tried; missing ones 404
        return docs[:MAX_DOCS]

    def _fetch_many(self, candidate, ctx, docs: list[str]) -> list[str | None]:
        """Batch-fetch proposal docs; a series can be hundreds of files.

        Uses the client's GraphQL batch path (separate rate bucket, many files
        per request), preserving order to align with ``docs``.
        """
        if not docs:
            return []
        fetched = ctx.forge.get_files(candidate.owner, candidate.repo, docs)
        return [fetched.get(doc) for doc in docs]

    def _scan(self, candidate, ctx, path, fmt) -> list[Evidence]:
        docs = self._iter_docs(candidate, ctx, path)
        texts = self._fetch_many(candidate, ctx, docs)
        matched: list[str] = []
        weak_match = False
        for doc, text in zip(docs, texts):
            if text is None:
                continue
            header = parse_proposal_header(text, fmt)
            authors = parse_authors(header.get("author") or header.get("authors"))
            hit, weak = self._match(authors, ctx)
            if hit:
                matched.append(doc)
                weak_match = weak_match or weak

        if not matched:
            return []
        confidence = 0.5 if weak_match and len(matched) == 1 else 0.85
        first = matched[0]
        url = f"{candidate.url}/blob/HEAD/{first}"
        listed = ", ".join(m.rsplit('/', 1)[-1] for m in matched[:5])
        more = "" if len(matched) <= 5 else f" (+{len(matched) - 5} more)"
        return [Evidence(
            source=self.name, role=STANDARDS_AUTHOR, url=url, confidence=confidence,
            detail=f"author of {len(matched)} proposal(s): {listed}{more}",
        )]

    @staticmethod
    def _match(authors: list[Author], ctx) -> tuple[bool, bool]:
        """Return (matched, weak). Handle/email = strong; name-only = weak."""
        for a in authors:
            if ctx.identity.matches_handle(a.handle) or ctx.identity.matches_email(a.email):
                return True, False
        for a in authors:
            if ctx.identity.matches_name(a.name):
                return True, True
        return False, False


register(EnhancementProposalsExtractor())

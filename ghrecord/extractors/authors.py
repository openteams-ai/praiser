"""AUTHORS / THANKS / CONTRIBUTORS extractor.

Many projects credit founders and major authors by name in these files
(e.g. SciPy's THANKS.txt). A name match alone is moderate confidence (common
names), but it corroborates the commit-volume signal: when both fire, the
record's confidence rises. A handle match is stronger.
"""

import re

from ..models import CORE_CONTRIBUTOR, Evidence
from . import register
from .base import Extractor, ExtractContext

AUTHORS_PATHS = [
    "AUTHORS", "AUTHORS.txt", "AUTHORS.rst", "AUTHORS.md",
    "THANKS", "THANKS.txt", "THANKS.rst", "THANKS.md",
    "CONTRIBUTORS", "CONTRIBUTORS.txt", "CONTRIBUTORS.md",
    "doc/source/dev/THANKS.txt",
]

_HANDLE_RE = re.compile(r"@([A-Za-z0-9-]+)")


def find_credit(text: str, names: set[str], logins: set[str]) -> tuple[str, bool] | None:
    """Return (matching line, strong) if the user is credited, else None.

    ``strong`` is True for a handle match, False for a name-only match.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        handles = {h.lower() for h in _HANDLE_RE.findall(line)}
        if handles & logins:
            return line, True
    low = text.lower()
    for name in names:
        if len(name) > 5 and name in low:  # full names only, avoid short tokens
            for raw in text.splitlines():
                if name in raw.lower():
                    return raw.strip(), False
    return None


class AuthorsExtractor(Extractor):
    name = "authors"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        files = ctx.client.get_files(
            candidate.owner, candidate.repo, AUTHORS_PATHS
        )
        for path in AUTHORS_PATHS:
            text = files.get(path)
            if text is None:
                continue
            hit = find_credit(text, ctx.identity.names, ctx.identity.logins)
            if hit is None:
                continue
            line, strong = hit
            # A name-only credit is easily copied; only trust it when the match
            # itself is trustworthy (own/org repo, or the canonical project).
            if not strong and not ctx.trust_role_file(candidate):
                return []
            snippet = (line[:60] + "…") if len(line) > 60 else line
            return [Evidence(
                source=self.name, role=CORE_CONTRIBUTOR,
                url=f"{candidate.url}/blob/HEAD/{path}",
                confidence=0.7 if strong else 0.5,
                detail=f"credited in {path}: “{snippet}”",
            )]
        return []


register(AuthorsExtractor())

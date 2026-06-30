"""Authoritative web-page role extractor.

Some projects record roles not in a repo file but on a web page — a team page
(numpy.org/teams), a "persons of interest" / maintainers page (PyTorch), a
steering-council roster, etc. The format is wildly inconsistent, so rather than
guess, the known-projects registry lets a maintainer point at the exact URL and
say which role it confers (``role_sources``). This extractor fetches those pages
and matches the user by GitHub handle (a github.com/<handle> link) or full name.

This is more authoritative than commit-count heuristics: it reflects the
project's own statement of who holds the role.
"""

import html
import re

from ..models import Evidence
from . import register
from .base import Extractor, ExtractContext

_TAG_RE = re.compile(r"<[^>]+>")
_GH_LINK_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)", re.I)
_WS_RE = re.compile(r"\s+")


def page_text(html_src: str) -> str:
    """Strip tags/entities to searchable lowercase text."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_src))).lower()


def handles_on_page(html_src: str) -> set[str]:
    return {h.lower() for h in _GH_LINK_RE.findall(html_src)}


def matches(html_src: str, logins: set[str], names: set[str]) -> bool | None:
    """True=handle match (strong), False=name-only (weaker), None=no match."""
    if handles_on_page(html_src) & logins:
        return True
    text = page_text(html_src)
    if any(len(n) > 5 and n in text for n in names):
        return False
    return None


class WebRolesExtractor(Extractor):
    name = "web_roles"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        known = ctx.known(candidate.name_with_owner)
        return bool(known and known.role_sources)

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        known = ctx.known(candidate.name_with_owner)
        if not known:
            return []
        out: list[Evidence] = []
        for src in known.role_sources:
            page = ctx.client.get_url(src.url)
            if not page:
                continue
            m = matches(page, ctx.identity.logins, ctx.identity.names)
            if m is None:
                continue
            label = src.label or "project role page"
            out.append(Evidence(
                source=self.name, role=src.role, url=src.url,
                confidence=0.9 if m else 0.75,
                detail=("listed by GitHub handle" if m else "listed by name")
                       + f" on {label}",
            ))
        return out


register(WebRolesExtractor())

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

from ..models import STEERING_COUNCIL, Evidence
from . import register
from .base import Extractor, ExtractContext

# High-authority roles must be backed by a GitHub-handle match, not just a name
# (a name on an "about"/history page is too easily a founder/credit mention).
HANDLE_REQUIRED_ROLES = frozenset({STEERING_COUNCIL})

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
            page = ctx.forge.get_url(src.url)
            if not page:
                continue
            m = matches(page, ctx.identity.logins, ctx.identity.names)
            if m is None:
                continue
            if not m and src.role in HANDLE_REQUIRED_ROLES:
                continue  # name-only is too weak for e.g. steering council
            label = src.label or "project role page"
            out.append(Evidence(
                source=self.name, role=src.role, url=src.url,
                confidence=0.9 if m else 0.75,
                detail=("listed by GitHub handle" if m else "listed by name")
                       + f" on {label}",
            ))
        return out


register(WebRolesExtractor())


class WebRolesAutoExtractor(Extractor):
    """Like web_roles, but discovers the role pages via Claude + web search.

    Runs only when enabled (--discover-roles), for popular candidates that have
    no curated role_sources, and when an LLM is available. Discovered URLs are
    cached, so re-runs cost nothing. Confidence is a notch below curated sources
    since the page was found automatically rather than vetted by a human.
    """

    name = "web_roles_auto"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        if not (ctx.auto_discover_roles and ctx.llm is not None):
            return False
        if not ctx.is_notable(candidate):
            return False
        known = ctx.known(candidate.name_with_owner)
        return not (known and known.role_sources)  # curated sources win

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        try:
            sources = ctx.llm.discover_role_sources(candidate.name_with_owner)
        except Exception:
            return []
        out: list[Evidence] = []
        reachable: list[dict] = []
        for src in sources:
            page = ctx.forge.get_url(src["url"])
            if not page:
                continue
            reachable.append(src)  # real, fetchable page -> worth saving
            m = matches(page, ctx.identity.logins, ctx.identity.names)
            if m is None:
                continue
            if not m and src["role"] in HANDLE_REQUIRED_ROLES:
                continue
            out.append(Evidence(
                source=self.name, role=src["role"], url=src["url"],
                confidence=0.85 if m else 0.6,
                detail=("listed by GitHub handle" if m else "listed by name")
                       + f" on {src.get('label', 'discovered role page')} "
                         "(web-search discovered)",
            ))
        # Record discovered, reachable sources so --save-registry can persist them.
        ctx.note_discovered(candidate.name_with_owner, reachable)
        return out


register(WebRolesAutoExtractor())

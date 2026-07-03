"""Wikidata role derivation (issue #24).

The scalable, hand-listing-free way to credit creators/founders and principal
developers: Wikidata carries structured, cited claims for many notable software
projects — ``creator`` (P170), ``founded by`` (P112), ``developer`` (P178) — and
the person items often carry a **GitHub username** (P2037). So we resolve a repo
to its Wikidata item via ``source code repository`` (P1324), read those claims,
and match to the identity **by handle** (no name ambiguity, no false merges).

Claim → role:
* P170 creator / P112 founded by → AUTHOR (originated the project).
* P178 developer → MAINTAINER (a named principal developer — not necessarily the
  creator, so we don't overclaim authorship).

Gated on popularity (notable projects are the ones Wikidata covers) and on
``use_wikidata``. The SPARQL result is fetched via the cached ``get_url``.
Parsing is pure for offline tests.
"""

import json
import re
import urllib.parse

from ..cache import Cache
from ..models import AUTHOR, MAINTAINER, Evidence
from . import register
from .base import Extractor, ExtractContext

_WDQS = "https://query.wikidata.org/sparql"
# Wikidata property -> (praiser role, human label, confidence)
_CLAIM = {
    "P170": (AUTHOR, "creator", 0.9),
    "P112": (AUTHOR, "founder", 0.9),
    "P178": (MAINTAINER, "developer", 0.75),
}


def build_sparql(repo_url: str) -> str:
    """SPARQL: people (with a GitHub handle) credited on the item whose source
    repository is ``repo_url``, and which claim credits them."""
    m = re.match(r"https?://(.+)$", repo_url.strip())
    hostpath = (m.group(1) if m else repo_url).rstrip("/")
    pat = (re.escape(hostpath) + r"(\.git)?/?$").replace("\\", "\\\\")  # SPARQL-escape
    props = " ".join(f"wdt:{p}" for p in _CLAIM)
    return (
        "SELECT ?item ?prop ?gh WHERE { "
        f"VALUES ?prop {{ {props} }} "
        "?item wdt:P1324 ?repo . "
        f'FILTER(REGEX(STR(?repo), "{pat}", "i")) '
        "?item ?prop ?person . ?person wdt:P2037 ?gh . }"
    )


def parse_people(resp: dict) -> list[tuple[str, str, str]]:
    """[(github_handle_lower, property_id, item_url)] from a SPARQL JSON result."""
    out: list[tuple[str, str, str]] = []
    for b in (resp.get("results") or {}).get("bindings", []):
        gh = (b.get("gh") or {}).get("value")
        item = (b.get("item") or {}).get("value")
        prop = (b.get("prop") or {}).get("value", "").rsplit("/", 1)[-1]
        if gh and item and prop:
            out.append((gh.lower(), prop, item))
    return out


class WikidataExtractor(Extractor):
    name = "wikidata"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        return (ctx.use_wikidata
                and candidate.stars >= ctx.role_discovery_floor)

    def _people(self, candidate, ctx: ExtractContext):
        """Repo-level Wikidata people ``[(handle, prop, item)]`` — cached in the
        shared/durable founder cache (a repo's creators are user-independent and
        time-independent), so it's resolved once and reused across scans instead
        of hitting the throttled WDQS every time (#108). Returns None on a fetch
        failure (transient — NOT cached, retried next scan)."""
        fc = ctx.founder_cache
        ck = Cache.key("wikidata-people", candidate.name_with_owner)
        if fc is not None:
            cached = fc.get(ck, default=None)
            if cached is not None:
                return [tuple(p) for p in cached]     # JSON stored lists
        url = f"{_WDQS}?format=json&query={urllib.parse.quote(build_sparql(candidate.url))}"
        page = ctx.forge.get_url(url, accept="application/sparql-results+json")
        if not page:
            return None                                # transient — don't cache
        try:
            people = parse_people(json.loads(page))
        except ValueError:
            return None
        if fc is not None:
            fc.set(ck, [list(p) for p in people])      # incl. empty (real "no claims")
        return people

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        people = self._people(candidate, ctx)
        if people is None:
            return []
        out: list[Evidence] = []
        for handle, prop, item in people:
            if not ctx.identity.matches_handle(handle):
                continue
            role, label, conf = _CLAIM.get(prop, (None, None, 0.0))
            if role is None:
                continue
            out.append(Evidence(
                source=self.name, role=role, url=item, confidence=conf,
                detail=f"{label} of the project (Wikidata)",
            ))
        return out


register(WikidataExtractor())

"""Wikipedia infobox author derivation.

A cheap, derivable *corroborating* signal for original authors/founders — NOT an
authority. Software infoboxes on Wikipedia carry an ``author`` field (rendered
"Original author(s)") that often outlives the project's own AUTHORS/THANKS file
(e.g. SciPy deleted THANKS.txt, but its Wikipedia infobox still lists
"Travis Oliphant, Pearu Peterson, Eric Jones"). So this fills the gap between
Wikidata creator claims (structured but sparse — many items have none) and the
LLM founder fallback (opt-in, costs quota).

It is deliberately treated as one moderate-confidence signal, not truth:
Wikipedia is mutable and incomplete, so a miss is fine (the LLM stays the
fallback) and a hit only corroborates.

Repo → article resolution goes through Wikidata (``source code repository``
P1324 → the item's English Wikipedia sitelink) — the reliable bridge, avoiding
fragile name-search onto the wrong page. Matching is by **full name** (the
infobox names people, not handles), so confidence is moderate and only exact
profile-name matches count. Gated on ``use_wikidata`` + popularity; both network
calls go through the cached ``get_url``. Parsing is pure for offline tests.
"""

import json
import re
import urllib.parse

from ..cache import Cache
from ..models import AUTHOR, Evidence
from . import register
from .base import Extractor, ExtractContext

_WDQS = "https://query.wikidata.org/sparql"
_WIKI_API = "https://en.wikipedia.org/w/api.php"
_CONFIDENCE = 0.6  # name-match, non-authoritative: corroborates, doesn't dictate

# Infobox values that are placeholders, not people.
_NOT_A_PERSON = re.compile(
    r"\b(team|project|community|contributors?|developers?|foundation|inc\.?|"
    r"llc|group|labs?)\b", re.I)


def build_title_sparql(repo_url: str) -> str:
    """SPARQL: the English Wikipedia article for the item whose source-code
    repository (P1324) is ``repo_url``."""
    m = re.match(r"https?://(.+)$", repo_url.strip())
    hostpath = (m.group(1) if m else repo_url).rstrip("/")
    pat = (re.escape(hostpath) + r"(\.git)?/?$").replace("\\", "\\\\")
    return (
        "SELECT ?article WHERE { "
        "?item wdt:P1324 ?repo . "
        f'FILTER(REGEX(STR(?repo), "{pat}", "i")) '
        "?article schema:about ?item ; "
        "schema:isPartOf <https://en.wikipedia.org/> . } LIMIT 1"
    )


def parse_article_title(resp: dict) -> str | None:
    """The Wikipedia page title from a SPARQL result (``…/wiki/Title`` → Title)."""
    for b in (resp.get("results") or {}).get("bindings", []):
        url = (b.get("article") or {}).get("value", "")
        seg = url.rsplit("/wiki/", 1)[-1] if "/wiki/" in url else ""
        if seg:
            return urllib.parse.unquote(seg).replace("_", " ")
    return None


def _clean_markup(value: str) -> str:
    value = re.sub(r"<ref[^>]*/>", "", value)
    value = re.sub(r"<ref[^>]*>.*?</ref>", "", value, flags=re.I | re.S)
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)              # templates
    value = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", value)  # [[a|b]]->b
    value = re.sub(r"</?br\s*/?>", ",", value, flags=re.I)   # line breaks -> commas
    return value


def parse_infobox_authors(wikitext: str) -> list[str]:
    """Names in the infobox ``author`` field ("Original author(s)"), cleaned.

    Only well-formed full names are kept (a space, length > 5, and no
    placeholder token like "Team"/"Community project") — matching is by exact
    profile name downstream, so junk simply never matches, but this keeps the
    list tidy."""
    m = re.search(r"^\s*\|\s*author\s*=\s*(.+)$", wikitext, re.I | re.M)
    if not m:
        return []
    names = []
    for tok in re.split(r",|;|\band\b", _clean_markup(m.group(1))):
        tok = tok.strip(" \t'\"·•")
        if len(tok) > 5 and " " in tok and not _NOT_A_PERSON.search(tok):
            names.append(tok)
    return names


class WikipediaFoundersExtractor(Extractor):
    name = "wikipedia_authors"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Rides the Wikidata toggle (uses WDQS to resolve the page) and the same
        # popularity floor — and, crucially, is NOT gated on the LLM flag, so it
        # surfaces well-known founders in a default scan.
        return (ctx.use_wikidata
                and candidate.stars >= ctx.role_discovery_floor
                and bool(ctx.identity.names))

    def _fetch_json(self, ctx, url, accept):
        page = ctx.forge.get_url(url, accept=accept)
        if not page:
            return None
        try:
            return json.loads(page)
        except ValueError:
            return None

    def _authors(self, candidate, ctx: ExtractContext):
        """Repo-level ``(title, [author names])`` from the Wikipedia infobox —
        cached in the shared/durable founder cache (a repo's original authors are
        user- and time-independent), resolved once and reused so we don't re-hit
        the throttled WDQS/Wikipedia each scan (#108). Returns None on a fetch
        failure (transient — NOT cached, retried next scan)."""
        fc = ctx.founder_cache
        ck = Cache.key("wikipedia-authors", candidate.name_with_owner)
        if fc is not None:
            cached = fc.get(ck, default=None)
            if cached is not None:
                return cached[0], list(cached[1])      # (title, authors)
        sparql = build_title_sparql(candidate.url)
        resp = self._fetch_json(
            ctx, f"{_WDQS}?format=json&query={urllib.parse.quote(sparql)}",
            "application/sparql-results+json")
        if resp is None:
            return None                                # transient — don't cache
        title = parse_article_title(resp)
        if not title:
            if fc is not None:
                fc.set(ck, ["", []])                   # no enwiki article: real empty
            return "", []
        api = (f"{_WIKI_API}?action=parse&prop=wikitext&section=0&format=json"
               f"&page={urllib.parse.quote(title)}")
        data = self._fetch_json(ctx, api, "application/json")
        if data is None:
            return None                                # transient — don't cache
        wikitext = ((data.get("parse") or {}).get("wikitext") or {}).get("*") or ""
        authors = parse_infobox_authors(wikitext)
        if fc is not None:
            fc.set(ck, [title, authors])
        return title, authors

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        resolved = self._authors(candidate, ctx)
        if resolved is None:
            return []
        title, authors = resolved
        if not title:
            return []
        for person in authors:
            if ctx.identity.matches_name(person):
                page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
                return [Evidence(
                    source=self.name, role=AUTHOR, url=page_url,
                    confidence=_CONFIDENCE,
                    detail=f"listed as an original author in the “{title}” "
                           "Wikipedia infobox")]
        return []


register(WikipediaFoundersExtractor())

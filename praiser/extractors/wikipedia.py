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


def _first_last(name: str) -> tuple[str, str] | None:
    """(first, last) tokens of a name, lowercased, punctuation/middle stripped —
    so "Travis E. Oliphant" and "Travis Oliphant" share (travis, oliphant)."""
    toks = [t for t in re.sub(r"[.,]", " ", name).lower().split() if t]
    if not toks:
        return None
    return (toks[0], toks[-1])


def _relaxed_name_match(name: str, identity_names: set[str]) -> bool:
    """True if ``name`` agrees with an identity name on first AND last token
    (ignoring middle initials/names) — a looser match than exact equality, only
    safe to act on WITH independent same-person evidence (see the extractor)."""
    fl = _first_last(name)
    if fl is None or fl[0] == fl[1]:      # need a distinct first+last to be safe
        return False
    return any(_first_last(n) == fl for n in identity_names)


class WikipediaFoundersExtractor(Extractor):
    name = "wikipedia_authors"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Rides the Wikidata toggle; NOT gated on the LLM flag, so it surfaces
        # founders in a default scan. Name-match extractor, so needs identity names.
        return ctx.use_wikidata and ctx.is_notable(candidate) and bool(ctx.identity.names)

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
        # Resolve repo → article title. Prefer the registry's curated title: it
        # skips the Wikidata Query Service (WDQS), which throttles cloud IPs to
        # ~1 req/min and starves a scan's founder lookups (#108). The Wikipedia
        # API itself stays reachable, so a curated title makes the whole path
        # WDQS-free. Fall back to WDQS resolution for uncurated repos.
        known = ctx.known(candidate.name_with_owner)
        title = known.wikipedia if (known and known.wikipedia) else None
        if not title:
            sparql = build_title_sparql(candidate.url)
            resp = self._fetch_json(
                ctx, f"{_WDQS}?format=json&query={urllib.parse.quote(sparql)}",
                "application/sparql-results+json")
            if resp is None:
                return None                            # transient — don't cache
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

    def _contributes(self, candidate, ctx: ExtractContext) -> bool:
        """Independent evidence the scanned user is tied to THIS repo — a
        contributor by handle, or discovered via a genuine live-contribution
        signal. Used to safely allow a relaxed name match (below)."""
        if candidate.sources & {"contributed", "history"}:
            return True
        contribs = ctx.contributors(candidate) or {}
        return any(h in contribs for h in ctx.identity.logins)

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        resolved = self._authors(candidate, ctx)
        if resolved is None:
            ctx.diag(f"wiki {candidate.name_with_owner}: _authors=None "
                     "(founder-cache miss + live fetch failed/throttled)")
            return []
        title, authors = resolved
        if not title:
            ctx.diag(f"wiki {candidate.name_with_owner}: no enwiki article")
            return []
        if ctx.diag_on:
            ex = [p for p in authors if ctx.identity.matches_name(p)]
            rx = [p for p in authors if _relaxed_name_match(p, ctx.identity.names)]
            ctx.diag(
                f"wiki {candidate.name_with_owner}: title={title!r} "
                f"authors={authors} names={sorted(ctx.identity.names)} "
                f"exact={ex} relaxed={rx} contributes={self._contributes(candidate, ctx)} "
                f"sources={sorted(candidate.sources)}")
        page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"

        def evidence(how=""):
            return [Evidence(source=self.name, role=AUTHOR, url=page_url,
                             confidence=_CONFIDENCE,
                             detail=f"listed as an original author in the “{title}” "
                                    f"Wikipedia infobox{how}")]

        # Exact name match — always trustworthy.
        if any(ctx.identity.matches_name(p) for p in authors):
            return evidence()
        # A relaxed (first+last, middle-initial-tolerant) match — e.g. "Travis
        # Oliphant" (infobox) vs "Travis E. Oliphant" (GitHub) — only WITH
        # independent same-person evidence: the scanned user also contributes to
        # this repo. Without that a bare relaxed match would risk common-name
        # false positives (cf. #72/#73), so check contribution lazily first.
        if (any(_relaxed_name_match(p, ctx.identity.names) for p in authors)
                and self._contributes(candidate, ctx)):
            return evidence(" (name + contribution)")
        return []


register(WikipediaFoundersExtractor())

"""Offline tests for the Wikipedia-infobox author extractor."""

import json

from praiser.extractors.base import ExtractContext
from praiser.extractors.wikipedia import (
    build_title_sparql,
    parse_article_title,
    parse_infobox_authors,
)
from praiser.models import AUTHOR, Candidate, Identity
from praiser.registry import KnownProjects

# The real SciPy infobox (verified via the MediaWiki API): the founders survive
# in Wikipedia even though scipy deleted its own THANKS.txt.
SCIPY_WIKITEXT = """\
{{Infobox software
| name = SciPy
| author = [[Travis Oliphant]], Pearu Peterson, Eric Jones
| developer = Community library project
| released = {{Start date and age|2001}}
}}
'''SciPy''' is a library…
"""


def test_build_title_sparql_matches_repo_and_scopes_to_enwiki():
    q = build_title_sparql("https://github.com/scipy/scipy")
    assert "wdt:P1324" in q                       # source code repository
    assert "scipy/scipy" in q                     # repo path in the regex filter
    assert "en.wikipedia.org" in q                # English Wikipedia only


def test_parse_article_title_from_sparql():
    resp = {"results": {"bindings": [
        {"article": {"value": "https://en.wikipedia.org/wiki/SciPy"}}]}}
    assert parse_article_title(resp) == "SciPy"
    # underscores in the URL segment become spaces
    resp2 = {"results": {"bindings": [
        {"article": {"value": "https://en.wikipedia.org/wiki/Visual_Studio_Code"}}]}}
    assert parse_article_title(resp2) == "Visual Studio Code"
    assert parse_article_title({"results": {"bindings": []}}) is None


def test_parse_infobox_authors_cleans_and_splits():
    authors = parse_infobox_authors(SCIPY_WIKITEXT)
    assert authors == ["Travis Oliphant", "Pearu Peterson", "Eric Jones"]


def test_parse_infobox_authors_drops_placeholders_and_junk():
    wt = "| author = SciPy Team, The Community Project, {{nowrap|X}}, ab"
    # "SciPy Team"/"Community Project" are placeholders; "ab" too short/no space
    assert parse_infobox_authors(wt) == []


def test_parse_infobox_authors_none_when_no_author_field():
    assert parse_infobox_authors("{{Infobox software\n| name = X\n}}") == []


class _WikiForge:
    """Serves the SPARQL title lookup, then the MediaWiki wikitext."""
    def __init__(self, wikitext=SCIPY_WIKITEXT, title="SciPy"):
        self.wikitext, self.title = wikitext, title
        self.calls = []

    def get_url(self, url, accept="text/html"):
        self.calls.append(url)
        if "query.wikidata.org" in url:
            return json.dumps({"results": {"bindings": [
                {"article": {"value": f"https://en.wikipedia.org/wiki/{self.title}"}}]}})
        if "api.php" in url:
            return json.dumps({"parse": {"wikitext": {"*": self.wikitext}}})
        return None


def _ctx(forge, name="Pearu Peterson", use_wikidata=True, floor=1000, founder_cache=None):
    return ExtractContext(
        identity=Identity(primary_login="pearu", names={name}),
        forge=forge, registry=KnownProjects(projects={}),
        use_wikidata=use_wikidata, role_discovery_floor=floor,
        founder_cache=founder_cache)


class _MemCache:
    def __init__(self): self.d = {}
    def get(self, k, default=None): return self.d.get(k, default)
    def set(self, k, v, ttl=None): self.d[k] = v


def _extract(ctx, cand):
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor
    ext = WikipediaFoundersExtractor()
    return ext.extract(cand, ctx) if ext.applicable(cand, ctx) else []


def test_scipy_founder_matched_by_name_default_scan():
    # The whole point: an AUTHOR role WITHOUT --discover-roles (LLM off).
    forge = _WikiForge()
    ev = _extract(_ctx(forge), Candidate("scipy/scipy", stars=15000))
    assert len(ev) == 1
    assert ev[0].role == AUTHOR and ev[0].source == "wikipedia_authors"
    assert "SciPy" in ev[0].detail
    assert ev[0].url == "https://en.wikipedia.org/wiki/SciPy"
    assert len(forge.calls) == 2                  # 1 SPARQL + 1 MediaWiki, both cached


def test_non_founder_gets_no_author_role():
    # A contributor whose name isn't in the infobox author field.
    ev = _extract(_ctx(_WikiForge(), name="Random Person"),
                  Candidate("scipy/scipy", stars=15000))
    assert ev == []


def test_gated_off_below_popularity_floor():
    forge = _WikiForge()
    ev = _extract(_ctx(forge, floor=1000), Candidate("scipy/scipy", stars=50))
    assert ev == [] and forge.calls == []         # not even a network call


def test_gated_off_when_wikidata_disabled():
    forge = _WikiForge()
    ev = _extract(_ctx(forge, use_wikidata=False), Candidate("scipy/scipy", stars=15000))
    assert ev == [] and forge.calls == []


def test_no_wikipedia_article_is_a_clean_miss():
    class NoArticle(_WikiForge):
        def get_url(self, url, accept="text/html"):
            self.calls.append(url)
            if "query.wikidata.org" in url:
                return json.dumps({"results": {"bindings": []}})
            return None
    forge = NoArticle()
    ev = _extract(_ctx(forge), Candidate("obscure/repo", stars=15000))
    assert ev == [] and len(forge.calls) == 1     # stops after the empty SPARQL


def test_founder_cache_avoids_second_wdqs_fetch():
    # #108: once a repo's authors are resolved, they're cached in the shared
    # cache and reused — no more WDQS/Wikipedia calls (which throttle cloud IPs).
    cache = _MemCache()
    forge = _WikiForge()
    cand = Candidate("scipy/scipy", stars=15000)
    ev1 = _extract(_ctx(forge, founder_cache=cache), cand)
    assert ev1 and ev1[0].role == AUTHOR
    n_after_first = len(forge.calls)
    # a DIFFERENT user scanning the same repo reuses the cache — zero new fetches
    ev2 = _extract(_ctx(_reuse := forge, name="Travis Oliphant", founder_cache=cache), cand)
    assert ev2 and ev2[0].role == AUTHOR
    assert len(forge.calls) == n_after_first          # served from founder cache


def test_fetch_failure_is_not_cached():
    # A throttled WDQS (get_url -> None) must NOT be cached as a permanent miss;
    # the next scan retries (and can succeed once the throttle lifts).
    cache = _MemCache()

    class Flaky(_WikiForge):
        def __init__(self): super().__init__(); self.fail = True
        def get_url(self, url, accept="text/html"):
            if self.fail and "query.wikidata.org" in url:
                self.calls.append(url); return None   # throttled
            return super().get_url(url, accept)
    forge = Flaky()
    cand = Candidate("scipy/scipy", stars=15000)
    assert _extract(_ctx(forge, founder_cache=cache), cand) == []   # failed, no cache
    assert cache.d == {}                                            # nothing cached
    forge.fail = False                                              # throttle lifts
    ev = _extract(_ctx(forge, founder_cache=cache), cand)
    assert ev and ev[0].role == AUTHOR                              # retried, succeeded

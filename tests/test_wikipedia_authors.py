"""Offline tests for the Wikipedia-infobox author extractor."""

import json

from praiser.extractors.base import ExtractContext
from praiser.extractors.wikipedia import (
    WikipediaFoundersExtractor,
    build_title_sparql,
    parse_article_title,
    parse_infobox_authors,
    parse_prose_founders,
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


# numba: the infobox author names only the sponsoring company, but the lead prose
# credits the individual — the case prose-scanning exists for (#124 follow-up).
NUMBA_WIKITEXT = """\
{{Infobox software
| name = Numba
| author = Continuum Analytics
| developer = Community project
}}
'''Numba''' is an open-source JIT compiler. Numba was started by \
[[Travis Oliphant]] in 2012 and has since been under active development.
"""


def test_parse_prose_founders_started_by():
    names = parse_prose_founders(NUMBA_WIKITEXT)
    assert "Travis Oliphant" in names
    assert "Continuum Analytics" not in names   # company isn't a prose "started by"


def test_parse_prose_founders_subject_verb_and_negatives():
    assert "Guido Rossum" in parse_prose_founders("Guido Rossum created the tool in 1991.")
    # lowercase actors / placeholders never produce a name
    assert parse_prose_founders("it was developed by the community over time.") == []
    assert parse_prose_founders("was started by a group of engineers.") == []


def test_prose_founder_matched_when_infobox_names_company_only():
    # numba end-to-end: infobox author = "Continuum Analytics"; prose = "started by
    # Travis Oliphant". A contributing user "Travis E. Oliphant" earns AUTHOR via the
    # relaxed match + contribution corroboration.
    forge = _WikiForge(wikitext=NUMBA_WIKITEXT, title="Numba")
    ident = Identity(primary_login="teoliphant", names={"Travis E. Oliphant"})
    ctx = ExtractContext(identity=ident, forge=forge,
                         registry=KnownProjects(projects={}),
                         use_wikidata=True, role_discovery_floor=1000)
    cand = Candidate("numba/numba", stars=11000, sources={"history"})
    ev = WikipediaFoundersExtractor().extract(cand, ctx)
    assert len(ev) == 1 and ev[0].role == AUTHOR
    assert "Numba" in ev[0].detail and "Wikipedia article" in ev[0].detail


def test_prose_founder_relaxed_match_needs_contribution():
    # Same numba prose, but a user who does NOT contribute (no roster, no history/
    # contributed source) must NOT get AUTHOR from a bare relaxed name match.
    forge = _WikiForge(wikitext=NUMBA_WIKITEXT, title="Numba")
    ident = Identity(primary_login="someone", names={"Travis E. Oliphant"})
    ctx = ExtractContext(identity=ident, forge=forge,
                         registry=KnownProjects(projects={}),
                         use_wikidata=True, role_discovery_floor=1000)
    cand = Candidate("numba/numba", stars=11000)   # no contribution source
    assert WikipediaFoundersExtractor().extract(cand, ctx) == []


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

    def repo_contributors(self, o, r, max_pages=2):  # like a real Forge (default)
        return []


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


def test_registry_title_skips_wdqs():
    # #108: with a curated Wikipedia title, the extractor never calls WDQS
    # (which throttles cloud IPs) — it goes straight to the reachable Wikipedia API.
    from praiser.registry import KnownProject, KnownProjects
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor

    class WikiOnly(_WikiForge):
        def get_url(self, url, accept="text/html"):
            self.calls.append(url)
            if "query.wikidata.org" in url:
                raise AssertionError("WDQS must not be called when a title is curated")
            if "api.php" in url:
                return json.dumps({"parse": {"wikitext": {"*": self.wikitext}}})
            return None
    reg = KnownProjects(projects={"scipy/scipy": KnownProject("scipy/scipy", wikipedia="SciPy")})
    forge = WikiOnly()
    ctx = ExtractContext(identity=Identity(primary_login="pearu", names={"Pearu Peterson"}),
                         forge=forge, registry=reg, use_wikidata=True, role_discovery_floor=1000)
    ev = WikipediaFoundersExtractor().extract(Candidate("scipy/scipy", stars=15000), ctx)
    assert ev and ev[0].role == AUTHOR
    assert all("query.wikidata.org" not in u for u in forge.calls)   # WDQS-free


def test_diag_trace_records_why_no_founder_role_when_enabled(monkeypatch):
    # PRAISER_DIAG makes a production miss observable from the stored record: it
    # traces the resolved authors + match/contribution outcome per notable repo.
    monkeypatch.setenv("PRAISER_DIAG", "1")
    ctx = _ctx(_WikiForge(), name="Random Person")   # name not in the infobox
    _extract(ctx, Candidate("scipy/scipy", stars=15000))
    notes = ctx.diag_notes()
    assert any("wiki scipy/scipy" in n and "exact=[]" in n for n in notes)


def test_diag_trace_is_empty_when_disabled(monkeypatch):
    monkeypatch.delenv("PRAISER_DIAG", raising=False)
    ctx = _ctx(_WikiForge(), name="Random Person")
    _extract(ctx, Candidate("scipy/scipy", stars=15000))
    assert ctx.diag_notes() == []


def test_zero_star_notable_repo_still_yields_founder_role_108_regression():
    # Reproduces #108 at the gate: scipy was discovered with stars=0 at
    # ATTRIBUTION time (star enrichment lags), so the founder extractor's
    # stars-only gate skipped it and pearu's Author role was lost. A notable repo
    # with stars=0 must still PRODUCE the Author role — fails if the gate reverts
    # to `candidate.stars >= floor` alone.
    from praiser.registry import KnownProject, KnownProjects
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor
    ext = WikipediaFoundersExtractor()
    ident = Identity(primary_login="pearu", names={"Pearu Peterson"})

    def role(cand, reg):
        ctx = ExtractContext(identity=ident, forge=_WikiForge(), registry=reg,
                             use_wikidata=True, role_discovery_floor=1000,
                             canonical_forks=100)
        return [e.role for e in ext.extract(cand, ctx)] if ext.applicable(cand, ctx) else []

    # (a) notable by CURATION, stars=0 and forks=0 (the exact scipy condition)
    curated = KnownProjects(projects={"scipy/scipy": KnownProject("scipy/scipy", wikipedia="SciPy")})
    assert role(Candidate("scipy/scipy", stars=0, forks=0), curated) == [AUTHOR]
    # (b) notable by FORKS, stars=0, uncurated (title resolved via WDQS fallback)
    assert role(Candidate("scipy/scipy", stars=0, forks=500), KnownProjects(projects={})) == [AUTHOR]


def test_uncurated_unpopular_repo_is_skipped():
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor
    ext = WikipediaFoundersExtractor()
    ctx = ExtractContext(identity=Identity(primary_login="pearu", names={"Pearu Peterson"}),
                         forge=_WikiForge(), registry=KnownProjects(projects={}),
                         use_wikidata=True, role_discovery_floor=1000, canonical_forks=100)
    uncurated = Candidate("random/repo", stars=0)
    assert ext.applicable(uncurated, ctx) is False   # uncurated + low stars/forks → skipped


def test_relaxed_name_match_first_last_ignoring_middle():
    from praiser.extractors.wikipedia import _relaxed_name_match
    assert _relaxed_name_match("Travis Oliphant", {"travis e. oliphant"})   # middle initial
    assert _relaxed_name_match("Travis E. Oliphant", {"travis oliphant"})
    assert not _relaxed_name_match("Travis Smith", {"travis oliphant"})     # diff last
    assert not _relaxed_name_match("Guido", {"guido van rossum"})           # single token → unsafe


def test_relaxed_founder_match_needs_contribution_corroboration():
    # #124: teoliphant ("Travis E. Oliphant") should be credited as SciPy author
    # via relaxed match — but ONLY because he also contributes to scipy.
    from praiser.registry import KnownProject, KnownProjects
    from praiser.extractors.wikipedia import WikipediaFoundersExtractor
    from praiser.forge import ContributorCount

    class ForgeWithContribs(_WikiForge):
        def __init__(self, contribs): super().__init__(); self._c = contribs
        def repo_contributors(self, o, r, max_pages=2): return self._c

    reg = KnownProjects(projects={"scipy/scipy": KnownProject("scipy/scipy", wikipedia="SciPy")})
    ident = Identity(primary_login="teoliphant", names={"Travis E. Oliphant"})

    def roles(forge):
        ctx = ExtractContext(identity=ident, forge=forge, registry=reg,
                             use_wikidata=True, role_discovery_floor=1000)
        cand = Candidate("scipy/scipy", stars=15000)
        return [e.role for e in WikipediaFoundersExtractor().extract(cand, ctx)]

    # contributes to scipy → relaxed match allowed → Author
    assert roles(ForgeWithContribs([ContributorCount("teoliphant", 200)])) == [AUTHOR]
    # NOT a contributor and not exact name → no relaxed match, no false credit
    assert roles(ForgeWithContribs([ContributorCount("someoneelse", 200)])) == []

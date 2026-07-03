import json

from praiser.extractors.base import ExtractContext
from praiser.extractors.wikidata import (
    WikidataExtractor,
    build_sparql,
    parse_people,
)
from praiser.models import AUTHOR, MAINTAINER, Candidate, Identity
from praiser.registry import KnownProjects


def _sparql_json(rows):
    # rows: list of (item_qid, prop_pid, github_handle)
    return json.dumps({"results": {"bindings": [
        {"item": {"value": f"http://www.wikidata.org/entity/{q}"},
         "prop": {"value": f"http://www.wikidata.org/prop/direct/{p}"},
         "gh": {"value": gh}}
        for q, p, gh in rows
    ]}})


class _Forge:
    def __init__(self, payload):
        self._payload = payload
        self.asked = None

    def get_url(self, url, accept=None):
        self.asked = url
        return self._payload


def _ctx(login, forge, use_wikidata=True):
    return ExtractContext(
        identity=Identity(primary_login=login), forge=forge,
        registry=KnownProjects(projects={}), use_wikidata=use_wikidata,
        role_discovery_floor=1000,
    )


# --- pure helpers ---------------------------------------------------------
def test_build_sparql_escapes_and_targets_repo():
    q = build_sparql("https://github.com/sympy/sympy")
    assert "P1324" in q and "P2037" in q
    assert "github" in q and "sympy/sympy" in q
    assert '"\\\\."' not in q  # dot is escaped for the SPARQL string, not raw


def test_parse_people():
    resp = json.loads(_sparql_json([("Q1", "P170", "Alice"), ("Q1", "P178", "bob")]))
    assert parse_people(resp) == [("alice", "P170", "http://www.wikidata.org/entity/Q1"),
                                  ("bob", "P178", "http://www.wikidata.org/entity/Q1")]


# --- extractor ------------------------------------------------------------
def test_creator_claim_maps_to_author_for_matching_handle():
    forge = _Forge(_sparql_json([("Q3488521", "P170", "teoliphant")]))
    ev = WikidataExtractor().extract(
        Candidate("numpy/numpy", stars=30000), _ctx("teoliphant", forge))
    assert len(ev) == 1
    assert ev[0].role == AUTHOR and ev[0].source == "wikidata"
    assert "creator" in ev[0].detail
    assert ev[0].url == "http://www.wikidata.org/entity/Q3488521"


def test_developer_claim_maps_to_maintainer():
    forge = _Forge(_sparql_json([("Q123", "P178", "asmeurer")]))
    ev = WikidataExtractor().extract(
        Candidate("sympy/sympy", stars=15000), _ctx("asmeurer", forge))
    assert ev[0].role == MAINTAINER and "developer" in ev[0].detail


def test_only_the_matching_handle_is_credited():
    # the SPARQL lists three developers; only the scanned identity is emitted
    forge = _Forge(_sparql_json([("Q1", "P178", "certik"),
                                 ("Q1", "P178", "asmeurer"),
                                 ("Q1", "P178", "smichr")]))
    ev = WikidataExtractor().extract(
        Candidate("sympy/sympy", stars=15000), _ctx("certik", forge))
    assert len(ev) == 1  # not asmeurer/smichr


def test_gated_by_popularity_floor():
    forge = _Forge(_sparql_json([("Q1", "P170", "someone")]))
    ext = WikidataExtractor()
    assert ext.applicable(Candidate("a/b", stars=50), _ctx("someone", forge)) is False
    assert ext.applicable(Candidate("a/b", stars=5000), _ctx("someone", forge)) is True


def test_disabled_when_use_wikidata_false():
    forge = _Forge(_sparql_json([("Q1", "P170", "someone")]))
    ctx = _ctx("someone", forge, use_wikidata=False)
    assert WikidataExtractor().applicable(Candidate("a/b", stars=5000), ctx) is False


def test_is_notable_accepts_stars_forks_or_curated():
    from praiser.extractors.base import ExtractContext
    from praiser.registry import KnownProject, KnownProjects
    from praiser.models import Candidate, Identity

    class F: pass
    reg = KnownProjects(projects={"a/curated": KnownProject("a/curated")})
    ctx = ExtractContext(identity=Identity(primary_login="u"), forge=F(),
                         registry=reg, role_discovery_floor=1000, canonical_forks=100)
    assert ctx.is_notable(Candidate("a/b", stars=1500)) is True        # by stars
    assert ctx.is_notable(Candidate("a/b", stars=0, forks=150)) is True  # by forks
    assert ctx.is_notable(Candidate("a/curated", stars=0, forks=0)) is True  # curated
    # none of the three → not notable (stars can be 0 at attribution; that's fine
    # for a genuinely small, uncurated repo)
    assert ctx.is_notable(Candidate("x/small", stars=0, forks=0)) is False

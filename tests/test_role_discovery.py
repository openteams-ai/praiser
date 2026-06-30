import ghrecord.llm as llm_mod
from ghrecord.extractors.base import ExtractContext
from ghrecord.extractors.web_roles import WebRolesAutoExtractor
from ghrecord.llm import LLM, availability
from ghrecord.models import Candidate, Identity
from ghrecord.registry import KnownProject, KnownProjects, RoleSource

parse = LLM._parse_role_sources


def test_availability_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # anthropic is installed in the dev env, so the only blocker is the key.
    assert availability() == "ANTHROPIC_API_KEY is not set"


def test_availability_ok_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert availability() is None


def test_parse_normalises_roles_and_filters_urls():
    raw = """sure, here:
    [
      {"url": "https://proj.org/team", "role": "Core Team", "label": "Team"},
      {"url": "https://proj.org/gov", "role": "Steering Committee"},
      {"url": "ftp://bad", "role": "maintainer"},
      {"role": "maintainer"}
    ] done"""
    out = parse(raw)
    assert [o["url"] for o in out] == ["https://proj.org/team", "https://proj.org/gov"]
    assert out[0]["role"] == "maintainer"
    assert out[1]["role"] == "steering_council"


def test_parse_garbage_returns_empty():
    assert parse("no json here") == []


PAGE = '<a href="https://github.com/pearu">Pearu Peterson</a>'


class _LLM:
    def discover_role_sources(self, name_with_owner, project_name=None):
        return [{"url": "https://proj.org/team", "role": "maintainer", "label": "Team"}]


class _Client:
    def get_url(self, url):
        return PAGE


def _ctx(**kw):
    defaults = dict(
        identity=Identity(primary_login="pearu"),
        client=_Client(),
        registry=KnownProjects(projects={}),
        llm=_LLM(),
        auto_discover_roles=True,
        role_discovery_floor=1000,
    )
    defaults.update(kw)
    return ExtractContext(**defaults)


def test_auto_extractor_discovers_and_matches():
    ext = WebRolesAutoExtractor()
    cand = Candidate("acme/big", stars=5000)
    assert ext.applicable(cand, _ctx())
    ev = ext.extract(cand, _ctx())
    assert len(ev) == 1
    assert ev[0].role == "maintainer"
    assert ev[0].confidence == 0.85  # handle match, auto-discovered


def test_auto_extractor_gating():
    ext = WebRolesAutoExtractor()
    big = Candidate("acme/big", stars=5000)
    assert not ext.applicable(big, _ctx(auto_discover_roles=False))
    assert not ext.applicable(big, _ctx(llm=None))
    assert not ext.applicable(Candidate("acme/small", stars=10), _ctx())
    # curated role_sources take precedence -> auto skips
    reg = KnownProjects({"acme/big": KnownProject(
        "acme/big", role_sources=[RoleSource("http://x", "maintainer")])})
    assert not ext.applicable(big, _ctx(registry=reg))

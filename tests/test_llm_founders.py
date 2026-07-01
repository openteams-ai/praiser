from praiser.extractors.base import ExtractContext
from praiser.extractors.llm_founders import LlmFoundersExtractor
from praiser.models import AUTHOR, Candidate, Identity
from praiser.registry import KnownProjects


class _LLM:
    def __init__(self, founders):
        self._founders = founders

    def discover_founders(self, name_with_owner):
        return self._founders


def _ctx(login, founders, *, names=(), stars_floor=1000, auto=True, llm=True):
    return ExtractContext(
        identity=Identity(primary_login=login, names=set(names)),
        forge=None,
        registry=KnownProjects(projects={}),
        llm=_LLM(founders) if llm else None,
        auto_discover_roles=auto,
        role_discovery_floor=stars_floor,
    )


SCIPY_FOUNDERS = [
    {"name": "Travis Oliphant", "handle": "teoliphant", "url": "https://scipy.org/history"},
    {"name": "Pearu Peterson", "handle": "pearu", "url": "https://scipy.org/history"},
    {"name": "Eric Jones", "handle": None, "url": "https://scipy.org/history"},
]


def test_credits_scanned_identity_by_handle():
    ev = LlmFoundersExtractor().extract(
        Candidate("scipy/scipy", stars=15000), _ctx("pearu", SCIPY_FOUNDERS))
    assert len(ev) == 1
    assert ev[0].role == AUTHOR and ev[0].source == "llm_founders"
    assert ev[0].confidence == 0.75
    assert "founder" in ev[0].detail


def test_credits_by_name_when_no_handle():
    # Eric Jones has no handle in the LLM output; match on full name.
    ev = LlmFoundersExtractor().extract(
        Candidate("scipy/scipy", stars=15000),
        _ctx("ejones", SCIPY_FOUNDERS, names=["Eric Jones"]))
    assert len(ev) == 1 and ev[0].confidence == 0.6


def test_does_not_credit_a_non_founder():
    # scanning someone who isn't among the founders -> nothing
    ev = LlmFoundersExtractor().extract(
        Candidate("scipy/scipy", stars=15000), _ctx("randomdev", SCIPY_FOUNDERS))
    assert ev == []


def test_gated_off_without_llm_or_discover_or_popularity():
    ext = LlmFoundersExtractor()
    cand = Candidate("scipy/scipy", stars=15000)
    assert ext.applicable(cand, _ctx("pearu", SCIPY_FOUNDERS, llm=False)) is False
    assert ext.applicable(cand, _ctx("pearu", SCIPY_FOUNDERS, auto=False)) is False
    small = Candidate("a/b", stars=10)
    assert ext.applicable(small, _ctx("pearu", SCIPY_FOUNDERS)) is False

"""Offline tests for name→username resolution (shared by CLI + web)."""

import json

from praiser.forge import UserRef
from praiser.nameresolve import looks_like_name, name_matches, resolve_name


def test_looks_like_name():
    assert looks_like_name("Ralf Gommers")
    assert looks_like_name("  Travis  Oliphant ")
    assert not looks_like_name("rgommers")
    assert not looks_like_name("teoliphant")


def test_name_matches_middle_name_tolerant():
    assert name_matches("Travis Oliphant", "Travis E. Oliphant")   # subset ✓
    assert name_matches("Travis E Oliphant", "Travis E. Oliphant")
    assert not name_matches("Victor Fomin", "FominVictor")         # not a subset
    assert not name_matches("Victor Fomin", None)


class _Forge:
    """Fake forge: canned user-search + Wikidata (get_url) + resolve_user."""
    def __init__(self, users=(), wikidata=None, profiles=None):
        self._users = list(users)
        self._wikidata = wikidata            # list of P2037 logins, or None
        self._profiles = profiles or {}

    def search_users(self, name, limit=8):
        return list(self._users)

    def get_url(self, url, accept="text/html"):
        if self._wikidata is None:
            return None
        rows = [{"login": {"value": lg}} for lg in self._wikidata]
        return json.dumps({"results": {"bindings": rows}})

    def resolve_user(self, login):
        return self._profiles.get(login, UserRef(login=login))


def test_single_matching_hit_is_confident():
    f = _Forge(users=[UserRef("teoliphant", "Travis E. Oliphant")])
    confident, cands = resolve_name(f, "Travis Oliphant")
    assert confident == "teoliphant"
    assert [c.login for c in cands] == ["teoliphant"]


def test_multiple_hits_need_disambiguation():
    f = _Forge(users=[UserRef("torvalds", "Linus Torvalds"),
                      UserRef("someone", "Linus T.")])
    confident, cands = resolve_name(f, "Linus Torvalds")
    assert confident is None
    assert [c.login for c in cands] == ["torvalds", "someone"]


def test_single_hit_that_does_not_name_match_is_not_confident():
    # GitHub ranks a loose hit first (the vfdev-5 / "FominVictor" shape).
    f = _Forge(users=[UserRef("vvomk22", "FominVictor")])
    confident, cands = resolve_name(f, "Victor Fomin")
    assert confident is None
    assert [c.login for c in cands] == ["vvomk22"]


def test_wikidata_fallback_only_when_search_empty_and_opted_in():
    f = _Forge(users=[], wikidata=["teoliphant", "teoliphant"],
               profiles={"teoliphant": UserRef("teoliphant", "Travis E. Oliphant")})
    # Off by default → no fallback, no candidates.
    assert resolve_name(f, "Travis Oliphant") == (None, [])
    # Opt-in → authoritative single P2037 mapping is confident (deduped).
    confident, cands = resolve_name(f, "Travis Oliphant", use_wikidata=True)
    assert confident == "teoliphant"
    assert [c.login for c in cands] == ["teoliphant"]


def test_wikidata_not_consulted_when_search_has_hits():
    # A non-empty search short-circuits; Wikidata (get_url) must not be needed.
    f = _Forge(users=[UserRef("a", "Some One"), UserRef("b", "Some One")],
               wikidata=["should-not-be-used"])
    confident, cands = resolve_name(f, "Some One", use_wikidata=True)
    assert confident is None
    assert [c.login for c in cands] == ["a", "b"]

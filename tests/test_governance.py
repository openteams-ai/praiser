from praiser.extractors.governance import governance_match
from praiser.models import MAINTAINER, STEERING_COUNCIL

GOV = """\
# Governance

The project is led by a Steering Council.

## Steering Council members

- @alice
- @carol

## Maintainers

- @dave maintains the build system.
"""


def test_handle_near_council_keyword_is_strong():
    m = governance_match(GOV, logins={"alice"}, names=set())
    assert m is not None
    assert m.role == STEERING_COUNCIL
    assert not m.ambiguous
    assert m.confidence >= 0.7


def test_handle_near_maintainer_keyword():
    m = governance_match(GOV, logins={"dave"}, names=set())
    assert m is not None
    assert m.role == MAINTAINER
    assert not m.ambiguous


def test_no_match_returns_none():
    assert governance_match(GOV, logins={"nobody"}, names={"zzz qqq"}) is None


def test_name_only_near_keyword_is_ambiguous():
    text = "Steering Council\n\nJane Roe is a member.\n"
    m = governance_match(text, logins=set(), names={"jane roe"})
    assert m is not None
    assert m.ambiguous

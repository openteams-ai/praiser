from praiser.extractors.maintainers import parse_maintainers, parse_owners_yaml

MAINTAINERS_TXT = """\
# Maintainers

Alice Smith <alice@example.com> (@alice)
Bob Jones (@bjones)
Carol Danvers <carol@marvel.com>
- Dave Lister @dlister
"""

OWNERS_YAML = """\
approvers:
  - alice
  - "@bob"
reviewers:
  - carol
  - dave
options:
  no_parent_owners: true
"""


def test_parse_maintainers_handles_and_emails():
    people = parse_maintainers(MAINTAINERS_TXT)
    handles = {p.handle for p in people}
    emails = {p.email for p in people}
    assert "alice" in handles
    assert "bjones" in handles
    assert "dlister" in handles
    assert "alice@example.com" in emails
    assert "carol@marvel.com" in emails


def test_parse_maintainers_name_only_line():
    people = parse_maintainers(MAINTAINERS_TXT)
    carol = next(p for p in people if p.email == "carol@marvel.com")
    assert carol.name == "Carol Danvers"


def test_parse_owners_yaml():
    owners = parse_owners_yaml(OWNERS_YAML)
    assert owners["approvers"] == ["alice", "bob"]
    assert owners["reviewers"] == ["carol", "dave"]

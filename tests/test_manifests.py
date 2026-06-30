from ghrecord.extractors.manifests import (
    authors_from_cargo,
    authors_from_composer,
    authors_from_package_json,
    authors_from_pyproject,
    maintainers_from_package_json,
    maintainers_from_pyproject,
    parse_person_string,
)

PYPROJECT_621 = """\
[project]
name = "thing"
authors = [{name = "Alice Smith", email = "alice@example.com"}]
maintainers = [{name = "Bob Jones", email = "bob@example.com"}]
"""

PYPROJECT_POETRY = """\
[tool.poetry]
name = "thing"
authors = ["Alice Smith <alice@example.com>"]
"""

PACKAGE_JSON = """\
{
  "name": "thing",
  "author": "Alice Smith <alice@example.com> (https://alice.dev)",
  "maintainers": [{"name": "Bob Jones", "email": "bob@example.com"}]
}
"""

CARGO = """\
[package]
name = "thing"
authors = ["Alice Smith <alice@example.com>", "Bob <bob@example.com>"]
"""

COMPOSER = """\
{"authors": [{"name": "Alice Smith", "email": "alice@example.com"}]}
"""


def test_parse_person_string():
    p = parse_person_string("Alice Smith <alice@example.com> (https://x)")
    assert p.name == "Alice Smith"
    assert p.email == "alice@example.com"


def test_pyproject_pep621_splits_authors_and_maintainers():
    authors = authors_from_pyproject(PYPROJECT_621)
    maints = maintainers_from_pyproject(PYPROJECT_621)
    assert {p.email for p in authors} == {"alice@example.com"}
    assert {p.email for p in maints} == {"bob@example.com"}


def test_pyproject_poetry():
    people = authors_from_pyproject(PYPROJECT_POETRY)
    assert people[0].name == "Alice Smith"
    assert people[0].email == "alice@example.com"


def test_package_json_splits_author_and_maintainers():
    authors = authors_from_package_json(PACKAGE_JSON)
    maints = maintainers_from_package_json(PACKAGE_JSON)
    assert {p.email for p in authors} == {"alice@example.com"}
    assert {p.email for p in maints} == {"bob@example.com"}


def test_cargo():
    people = authors_from_cargo(CARGO)
    assert len(people) == 2
    assert people[0].email == "alice@example.com"


def test_composer():
    people = authors_from_composer(COMPOSER)
    assert people[0].name == "Alice Smith"

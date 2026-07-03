from praiser.extractors.manifests import (
    authors_from_cargo,
    authors_from_composer,
    authors_from_package_json,
    authors_from_pyproject,
    authors_from_setup_cfg,
    authors_from_setup_py,
    maintainers_from_package_json,
    maintainers_from_pyproject,
    maintainers_from_setup_cfg,
    maintainers_from_setup_py,
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


SETUP_PY = '''
from setuptools import setup
setup(
    name="pylibtiff",
    author="Pearu Peterson",
    author_email="pearu.peterson@gmail.com",
    maintainer="Some Maintainer",
    maintainer_email="maint@example.com",
)
'''

SETUP_PY_VAR = '''
__author__ = 'Pearu Peterson'
__author_email__ = "pearu@x.org"
setup(name="x", author=__author__)
'''

SETUP_CFG = """\
[metadata]
name = thing
author = Pearu Peterson
author_email = pearu@x.org
maintainer = Bob, Carol
maintainer_email = bob@x.org, carol@x.org
"""


def test_setup_py_author_and_maintainer():
    a = authors_from_setup_py(SETUP_PY)
    m = maintainers_from_setup_py(SETUP_PY)
    assert a[0].name == "Pearu Peterson"
    assert a[0].email == "pearu.peterson@gmail.com"
    assert m[0].name == "Some Maintainer" and m[0].email == "maint@example.com"


def test_setup_py_dunder_author_fallback():
    a = authors_from_setup_py(SETUP_PY_VAR)
    assert a[0].name == "Pearu Peterson"
    assert a[0].email == "pearu@x.org"


def test_setup_cfg_metadata_and_multiple_maintainers():
    a = authors_from_setup_cfg(SETUP_CFG)
    m = maintainers_from_setup_cfg(SETUP_CFG)
    assert a[0].name == "Pearu Peterson" and a[0].email == "pearu@x.org"
    assert {p.name for p in m} == {"Bob", "Carol"}
    assert {p.email for p in m} == {"bob@x.org", "carol@x.org"}


def test_manifest_author_is_strong_when_corroborated_by_commits():
    # #123: a pyproject author name is fakeable, but committer attribution is not.
    # Matching author name + being a committer → strong (0.8); name-only → soft.
    from praiser.extractors.manifests import ManifestsExtractor
    from praiser.extractors.base import ExtractContext
    from praiser.models import Identity, Candidate
    from praiser.registry import KnownProjects
    from praiser.forge import ContributorCount

    manifest = '[project]\nname = "praiser"\nauthors = [{name = "Pearu Peterson"}]\n'

    class Forge:
        def __init__(self, committers): self.committers = committers
        def get_files(self, o, r, paths, ref=None):
            return {"pyproject.toml": manifest}
        def repo_contributors(self, o, r, max_pages=2):
            return [ContributorCount(c, 100) for c in self.committers]

    def author_conf(committers):
        ctx = ExtractContext(
            identity=Identity(primary_login="pearu", names={"Pearu Peterson"}),
            forge=Forge(committers), registry=KnownProjects(projects={}))
        ev = ManifestsExtractor().extract(Candidate("openteams-ai/praiser"), ctx)
        return next((e.confidence for e in ev if e.role == "author"), None)

    assert author_conf(["pearu"]) == 0.8          # listed AND a committer → strong
    assert author_conf(["someoneelse"]) == 0.45   # listed but not a committer → soft

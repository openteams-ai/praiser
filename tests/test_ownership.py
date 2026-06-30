from ghrecord.extractors.base import ExtractContext
from ghrecord.extractors.ownership import OwnershipExtractor
from ghrecord.models import AUTHOR, Candidate, Identity
from ghrecord.registry import KnownProjects


def _ctx():
    return ExtractContext(
        identity=Identity(primary_login="pearu"),
        client=None,
        registry=KnownProjects(projects={}),
    )


def test_owned_repo_is_authored():
    ev = OwnershipExtractor().extract(Candidate("pearu/f2py"), _ctx())
    assert len(ev) == 1
    assert ev[0].role == AUTHOR
    assert ev[0].confidence == 0.9


def test_owned_fork_is_not_authorship():
    ev = OwnershipExtractor().extract(
        Candidate("pearu/somefork", is_fork=True), _ctx())
    assert ev == []


def test_other_owner_is_not_authorship():
    assert OwnershipExtractor().extract(Candidate("numpy/numpy"), _ctx()) == []

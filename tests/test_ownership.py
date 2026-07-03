from praiser.extractors.base import ExtractContext
from praiser.extractors.ownership import OwnershipExtractor
from praiser.forge import ContributorCount
from praiser.models import AUTHOR, Candidate, Identity
from praiser.registry import KnownProjects


class _Forge:
    def __init__(self, committers=()):
        self.committers = list(committers)

    def repo_contributors(self, o, r, max_pages=2):
        return [ContributorCount(c, 50) for c in self.committers]


def _ctx(committers=("pearu",)):
    return ExtractContext(
        identity=Identity(primary_login="pearu"),
        forge=_Forge(committers),
        registry=KnownProjects(projects={}),
    )


def test_owned_repo_committed_to_is_authored():
    # #123/#124: ownership ⟂ authorship, so require committer corroboration —
    # you own it AND you commit to it → author/creator.
    ev = OwnershipExtractor().extract(Candidate("pearu/f2py"), _ctx(["pearu"]))
    assert len(ev) == 1
    assert ev[0].role == AUTHOR
    assert ev[0].confidence == 0.9


def test_owned_repo_without_commits_is_not_authorship():
    # Owning a repo you didn't write (imported/transferred code) is not authoring it.
    assert OwnershipExtractor().extract(
        Candidate("pearu/hosted"), _ctx(["someoneelse"])) == []


def test_owned_fork_is_not_authorship():
    ev = OwnershipExtractor().extract(
        Candidate("pearu/somefork", is_fork=True), _ctx())
    assert ev == []


def test_other_owner_is_not_authorship():
    assert OwnershipExtractor().extract(Candidate("numpy/numpy"), _ctx()) == []

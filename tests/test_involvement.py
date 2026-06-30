from ghrecord.extractors.base import ExtractContext
from ghrecord.models import Candidate, Identity
from ghrecord.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def _ctx(org_logins=(), canonical_stars=1000):
    return ExtractContext(
        identity=Identity(primary_login="pearu"),
        client=None,
        registry=EMPTY,
        org_logins=set(org_logins),
        canonical_stars=canonical_stars,
    )


def test_trust_users_own_repo():
    assert _ctx().trust_role_file(Candidate("pearu/thing"))


def test_trust_org_repo():
    assert _ctx(org_logins=["numpy"]).trust_role_file(Candidate("numpy/numpy"))


def test_trust_canonical_popular_repo():
    # pytorch/pytorch: not the user's org, but unmistakably the real project.
    assert _ctx().trust_role_file(Candidate("pytorch/pytorch", stars=100000))


def test_reject_vendored_copy():
    # EasyFHE / a pytorch copy: vendored history makes the user a "contributor",
    # but it's neither affiliated nor popular -> not trustworthy.
    assert not _ctx().trust_role_file(Candidate("jizhuoran/EasyFHE", stars=53))
    assert not _ctx().trust_role_file(Candidate("rando/pytorch", stars=5))

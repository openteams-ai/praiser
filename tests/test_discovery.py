from ghrecord.discovery import keep_candidate
from ghrecord.models import Candidate
from ghrecord.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def test_public_non_fork_is_kept():
    c = Candidate("acme/widget")
    assert keep_candidate(c, EMPTY, include_private=False)


def test_fork_is_dropped():
    c = Candidate("someone/cpython", is_fork=True)
    assert not keep_candidate(c, EMPTY, include_private=False)


def test_private_dropped_by_default_but_kept_with_flag():
    c = Candidate("acme/secret", is_private=True)
    assert not keep_candidate(c, EMPTY, include_private=False)
    assert keep_candidate(c, EMPTY, include_private=True)


def test_registry_seed_kept_even_if_fork_or_private():
    reg = KnownProjects.load()  # ships python/peps etc.
    c = Candidate("python/peps", is_fork=True, is_private=True)
    assert keep_candidate(c, reg, include_private=False)

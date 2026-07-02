from praiser.extractors.authors import (
    AuthorsExtractor,
    subcomponent_credits,
)
from praiser.extractors.base import ExtractContext
from praiser.models import AUTHOR, CORE_CONTRIBUTOR, Candidate, Identity
from praiser.registry import KnownProject, KnownProjects, Subcomponent

THANKS = """\
NumPy Developers
================

Pearu Peterson for f2py, numpy.distutils and help with code
Some One for the sparse module
"""


class _Forge:
    def __init__(self, files, commits=0):
        self._files = files
        self._commits = commits  # pearu's commit count, to (not) corroborate

    def get_files(self, owner, repo, paths, ref=None):
        return {p: self._files.get(p) for p in paths}

    def repo_contributors(self, owner, repo, max_pages=2):
        from praiser.forge import ContributorCount
        return [ContributorCount("pearu", self._commits)] if self._commits else []


def _ctx(files, names, subs, commits=0):
    reg = KnownProjects({"numpy/numpy": KnownProject(
        "numpy/numpy", subcomponents=subs)})
    return ExtractContext(
        identity=Identity(primary_login="pearu", names=names),
        forge=_Forge(files, commits), registry=reg,
    )


def test_subcomponent_credits_matches_named_part_only():
    subs = [Subcomponent("numpy/f2py", "author", "f2py")]
    assert subcomponent_credits("Pearu Peterson for f2py, distutils", subs) == \
        [("author", "f2py")]
    # a line that doesn't name the part -> no subcomponent authorship
    assert subcomponent_credits("Pearu Peterson for docs", subs) == []
    # whole-token: "f2py" must not match inside a larger token
    assert subcomponent_credits("worked on libf2python", subs) == []


def test_credit_naming_subcomponent_grants_author_qualified():
    # pearu is a real contributor (corroborated), so the bare credit is emitted;
    # the "for f2py" credit adds Author(f2py) alongside it.
    ctx = _ctx({"THANKS.txt": THANKS}, {"pearu peterson"},
               [Subcomponent("numpy/f2py", "author", "f2py")], commits=800)
    ev = AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx)
    roles = {(e.role, e.qualifier) for e in ev}
    assert (CORE_CONTRIBUTOR, None) in roles      # generic project credit (corroborated)
    assert (AUTHOR, "f2py") in roles              # subcomponent authorship
    author_ev = next(e for e in ev if e.role == AUTHOR)
    assert "f2py" in author_ev.detail


def test_subcomponent_author_credit_stands_without_corroboration():
    # The "for f2py" authorship credit is a specific claim — emitted even with no
    # commit corroboration; the bare listing is NOT (uncorroborated).
    ctx = _ctx({"THANKS.txt": THANKS}, {"pearu peterson"},
               [Subcomponent("numpy/f2py", "author", "f2py")], commits=0)
    ev = AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx)
    roles = {(e.role, e.qualifier) for e in ev}
    assert (AUTHOR, "f2py") in roles
    assert (CORE_CONTRIBUTOR, None) not in roles


def test_bare_listing_without_contribution_is_dropped():
    # The ngoldbaum/black class: listed in an all-contributors AUTHORS file from a
    # single PR, no real commits -> NOT credited as core (corroboration-only).
    thanks = "NumPy\n=====\n\nPearu Peterson for general help\n"
    ctx = _ctx({"THANKS.txt": thanks}, {"pearu peterson"},
               [Subcomponent("numpy/f2py", "author", "f2py")], commits=0)
    assert AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx) == []


def test_bare_listing_with_real_contribution_is_kept():
    # Same plain credit, but backed by real commits -> core_contributor (corroboration).
    thanks = "NumPy\n=====\n\nPearu Peterson for general help\n"
    ctx = _ctx({"THANKS.txt": thanks}, {"pearu peterson"}, [], commits=50)
    ev = AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx)
    assert ev and all(e.role == CORE_CONTRIBUTOR and not e.qualifier for e in ev)

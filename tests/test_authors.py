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
    def __init__(self, files):
        self._files = files

    def get_files(self, owner, repo, paths, ref=None):
        return {p: self._files.get(p) for p in paths}


def _ctx(files, names, subs):
    reg = KnownProjects({"numpy/numpy": KnownProject(
        "numpy/numpy", subcomponents=subs)})
    return ExtractContext(
        identity=Identity(primary_login="pearu", names=names),
        forge=_Forge(files), registry=reg,
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
    # numpy is canonical (trusted), so the name-only credit is accepted; the
    # "for f2py" credit yields Author(f2py) alongside the generic contributor credit.
    ctx = _ctx({"THANKS.txt": THANKS}, {"pearu peterson"},
               [Subcomponent("numpy/f2py", "author", "f2py")])
    ev = AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx)
    roles = {(e.role, e.qualifier) for e in ev}
    assert (CORE_CONTRIBUTOR, None) in roles      # generic project credit
    assert (AUTHOR, "f2py") in roles              # subcomponent authorship
    author_ev = next(e for e in ev if e.role == AUTHOR)
    assert "f2py" in author_ev.detail


def test_credit_without_subcomponent_mention_stays_contributor():
    # A person credited but NOT "for f2py" gets only the generic contributor role.
    thanks = "NumPy\n=====\n\nPearu Peterson for general help\n"
    ctx = _ctx({"THANKS.txt": thanks}, {"pearu peterson"},
               [Subcomponent("numpy/f2py", "author", "f2py")])
    ev = AuthorsExtractor().extract(Candidate("numpy/numpy", stars=32000), ctx)
    assert all(e.role == CORE_CONTRIBUTOR for e in ev)
    assert not any(e.qualifier for e in ev)

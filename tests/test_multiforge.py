from praiser.models import CODE_OWNER, Evidence, ProjectRecord
from praiser.pipeline import _dedupe


def _rec(nwo, url, forge_has_stars=True, stars=0, forks=0):
    return ProjectRecord(
        name_with_owner=nwo, url=url, stars=stars, forks=forks,
        forge_has_stars=forge_has_stars,
        evidence=[Evidence("x", CODE_OWNER, url, 0.9, "")],
    )


def test_dedupe_keeps_first_by_url():
    a = _rec("o/r", "https://github.com/o/r")
    b = _rec("o/r", "https://github.com/o/r")  # same url -> dropped
    assert _dedupe([a, b]) == [a]


def test_dedupe_keeps_same_name_on_different_forges():
    # A project named o/r on two different hosts is two distinct records
    # (URLs differ), so both survive a merged multi-forge scan.
    gh = _rec("o/r", "https://github.com/o/r")
    gl = _rec("o/r", "https://gitlab.com/o/r")
    merged = _dedupe([gh, gl])
    assert {r.url for r in merged} == {"https://github.com/o/r", "https://gitlab.com/o/r"}

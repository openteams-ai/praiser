from ghrecord.models import CODE_OWNER, Evidence, ProjectRecord
from ghrecord.popularity import (
    _is_maintained,
    filter_records,
    is_widely_used_and_maintained,
)
from ghrecord.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def _rec(name, stars=0, forks=0, pushed_at=None, conf=0.9):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}",
        stars=stars, forks=forks, pushed_at=pushed_at,
        evidence=[Evidence("codeowners", CODE_OWNER, "u", conf, "")],
    )


def test_is_maintained():
    assert _is_maintained("2026-06-01T00:00:00Z") is True   # recent (today 2026-06-30)
    assert _is_maintained("2018-01-01T00:00:00Z") is False  # stale
    assert _is_maintained(None) is True                     # unknown -> lenient
    assert _is_maintained("not-a-date") is True


def test_widely_used_needs_use_and_maintenance():
    assert is_widely_used_and_maintained(
        _rec("a/b", stars=10, forks=30, pushed_at="2026-06-01T00:00:00Z"), 100)
    # used but stale -> no
    assert not is_widely_used_and_maintained(
        _rec("a/b", stars=10, forks=30, pushed_at="2018-01-01T00:00:00Z"), 100)
    # maintained but barely used -> no
    assert not is_widely_used_and_maintained(
        _rec("a/b", stars=2, forks=1, pushed_at="2026-06-01T00:00:00Z"), 100)


def test_filter_splits_primary_and_secondary():
    popular = _rec("a/popular", stars=500)
    used = _rec("a/lib", stars=10, forks=40, pushed_at="2026-06-01T00:00:00Z")
    obscure = _rec("a/tiny", stars=1, forks=0)

    primary, secondary = filter_records(
        [popular, used, obscure], min_stars=100, registry=EMPTY
    )
    assert [r.name_with_owner for r in primary] == ["a/popular"]
    assert [r.name_with_owner for r in secondary] == ["a/lib"]
    # obscure is dropped from both

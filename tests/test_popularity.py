from ghrecord.models import AUTHOR, CODE_OWNER, Evidence, ProjectRecord
from ghrecord.popularity import (
    _is_maintained,
    filter_records,
    is_notable_authored,
    is_widely_used_and_maintained,
)
from ghrecord.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def _rec(name, stars=0, forks=0, pushed_at=None, conf=0.9, role=CODE_OWNER):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}",
        stars=stars, forks=forks, pushed_at=pushed_at,
        evidence=[Evidence("x", role, "u", conf, "")],
    )


def test_authored_project_kept_even_if_old_and_small():
    # sympycore-like: authored, 11 stars, 2 forks, dormant since 2015.
    rec = _rec("pearu/sympycore", stars=11, forks=2,
               pushed_at="2015-08-01T00:00:00Z", role=AUTHOR)
    assert not is_widely_used_and_maintained(rec, 50)  # fails the generic check
    assert is_notable_authored(rec)                    # but kept as authored


def test_throwaway_authored_repo_still_dropped():
    assert not is_notable_authored(
        _rec("pearu/callseq", stars=2, forks=0, role=AUTHOR))
    # a personal site / tiny repo with a stray fork is noise, not a role
    assert not is_notable_authored(
        _rec("pearu/pearu.github.io", stars=1, forks=2, role=AUTHOR))


def test_non_author_role_not_kept_by_authored_rule():
    assert not is_notable_authored(
        _rec("a/b", stars=8, forks=4, role=CODE_OWNER))


def test_authored_lands_in_secondary():
    rec = _rec("pearu/sympycore", stars=11, forks=2,
               pushed_at="2015-08-01T00:00:00Z", role=AUTHOR)
    primary, secondary = filter_records([rec], min_stars=50, registry=EMPTY)
    assert not primary
    assert [r.name_with_owner for r in secondary] == ["pearu/sympycore"]


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

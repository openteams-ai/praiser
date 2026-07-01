"""Tests for the web service layer (offline; no network)."""

from praiser.models import CODE_OWNER, Evidence, ProjectRecord
from praiser.pipeline import RunResult
from web.core import service


def _rec(name, stars):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}", stars=stars,
        evidence=[Evidence("x", CODE_OWNER, "u", 0.9, "")],
    )


def test_min_stars_excluded_from_data_options():
    # min_stars is a display filter, not a collection option — so it must not be
    # part of the scan/cache key (else changing it would trigger a re-scan).
    assert "min_stars" not in service.DATA_OPTIONS


def test_render_result_applies_min_stars_at_render_time():
    # A superset collected at floor 0; render-time min_stars re-splits it.
    result = RunResult(
        records=[_rec("a/big", 5000), _rec("a/mid", 200), _rec("a/small", 3)],
        secondary=[],
    )
    at0 = service.render_result(result, "u", view="json", min_stars=0)
    at1000 = service.render_result(result, "u", view="json", min_stars=1000)
    import json
    n0 = json.loads(at0)["count"]
    n1000 = json.loads(at1000)["count"]
    assert n0 == 3            # everything clears a 0 floor
    assert n1000 == 1         # only the 5000-star project clears 1000
    assert n1000 < n0         # higher threshold -> fewer primary


def test_render_result_highlights_respects_top_n():
    result = RunResult(records=[_rec(f"o/r{i}", 1000 - i) for i in range(10)],
                        secondary=[])
    out = service.render_result(result, "u", view="highlights", highlights=3,
                                min_stars=0)
    assert out.splitlines()[0] == "u — top 3 highlights:"

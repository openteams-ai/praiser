import json

from praiser.models import CODE_OWNER, CORE_CONTRIBUTOR, Evidence, ProjectRecord
from praiser.render import render_highlights, render_json, render_markdown


def _r(name, role, stars):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}", stars=stars,
        evidence=[Evidence("x", role, "u", 0.9, "")],
    )


def test_highlights_caps_lines_and_summarizes_rest():
    recs = [_r(f"o{i}/r{i}", CODE_OWNER, 1000 - i) for i in range(20)]
    out = render_highlights("alice", recs, 8)
    lines = out.splitlines()
    assert lines[0] == "alice — top 8 highlights:"
    assert "12 more elevated-role" in out
    assert "o0/r0" in lines[1] and "1k★" in lines[1]
    assert "Reach: 20 project(s) across 20 communities" in out


def test_highlights_includes_secondary_stats():
    primary = [_r("numpy/numpy", CODE_OWNER, 30000)]
    secondary = [_r("pearu/sympycore", CODE_OWNER, 11),
                 _r("data-apis/array-api-extra", CORE_CONTRIBUTOR, 30)]
    out = render_highlights("pearu", primary, 8, secondary)
    assert "2 smaller but widely-used project(s) with a notable role" in out
    # reach counts both buckets, excludes the user's own account (pearu)
    assert "Reach: 3 project(s) across 2 communities" in out


def test_highlights_no_records():
    assert "no elevated roles" in render_highlights("bob", [], 8)


def test_highlights_fewer_than_n():
    out = render_highlights("alice", [_r("acme/r", CODE_OWNER, 50)], 8)
    assert out.splitlines()[0] == "alice — top 1 highlights:"
    assert "more elevated-role" not in out


def _rec(name, role, stars=10, forks=20):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}",
        stars=stars, forks=forks,
        evidence=[Evidence("contributors", role, "u", 0.8, "x")],
    )


def test_markdown_reports_secondary_count():
    primary = [_rec("a/big", CODE_OWNER, stars=900)]
    secondary = [_rec("a/lib", CORE_CONTRIBUTOR), _rec("a/tool", CORE_CONTRIBUTOR)]
    md = render_markdown("someone", primary, secondary)
    assert "plus **2**" in md
    assert "Less-popular but widely-used & maintained (2)" in md
    assert "a/lib" in md and "a/tool" in md


def test_markdown_no_secondary_is_clean():
    md = render_markdown("someone", [_rec("a/big", CODE_OWNER, stars=900)], [])
    assert "less-popular" not in md.lower()


def test_json_includes_secondary_block():
    data = json.loads(render_json(
        "someone",
        [_rec("a/big", CODE_OWNER, stars=900)],
        [_rec("a/lib", CORE_CONTRIBUTOR)],
    ))
    assert data["count"] == 1
    assert data["secondary_count"] == 1
    assert data["secondary"][0]["project"] == "a/lib"

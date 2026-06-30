import json

from ghrecord.models import CODE_OWNER, CORE_CONTRIBUTOR, Evidence, ProjectRecord
from ghrecord.render import render_highlights, render_json, render_markdown


def _r(name, role, stars):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}", stars=stars,
        evidence=[Evidence("x", role, "u", 0.9, "")],
    )


def test_highlights_caps_lines_and_summarizes_rest():
    recs = [_r(f"o/r{i}", CODE_OWNER, 1000 - i) for i in range(20)]
    out = render_highlights("alice", recs, 8)
    lines = out.splitlines()
    assert lines[0] == "alice — top 8 highlights:"
    assert len(lines) == 1 + 8 + 1            # header + 8 + "and N more"
    assert "12 more" in lines[-1]
    assert "o/r0" in lines[1] and "1k★" in lines[1]


def test_highlights_no_records():
    assert "no elevated roles" in render_highlights("bob", [], 8)


def test_highlights_fewer_than_n():
    out = render_highlights("alice", [_r("o/r", CODE_OWNER, 50)], 8)
    assert out.splitlines()[0] == "alice — top 1 highlights:"
    assert "more" not in out


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

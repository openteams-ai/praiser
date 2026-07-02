import json

from praiser.models import (
    AUTHOR,
    CODE_OWNER,
    CONTRIBUTOR,
    CORE_CONTRIBUTOR,
    Evidence,
    ProjectRecord,
)
from praiser.render import (
    _role_display,
    render_highlights,
    render_json,
    render_markdown,
)


def _multi(name, roles, stars=100):
    """A record carrying several role-evidences on one project."""
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}", stars=stars,
        evidence=[Evidence(f"s{i}", r, "u", 0.9, "") for i, r in enumerate(roles)],
    )


def test_highlight_line_format_stars_before_roles():
    # New format: `REPO (STARS★) — ROLES`, no #R/N when standing is unknown.
    rec = _r("numpy/numpy", CODE_OWNER, 32000)
    line = render_highlights("u", [rec], 8).splitlines()[1]
    assert line == "- numpy/numpy (32k★) — Code owner"


def test_highlight_line_shows_rank_of_n_when_available():
    rec = ProjectRecord(
        name_with_owner="a/b", url="https://github.com/a/b", stars=1000,
        evidence=[Evidence("contributors", CORE_CONTRIBUTOR, "u", 0.8,
                           "50 commits (~#6 contributor)", rank=6, n_contributors=72)],
    )
    line = render_highlights("u", [rec], 8).splitlines()[1]
    assert line == "- a/b (1k★) — Core contributor (#6/72)"


def test_highlight_line_marks_capped_contributor_count():
    rec = ProjectRecord(
        name_with_owner="a/b", url="https://github.com/a/b", stars=1000,
        evidence=[Evidence("contributors", CORE_CONTRIBUTOR, "u", 0.8, "",
                           rank=64, n_contributors=200, contributors_capped=True)],
    )
    line = render_highlights("u", [rec], 8).splitlines()[1]
    assert line == "- a/b (1k★) — Core contributor (#64/200+)"


def test_highlight_link_repos_makes_markdown_link():
    rec = _r("numpy/numpy", CODE_OWNER, 32000)
    out = render_highlights("u", [rec], 8, link_repos=True)
    line = next(l for l in out.splitlines() if l.startswith("- "))
    assert line == "- [numpy/numpy](https://github.com/numpy/numpy) (32k★) — Code owner"


def test_markdown_highlights_separate_list_from_footer_with_blank_lines():
    # Regression: st.markdown lazily merges the footer onto the last list item
    # without blank-line separation ("Reach: … on the same line as REPO").
    recs = [_r(f"o/r{i}", CODE_OWNER, 100 - i) for i in range(3)]
    out = render_highlights("u", recs, 1, secondary=[_r("s/x", CODE_OWNER, 5)],
                            link_repos=True)
    assert "\n\n- [o/r0]" in out          # blank line before the list
    assert "\n\n…plus" in out             # blank line before the footer
    assert "\n\nReach:" in out            # Reach on its own paragraph
    # plain text (CLI) stays compact — no blank lines
    plain = render_highlights("u", recs, 1, secondary=[_r("s/x", CODE_OWNER, 5)])
    assert "\n\n" not in plain


def test_highlights_show_multiple_roles_without_confidence():
    rec = _multi("sympy/sympy", [AUTHOR, CORE_CONTRIBUTOR], stars=15000)
    out = render_highlights("certik", [rec], 8)
    line = out.splitlines()[1]
    assert "Author, Core contributor" in line   # both distinct roles, strongest first
    assert "15k★" in line
    assert "conf" not in out                      # confidence dropped from highlights


def test_roles_drop_weak_and_dedupe():
    # plain 'contributor' (weak) is dropped; distinct elevated roles kept.
    rec = _multi("a/b", [AUTHOR, CONTRIBUTOR, CORE_CONTRIBUTOR])
    assert rec.roles == [AUTHOR, CORE_CONTRIBUTOR]


def test_roles_shown_in_lifecycle_order_not_weight_order():
    from praiser.models import MAINTAINER
    # Maintainer outweighs Author (0.85 > 0.84), but you can't maintain what
    # isn't created — Author must come first regardless of evidence order.
    assert _multi("a/b", [MAINTAINER, AUTHOR]).roles == [AUTHOR, MAINTAINER]
    # Maintenance is the LAST lifecycle phase: maintainer comes after the
    # contributor role even though it outweighs it.
    assert _multi("a/b", [MAINTAINER, CORE_CONTRIBUTOR]).roles == [
        CORE_CONTRIBUTOR, MAINTAINER]


def test_subcomponent_role_is_qualified_in_display():
    # pearu/numpy: "Author" only via the f2py subcomponent -> show "Author (f2py)".
    rec = ProjectRecord(
        name_with_owner="numpy/numpy", url="https://github.com/numpy/numpy", stars=32000,
        evidence=[
            Evidence("subcomponents", AUTHOR, "u", 0.85, "188 commits to f2py",
                     qualifier="f2py"),
            Evidence("contributors", CORE_CONTRIBUTOR, "u", 0.8, "1107 commits"),
        ],
    )
    out = render_highlights("pearu", [rec], 8)
    assert "Author (f2py), Core contributor" in out.splitlines()[1]


def test_multiple_subcomponents_are_listed_and_capped():
    def author_of(*parts):
        return ProjectRecord(
            name_with_owner="numpy/numpy", url="u", stars=32000,
            evidence=[Evidence("subcomponents", AUTHOR, "u", 0.85, "", qualifier=p)
                      for p in parts],
        )
    assert _role_display(author_of("f2py", "numpy.distutils"), AUTHOR) == \
        "Author (f2py, numpy.distutils)"
    # capped at 3 with a "+N more"
    assert _role_display(author_of("a", "b", "c", "d", "e"), AUTHOR) == \
        "Author (a, b, c, +2 more)"


def test_whole_project_author_stays_bare_even_with_a_subcomponent():
    # If authorship is ALSO evidenced project-wide (unqualified), don't narrow it.
    rec = ProjectRecord(
        name_with_owner="pearu/pkg", url="https://github.com/pearu/pkg", stars=100,
        evidence=[
            Evidence("ownership", AUTHOR, "u", 0.9, "owns the repository"),  # no qualifier
            Evidence("subcomponents", AUTHOR, "u", 0.85, "commits to core", qualifier="core"),
        ],
    )
    assert _role_display(rec, AUTHOR) == "Author"


def test_json_exposes_roles_list():
    rec = _multi("a/b", [AUTHOR, CORE_CONTRIBUTOR])
    data = json.loads(render_json("u", [rec]))
    assert data["projects"][0]["role"] == AUTHOR              # headline unchanged
    assert data["projects"][0]["roles"] == [AUTHOR, CORE_CONTRIBUTOR]


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

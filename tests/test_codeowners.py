from praiser.extractors.codeowners import (
    CodeownersExtractor,
    all_owners,
    classify_owner,
    parse_codeowners,
)
from praiser.extractors.base import ExtractContext
from praiser.models import CODE_OWNER, Candidate, Identity
from praiser.registry import KnownProjects

SAMPLE = """\
# Default owners
*       @alice @org/core-team
*.py    @bob data@example.com
# comment only line
docs/   @carol   # inline comment
/no-owner-here
"""


def test_parse_skips_comments_and_blanks():
    rules = parse_codeowners(SAMPLE)
    patterns = [r.pattern for r in rules]
    assert patterns == ["*", "*.py", "docs/"]  # /no-owner-here has no owners


def test_inline_comments_stripped():
    rules = parse_codeowners(SAMPLE)
    docs_rule = next(r for r in rules if r.pattern == "docs/")
    assert docs_rule.owners == ["@carol"]


def test_all_owners_dedupes_preserving_order():
    rules = parse_codeowners(SAMPLE)
    assert all_owners(rules) == [
        "@alice", "@org/core-team", "@bob", "data@example.com", "@carol",
    ]


def test_classify_owner():
    assert classify_owner("@alice") == ("user", ("alice",))
    assert classify_owner("@org/core-team") == ("team", ("org", "core-team"))
    assert classify_owner("x@y.com") == ("email", ("x@y.com",))
    assert classify_owner("garbage")[0] == "unknown"


# --- extract-level: code-ownership is path-scoped (#127) --------------------

class _Forge:
    def __init__(self, text, teams=None):
        self.text, self.teams = text, teams or {}

    def get_files(self, owner, repo, paths):
        return {".github/CODEOWNERS": self.text}

    def team_members(self, org, team):
        return self.teams.get((org, team), [])


def _ctx(identity, forge):
    return ExtractContext(identity=identity, forge=forge,
                          registry=KnownProjects(projects={}))


def test_codeowner_paths_become_qualifiers():
    # bob owns two path patterns -> two scoped Code-owner evidences.
    text = "*       @alice\n*.py    @bob\ndocs/   @bob\n"
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000), _ctx(Identity(primary_login="bob"), _Forge(text)))
    assert ev and all(e.role == CODE_OWNER for e in ev)
    assert sorted(e.qualifier for e in ev) == ["*.py", "docs/"]


def test_codeowner_catchall_is_whole_project_bare():
    # A "*" catch-all owner is whole-project -> no qualifier (rendered bare).
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000),
        _ctx(Identity(primary_login="alice"), _Forge("*   @alice\n")))
    assert len(ev) == 1 and ev[0].qualifier is None


def test_codeowner_via_team_membership_is_scoped():
    forge = _Forge("src/  @org/compiler-team\n",
                   teams={("org", "compiler-team"): ["bob", "carol"]})
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000), _ctx(Identity(primary_login="bob"), forge))
    assert len(ev) == 1 and ev[0].qualifier == "src/"
    assert "compiler-team" in ev[0].detail


# --- section headers name the sub-component, collapsing paths (#138) ---------

def test_parse_attaches_section_header_blank_ends_it():
    rules = parse_codeowners("# Sparse Tensors\n/a @x\n/b @x\n\n/c @y\n")
    assert {r.pattern: r.section for r in rules} == {
        "/a": "Sparse Tensors", "/b": "Sparse Tensors", "/c": None}


def test_section_header_collapses_paths_to_one_qualifier():
    # #138: pytorch-style file — many paths under "# Sparse Tensors" render as a
    # single concise "Code owner (Sparse Tensors)" instead of raw globs.
    text = (
        "# Sparse Tensors\n"
        "/aten/src/ATen/native/sparse/ @bob\n"
        "/aten/src/ATen/SparseTensorImpl.cpp @bob\n"
        "/aten/src/ATen/SparseCsrTensorImpl.cpp @bob\n"
        "\n"
        "# Distributed\n"
        "/torch/distributed/ @bob\n"
    )
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000), _ctx(Identity(primary_login="bob"), _Forge(text)))
    assert sorted(e.qualifier for e in ev) == ["Distributed", "Sparse Tensors"]


def test_owning_the_codeowners_file_itself_is_not_code_ownership():
    # #150: owning ".github/CODEOWNERS" is meta-administrative, not a code area —
    # it must not add a role or a nonsensical ".github/CODEOWNERS" scope label.
    # (scipy: rgommers owns the CODEOWNERS file plus real sections.)
    text = (
        ".github/CODEOWNERS  @bob\n"
        "\n"
        "# Build related files\n"
        "pyproject.toml  @bob\n"
    )
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000), _ctx(Identity(primary_login="bob"), _Forge(text)))
    quals = [e.qualifier for e in ev]
    assert ".github/CODEOWNERS" not in quals
    assert quals == ["Build related files"]


def test_owning_only_the_codeowners_file_yields_no_code_owner_role():
    ev = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000),
        _ctx(Identity(primary_login="bob"), _Forge("CODEOWNERS  @bob\n")))
    assert ev == []


def test_github_comment_gating_and_fallback():
    # Only section-style comments are kept as scope labels; others fall back to path.
    text = (
        "# Pavithra, Tania & Ralf as default reviewers for Labs blogs and assets\n"
        "blog/  @bob\n"
        "\n"
        "# Build related files\n"
        "build/  @bob\n"
        "\n"
        "# *.js @someone\n"
        "src/  @bob\n"
        "\n"
        "# Dev CLI\n"
        "cli/  @bob\n"
        "\n"
        "# Testing infrastructure\n"
        "test/  @bob\n"
        "\n"
        "# Meson\n"
        "meson.build  @bob\n"
        "\n"
        "# Frontend & Backend\n"
        "web/  @bob\n"
        "\n"
        "# Build and Release\n"
        "rel/  @bob\n"
        "\n"
        "# Docs and Tutorials\n"
        "docs/  @bob\n"
        "\n"
        "# Docs as code\n"
        "docs2/  @bob\n"
        "\n"
        "*  @bob\n"
    )
    evs = CodeownersExtractor().extract(
        Candidate("o/r", stars=15000), _ctx(Identity(primary_login="bob"), _Forge(text))
    )
    quals = set(e.qualifier for e in evs)
    expected = {
        "blog/", "src/", None,  # fallbacks
        "Build related files", "Dev CLI", "Testing infrastructure", "Meson",
        "Frontend & Backend", "Build and Release", "Docs and Tutorials", "Docs as code"
    }
    assert quals == expected
    # Assert personal names did not leak
    assert not any(q and "Pavithra" in q for q in quals)


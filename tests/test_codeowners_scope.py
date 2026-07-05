from praiser.extractors.codeowners import clean_codeowners_scope, CodeownersExtractor
from praiser.extractors.base import ExtractContext
from praiser.models import Candidate, Identity
from praiser.registry import KnownProjects


def test_clean_codeowners_scope():
    # Issue #177 exact case
    assert (
        clean_codeowners_scope(
            "Pavithra, Tania & Ralf as default reviewers for Labs blogs and assets"
        )
        == "default reviewer for Labs blogs and assets"
    )

    # "Foo & Bar as maintainers" -> "maintainer"
    assert clean_codeowners_scope("Foo & Bar as maintainers") == "maintainer"

    # Regression (MUST be unchanged)
    assert clean_codeowners_scope("Build related files") == "Build related files"
    assert clean_codeowners_scope("Dev CLI") == "Dev CLI"
    assert clean_codeowners_scope("Testing infrastructure") == "Testing infrastructure"
    assert clean_codeowners_scope("Meson") == "Meson"

    # "Alice, Bob and Carol" -> None (bare "Code owner")
    assert clean_codeowners_scope("Alice, Bob and Carol") is None

    # A single-name label like "Meson" -> unchanged (not dropped as a name)
    assert clean_codeowners_scope("Meson") == "Meson"


def test_codeowners_scope_end_to_end():
    text = (
        "# Pavithra, Tania & Ralf as default reviewers for Labs blogs and assets\n"
        "blog/  @bob\n"
        "\n"
        "# Foo & Bar as maintainers\n"
        "core/  @bob\n"
        "\n"
        "# Meson\n"
        "meson.build  @bob\n"
        "\n"
        "# Alice, Bob and Carol\n"
        "misc/  @bob\n"
        "\n"
        "# Alice, Bob and Carol\n"
        "*  @bob\n"
    )

    class _MockForge:
        def get_files(self, owner, repo, paths):
            return {".github/CODEOWNERS": text}

        def team_members(self, org, team):
            return []

    ctx = ExtractContext(
        identity=Identity(primary_login="bob"),
        forge=_MockForge(),
        registry=KnownProjects(projects={}),
    )

    evs = CodeownersExtractor().extract(Candidate("o/r", stars=15000), ctx)

    quals = [e.qualifier for e in evs]

    assert "default reviewer for Labs blogs and assets" in quals
    assert "maintainer" in quals
    assert "Meson" in quals
    assert "misc/" in quals
    assert (
        None in quals
    )  # Since the last one is `*` and `Alice, Bob and Carol` is stripped, it falls back to `None`.

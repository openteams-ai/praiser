from ghrecord.extractors.codeowners import (
    all_owners,
    classify_owner,
    parse_codeowners,
)

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

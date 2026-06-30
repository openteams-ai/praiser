from ghrecord.extractors.authors import find_credit
from ghrecord.extractors.contributors import classify

THANKS = """\
SciPy Developers
================

Founding authors:
  Pearu Peterson - f2py, core architecture
  Travis Oliphant - project lead

Many thanks to all contributors.
"""


def test_classify_elevation_tiers():
    assert classify(500, 3) == 0.8      # huge volume
    assert classify(5, 7) == 0.8        # very high rank
    assert classify(30, 25) == 0.6      # solid contributor
    assert classify(3, 200) is None     # a few commits -> not elevated


def test_find_credit_name_match():
    hit = find_credit(THANKS, names={"pearu peterson"}, logins=set())
    assert hit is not None
    line, strong = hit
    assert "Pearu Peterson" in line
    assert strong is False


def test_find_credit_handle_match_is_strong():
    text = "Maintainers:\n- @pearu\n- @someone\n"
    hit = find_credit(text, names=set(), logins={"pearu"})
    assert hit is not None
    _, strong = hit
    assert strong is True


def test_find_credit_short_name_ignored():
    # Very short names are skipped to avoid false positives.
    assert find_credit("contains bob somewhere", names={"bob"}, logins=set()) is None


def test_find_credit_absent():
    assert find_credit(THANKS, names={"nobody here"}, logins={"ghost"}) is None

"""Record scoring: the strongest *supported* claim drives rank/confidence (#96)."""

from praiser.models import (
    AUTHOR,
    CORE_CONTRIBUTOR,
    MAINTAINER,
    Evidence,
    ProjectRecord,
)


def _rec(name, stars, evidence):
    return ProjectRecord(name_with_owner=name, url=f"https://github.com/{name}",
                         stars=stars, evidence=evidence)


def test_weak_high_weight_role_does_not_sink_the_record():
    # pyvtk (#96): a bare setup.py "maintainer=" field (weight 0.85 but conf 0.45)
    # must NOT become best_evidence over strongly-evidenced Author (0.84 @ 0.90).
    rec = _rec("pearu/pyvtk", 78, [
        Evidence("manifests", MAINTAINER, "u", 0.45, "maintainer name in setup.py"),
        Evidence("ownership", AUTHOR, "u", 0.90, "owns the repository"),
        Evidence("pypi", AUTHOR, "u", 0.85, "PyPI author"),
        Evidence("contributors", CORE_CONTRIBUTOR, "u", 0.80, "21 commits (#1)"),
    ])
    assert rec.best_evidence.role == AUTHOR        # strongest claim, not highest weight
    assert rec.confidence >= 0.9                   # not dragged down to 0.45
    # ranks above a 0-star repo the user merely owns (author @ 0.90)
    owned = _rec("pearu/parseonly", 0,
                 [Evidence("ownership", AUTHOR, "u", 0.90, "owns the repository")])
    assert rec.score > owned.score
    # roles DISPLAY still lists all three (unchanged by the scoring fix)
    assert rec.roles == [AUTHOR, CORE_CONTRIBUTOR, MAINTAINER]


def test_strong_high_weight_role_still_wins():
    # A well-evidenced Maintainer (0.85 @ 0.90) still outranks Author (0.84 @ 0.90).
    rec = _rec("a/b", 100, [
        Evidence("codeowners", MAINTAINER, "u", 0.90, ""),
        Evidence("ownership", AUTHOR, "u", 0.90, ""),
    ])
    assert rec.best_evidence.role == MAINTAINER    # 0.85*0.90 > 0.84*0.90


def test_tie_breaks_to_higher_weight_role():
    # Equal weight×confidence products → prefer the more senior (higher-weight) role.
    rec = _rec("a/b", 10, [
        Evidence("x", MAINTAINER, "u", 0.70, ""),         # 0.85 * 0.70 = 0.595
        Evidence("y", CORE_CONTRIBUTOR, "u", 0.85, ""),   # 0.70 * 0.85 = 0.595
    ])
    assert rec.best_evidence.role == MAINTAINER

from praiser.extractors.base import ExtractContext
from praiser.extractors.curated import CuratedRolesExtractor
from praiser.models import AUTHOR, Candidate, Identity
from praiser.registry import CuratedRole, KnownProject, KnownProjects


def _ctx(login, project):
    return ExtractContext(
        identity=Identity(primary_login=login),
        forge=None,
        registry=KnownProjects({project.name_with_owner: project}),
    )


def _sympy():
    return KnownProject(
        "sympy/sympy",
        curated_roles=[CuratedRole("certik", "author",
                                   url="https://en.wikipedia.org/wiki/SymPy",
                                   label="SymPy founder")],
    )


def test_curated_role_emitted_for_the_named_person():
    ev = CuratedRolesExtractor().extract(Candidate("sympy/sympy"),
                                         _ctx("certik", _sympy()))
    assert len(ev) == 1
    assert ev[0].role == AUTHOR and ev[0].source == "curated"
    assert ev[0].confidence == 0.95
    assert ev[0].url == "https://en.wikipedia.org/wiki/SymPy"


def test_curated_role_NOT_emitted_for_anyone_else():
    # handle-scoped: a different person on the same project gets nothing
    # (this is why we can't just point web_roles at a multi-name page).
    ev = CuratedRolesExtractor().extract(Candidate("sympy/sympy"),
                                         _ctx("asmeurer", _sympy()))
    assert ev == []


def test_not_applicable_without_curated_roles():
    proj = KnownProject("a/b")
    assert CuratedRolesExtractor().applicable(Candidate("a/b"), _ctx("x", proj)) is False


def test_curated_roles_round_trip_through_registry_json():
    proj = _sympy()
    restored = KnownProject.from_dict("sympy/sympy", proj.to_dict())
    assert restored.curated_roles[0].login == "certik"
    assert restored.curated_roles[0].role == "author"
    assert restored.curated_roles[0].url == "https://en.wikipedia.org/wiki/SymPy"


def test_merge_project_overlay_keeps_base_curation():
    # Regression: a learned/user entry with only observed popularity must NOT
    # wipe the base entry's curated role_sources / curated_roles on merge.
    base = KnownProject(
        "sympy/sympy",
        curated_roles=[CuratedRole("certik", "author")],
        role_sources=[],
    )
    overlay = KnownProject("sympy/sympy", popularity={"stars": 14730})
    merged = KnownProjects.merge_project(base, overlay)
    assert merged.popularity.get("stars") == 14730          # overlay applied
    assert any(c.login == "certik" for c in merged.curated_roles)  # base preserved

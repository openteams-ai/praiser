from praiser.extractors.base import ExtractContext
from praiser.extractors.subcomponents import SubcomponentsExtractor, path_confidence
from praiser.models import Candidate, Identity
from praiser.registry import KnownProject, KnownProjects, Subcomponent


def test_path_confidence_tiers():
    assert path_confidence(80) == 0.85
    assert path_confidence(20) == 0.7
    assert path_confidence(6) == 0.55
    assert path_confidence(3) is None


class _Client:
    def __init__(self, counts):
        self._counts = counts  # path -> commit count

    def path_commit_count(self, owner, repo, path, login, max_pages=5):
        return self._counts.get(path, 0)


def _ctx(client, registry=None, manual_subs=None):
    return ExtractContext(
        identity=Identity(primary_login="pearu"),
        forge=client,
        registry=registry or KnownProjects(projects={}),
        manual_subcomponents=manual_subs or {},
    )


def test_subcomponent_grants_contribution_not_authorship():
    # Commit volume to a path is CONTRIBUTION, never authorship — even a large
    # count and even when the registry labels the subcomponent "author".
    # (Guards the rgommers/f2py false-positive: heavy f2py committer != author.)
    reg = KnownProjects({"numpy/numpy": KnownProject(
        "numpy/numpy",
        subcomponents=[Subcomponent("numpy/f2py", "author", "f2py")])})
    ctx = _ctx(_Client({"numpy/f2py": 120}), registry=reg)
    ev = SubcomponentsExtractor().extract(Candidate("numpy/numpy"), ctx)
    assert len(ev) == 1
    assert ev[0].role == "core_contributor"      # NOT "author"
    assert ev[0].confidence == 0.85
    assert ev[0].qualifier == "f2py" and "f2py" in ev[0].detail


def test_subcomponent_below_threshold_skipped():
    reg = KnownProjects({"numpy/numpy": KnownProject(
        "numpy/numpy",
        subcomponents=[Subcomponent("numpy/f2py", "author")])})
    ctx = _ctx(_Client({"numpy/f2py": 2}), registry=reg)
    assert SubcomponentsExtractor().extract(Candidate("numpy/numpy"), ctx) == []


def test_manual_subcomponent_defaults_to_core_contributor():
    ctx = _ctx(_Client({"python": 30}),
               manual_subs={"apache/arrow": ["python"]})
    ev = SubcomponentsExtractor().extract(Candidate("apache/arrow"), ctx)
    assert ev and ev[0].role == "core_contributor"
    assert ev[0].confidence == 0.7

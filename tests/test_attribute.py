import praiser.pipeline as pipeline
from praiser.config import Config
from praiser.extractors.base import ExtractContext
from praiser.github_client import RateLimitError
from praiser.models import MAINTAINER, Candidate, Evidence, Identity
from praiser.progress import Progress
from praiser.registry import KnownProjects


class FakeExtractor:
    name = "fake"

    def __init__(self, fn):
        self._fn = fn

    def applicable(self, cand, ctx):
        return True

    def extract(self, cand, ctx):
        return self._fn(cand)


class _StubForge:
    has_stars = True

    def rate_summary(self):
        return ""


def _ctx():
    return ExtractContext(
        identity=Identity(primary_login="u"),
        forge=_StubForge(),
        registry=KnownProjects(projects={}),
    )


def _run(monkeypatch, extract_fn, n=20, jobs=8):
    monkeypatch.setattr(pipeline, "all_extractors",
                        lambda: [FakeExtractor(extract_fn)])
    cands = [Candidate(f"org/repo{i}") for i in range(n)]
    cfg = Config(username="u", jobs=jobs)
    return pipeline._attribute(cfg, cands, _ctx(), Progress(enabled=False))


def test_all_candidates_scanned_concurrently(monkeypatch):
    def ev(cand):
        return [Evidence("fake", MAINTAINER, cand.url, 0.9, "")]
    records, reset_in = _run(monkeypatch, ev, n=25, jobs=8)
    assert reset_in is None
    assert len(records) == 25
    assert {r.name_with_owner for r in records} == {f"org/repo{i}" for i in range(25)}


def test_only_matching_candidates_become_records(monkeypatch):
    def ev(cand):
        return [Evidence("fake", MAINTAINER, cand.url, 0.9, "")] if "repo1" in cand.name_with_owner else []
    records, _ = _run(monkeypatch, ev, n=20)
    # repo1, repo10..repo19 -> 11
    assert len(records) == 11


def test_rate_limit_stops_with_reset(monkeypatch):
    def ev(cand):
        if cand.name_with_owner == "org/repo5":
            raise RateLimitError("limit", reset_in=42)
        return [Evidence("fake", MAINTAINER, cand.url, 0.9, "")]
    records, reset_in = _run(monkeypatch, ev, n=30, jobs=4)
    assert reset_in == 42  # partial run signalled with the reset time


# -- scoped --refresh (speculative org-membership repos ride the cache) --------

def _cand(name, *sources):
    c = Candidate(name)
    c.sources.update(sources)
    return c


def test_is_speculative_only_when_no_person_side_source():
    assert pipeline._is_speculative(_cand("o/r", "org-repo"))
    assert pipeline._is_speculative(_cand("o/r", "org-repo", "registry"))
    assert pipeline._is_speculative(_cand("py/peps", "registry"))
    # any person-side signal makes it anchored
    assert not pipeline._is_speculative(_cand("o/r", "org-repo", "contributed"))
    assert not pipeline._is_speculative(_cand("u/own", "owned"))
    assert not pipeline._is_speculative(_cand("o/r", "manual"))
    assert not pipeline._is_speculative(_cand("o/r"))  # no sources -> not speculative


def test_refresh_scopes_speculative_repos_to_cache(monkeypatch, tmp_path):
    from praiser.cache import Cache

    anchored = [_cand("a/owned", "owned"), _cand("a/contrib", "contributed")]
    speculative = [_cand("o/r1", "org-repo"), _cand("o/r2", "org-repo"),
                   _cand("py/peps", "registry")]
    monkeypatch.setattr(pipeline, "discover",
                        lambda *a, **k: anchored + speculative)
    monkeypatch.setattr(pipeline, "org_logins", lambda *a, **k: set())
    monkeypatch.setattr(pipeline, "enrich_stars", lambda *a, **k: None)

    cache = Cache(str(tmp_path), refresh=True)
    calls = []  # (cache.refresh seen during the call, sorted candidate names)

    def fake_attr(config, cands, ctx, progress):
        calls.append((cache.refresh,
                      sorted(c.name_with_owner for c in cands)))
        return [], None
    monkeypatch.setattr(pipeline, "_attribute", fake_attr)

    forge = _StubForge()
    forge.name = "github"
    cfg = Config(username="u", refresh=True, use_package_registries=False)
    pipeline._scan_forge(
        forge, "u", Identity(primary_login="u"), cfg,
        KnownProjects(projects={}), None, Progress(enabled=False),
        is_anchor=True, index=None, cache=cache,
    )

    # anchored batch runs with refresh forced ON; speculative batch with it OFF
    assert calls[0] == (True, ["a/contrib", "a/owned"])
    assert calls[1] == (False, ["o/r1", "o/r2", "py/peps"])
    assert cache.refresh is True   # restored afterwards

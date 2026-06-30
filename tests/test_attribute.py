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


class _StubClient:
    def rate_summary(self):
        return ""


def _ctx():
    return ExtractContext(
        identity=Identity(primary_login="u"),
        client=_StubClient(),
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

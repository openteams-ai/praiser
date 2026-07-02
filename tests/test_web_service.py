"""Tests for the web service layer (offline; no network)."""

import praiser.pipeline as pipeline
from praiser.cache import Cache
from praiser.models import CODE_OWNER, Evidence, ProjectRecord
from praiser.pipeline import RunResult
from web.core import service


def _rec(name, stars):
    return ProjectRecord(
        name_with_owner=name, url=f"https://github.com/{name}", stars=stars,
        evidence=[Evidence("x", CODE_OWNER, "u", 0.9, "")],
    )


def test_min_stars_excluded_from_data_options():
    # min_stars is a display filter, not a collection option — so it must not be
    # part of the scan/cache key (else changing it would trigger a re-scan).
    assert "min_stars" not in service.DATA_OPTIONS


def test_render_result_applies_min_stars_at_render_time():
    # A superset collected at floor 0; render-time min_stars re-splits it.
    result = RunResult(
        records=[_rec("a/big", 5000), _rec("a/mid", 200), _rec("a/small", 3)],
        secondary=[],
    )
    at0 = service.render_result(result, "u", view="json", min_stars=0)
    at1000 = service.render_result(result, "u", view="json", min_stars=1000)
    import json
    n0 = json.loads(at0)["count"]
    n1000 = json.loads(at1000)["count"]
    assert n0 == 3            # everything clears a 0 floor
    assert n1000 == 1         # only the 5000-star project clears 1000
    assert n1000 < n0         # higher threshold -> fewer primary


def test_collect_serves_from_result_cache_without_scanning(monkeypatch, tmp_path):
    # A warm result-cache entry must short-circuit collect() entirely — no call
    # to pipeline.run (i.e. zero praiser HTTP work, ~1 shared-cache read).
    calls = {"run": 0}

    def _fake_run(config, cache=None, progress_cb=None, index_cache=None, populate_index=True):
        calls["run"] += 1
        return RunResult(records=[_rec("a/b", 100)], secondary=[])

    monkeypatch.setattr(pipeline, "run", _fake_run)
    monkeypatch.setattr(service, "run", _fake_run)
    rc = Cache(tmp_path)  # stand-in shared result cache

    r1 = service.collect("alice", forge="github", result_cache=rc, http_cache=Cache(tmp_path / "h"))
    r2 = service.collect("alice", forge="github", result_cache=rc, http_cache=Cache(tmp_path / "h"))
    assert calls["run"] == 1                       # second call served from cache
    assert [x.name_with_owner for x in r2.records] == ["a/b"]
    assert r1.records[0].name_with_owner == r2.records[0].name_with_owner


def test_partial_results_are_not_cached(monkeypatch, tmp_path):
    # A rate-limited (partial) scan must NOT be cached — a retry should re-scan
    # for the full record, not keep serving the incomplete one.
    runs = {"n": 0}

    def _partial_run(config, cache=None, progress_cb=None, index_cache=None, populate_index=True):
        runs["n"] += 1
        return RunResult(records=[_rec("a/b", 100)], secondary=[],
                         partial_reset_in=1800)  # rate-limited mid-scan

    monkeypatch.setattr(service, "run", _partial_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    assert runs["n"] == 2   # partial not cached -> second call re-scans


def test_result_cache_key_ignores_display_options():
    # DATA_OPTIONS (the result-cache key inputs) must exclude display options.
    for display_only in ("view", "highlights", "min_stars"):
        assert display_only not in service.DATA_OPTIONS


def test_cache_version_bump_invalidates_stored_results(monkeypatch, tmp_path):
    # Bumping CACHE_VERSION must orphan prior results (different key), so an
    # extraction-logic change doesn't keep serving stale scans.
    runs = {"n": 0}

    def _fake_run(config, cache=None, progress_cb=None, index_cache=None, populate_index=True):
        runs["n"] += 1
        return RunResult(records=[_rec("a/b", 100)], secondary=[])

    monkeypatch.setattr(service, "run", _fake_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    assert runs["n"] == 1
    monkeypatch.setattr(service, "CACHE_VERSION", service.CACHE_VERSION + 1)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    assert runs["n"] == 2   # new version -> cache miss -> re-scanned


def test_recent_scans_records_on_scan_most_recent_first(monkeypatch, tmp_path):
    # The cache keys are hashed, so this index is the only way to enumerate
    # scanned names. Recorded on an actual scan (a cache HIT is not re-recorded,
    # to avoid extra shared-cache commands per view).
    monkeypatch.setattr(service, "run",
                        lambda config, cache=None, progress_cb=None, index_cache=None, populate_index=True:
                        RunResult(records=[_rec("a/b", 100)], secondary=[]))
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("bob", forge="gitlab", result_cache=rc, http_cache=hc)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)  # HIT
    recent = service.recent_scans(result_cache=rc)
    assert [r["username"] for r in recent] == ["bob", "alice"]  # most-recent-first
    assert len(recent) == 2                                     # no dup from the hit


def test_record_recent_dedupes_to_front(tmp_path):
    rc = Cache(tmp_path)
    service._record_recent(rc, "github", "alice")
    service._record_recent(rc, "gitlab", "bob")
    service._record_recent(rc, "github", "alice")  # re-scan -> moves to front
    assert [r["username"] for r in service.recent_scans(result_cache=rc)] == \
        ["alice", "bob"]


def test_recent_scans_empty_when_no_index(tmp_path):
    assert service.recent_scans(result_cache=Cache(tmp_path)) == []


def test_render_result_highlights_respects_top_n():
    result = RunResult(records=[_rec(f"o/r{i}", 1000 - i) for i in range(10)],
                        secondary=[])
    out = service.render_result(result, "u", view="highlights", highlights=3,
                                min_stars=0)
    assert out.splitlines()[0] == "u — top 3 highlights:"


def test_scan_fallback_uses_first_token_when_ok():
    from praiser.pipeline import RunResult
    calls = []
    def fake(u, token=None, **k): calls.append(token); return RunResult(records=[_rec("a/b",100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user", "T1"), ("bot", "T2")], data_opts={}, exhausted={}, now=1000, collect_fn=fake)
    assert label == "user" and calls == ["T1"] and soonest is None


def test_scan_fallback_switches_on_rate_limit():
    from praiser.pipeline import RunResult
    from praiser.github_client import RateLimitError
    def fake(u, token=None, **k):
        if token == "T1": raise RateLimitError("x", reset_in=1800)
        return RunResult(records=[_rec("a/b",100)], secondary=[])
    exhausted = {}
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={}, exhausted=exhausted, now=1000, collect_fn=fake)
    assert label == "bot" and result is not None and soonest is None
    assert exhausted["user"] == 2800                # user marked exhausted until reset


def test_scan_fallback_partial_then_complete_on_next():
    from praiser.pipeline import RunResult
    def fake(u, token=None, **k):
        if token == "T1": return RunResult(records=[], secondary=[], partial_reset_in=600)
        return RunResult(records=[_rec("a/b",100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={}, exhausted={}, now=1000, collect_fn=fake)
    assert label == "bot" and result.partial_reset_in is None   # completed on fallback


def test_scan_fallback_all_partial_returns_best_effort_partial():
    from praiser.pipeline import RunResult
    def fake(u, token=None, **k):
        return RunResult(records=[_rec("a/b",100)], secondary=[], partial_reset_in=600)
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={}, exhausted={}, now=1000, collect_fn=fake)
    assert result is not None and result.partial_reset_in == 600  # partial preserved
    assert soonest == 1600


def test_scan_fallback_all_exhausted_returns_soonest():
    from praiser.github_client import RateLimitError
    def fake(u, token=None, **k):
        raise RateLimitError("x", reset_in=1200 if token == "T1" else 300)
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={}, exhausted={}, now=1000, collect_fn=fake)
    assert result is None and label is None and soonest == 1300   # min(1000+1200, 1000+300)


def test_scan_fallback_skips_cooling_down_token():
    from praiser.pipeline import RunResult
    calls = []
    def fake(u, token=None, **k): calls.append(token); return RunResult(records=[_rec("a/b",100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={},
        exhausted={"user": 5000}, now=1000, collect_fn=fake)
    assert label == "bot" and calls == ["T2"]

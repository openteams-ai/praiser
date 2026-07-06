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


def test_looks_like_name_detects_a_space():
    # A forge username can't contain a space, so a space = a full name to resolve.
    assert service.looks_like_name("Ralf Gommers")
    assert service.looks_like_name("  Linus Torvalds ")
    assert not service.looks_like_name("torvalds")
    assert not service.looks_like_name("rgommers")


def test_name_matches_gates_auto_scan():
    # Exact / middle-name-tolerant → match (safe to auto-scan a single hit).
    assert service.name_matches("Ralf Gommers", "Ralf Gommers")
    assert service.name_matches("Travis Oliphant", "Travis E. Oliphant")
    # The Victor Fomin case: GitHub surfaces "FominVictor" — NOT a real match, so
    # praiser must not auto-scan it.
    assert not service.name_matches("Victor Fomin", "FominVictor")
    assert not service.name_matches("Victor Fomin", None)     # no profile name
    assert not service.name_matches("Victor Fomin", "vfdev")  # unrelated name


def test_search_people_is_github_only_for_now():
    # Other forges have no user search wired up → [] (caller shows guidance).
    assert service.search_people("Ralf Gommers", forge="gitlab") == []


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


def test_filtered_records_splits_by_min_stars_and_sorts_by_score():
    # The shared helper the web card view + text renderers both build on.
    result = RunResult(
        records=[_rec("a/big", 5000), _rec("a/mid", 200), _rec("a/small", 3)],
        secondary=[],
    )
    primary, secondary = service.filtered_records(result, min_stars=1000)
    assert [r.name_with_owner for r in primary] == ["a/big"]
    # below-threshold repos are NOT primary (secondary only holds the
    # widely-used/maintained ones; a 3-star repo is dropped entirely)
    assert "a/big" not in {r.name_with_owner for r in secondary}
    # a 0 floor keeps everything as primary, score-sorted (descending)
    p0, _ = service.filtered_records(result, min_stars=0)
    assert len(p0) == 3
    assert [r.stars for r in p0] == sorted((r.stars for r in p0), reverse=True)


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


def _clock(monkeypatch, start=1000.0):
    """Monkeypatch service.time.time to a strictly-increasing counter so
    created-timestamp ordering is deterministic in tests."""
    seq = iter(range(10**6))
    monkeypatch.setattr(service.time, "time", lambda: start + next(seq))


def test_recent_scans_records_on_scan_most_recent_first(monkeypatch, tmp_path):
    # The cache keys are hashed, so the catalog is the only way to enumerate
    # scanned names. Recorded on an actual scan (a cache HIT is not re-recorded).
    _clock(monkeypatch)
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


def test_recent_scans_empty_when_no_index(tmp_path):
    assert service.recent_scans(result_cache=Cache(tmp_path)) == []


def test_catalog_records_case_insensitively(monkeypatch, tmp_path):
    # "Pearu" (phone autocapitalization) and "pearu" are the same account.
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    service._catalog_record(rc, "github", "Pearu", "key1")
    service._catalog_record(rc, "github", "pearu", "key2")
    recent = service.recent_scans(result_cache=rc)
    assert [(r["forge"], r["username"]) for r in recent] == [("github", "pearu")]


def test_cache_catalog_lists_entries_and_trash_removes_one(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    rc.set("keyA", "resultA")
    service._catalog_record(rc, "github", "alice", "keyA")
    rc.set("keyB", "resultB")
    service._catalog_record(rc, "gitlab", "bob", "keyB")
    rows = service.cache_catalog(result_cache=rc)
    assert {(r["forge"], r["username"], r["cache_id"]) for r in rows} == {
        ("github", "alice", "keyA"), ("gitlab", "bob", "keyB")}
    assert all("created" in r for r in rows)
    # Trash alice: catalog row gone + a refresh marker replaces her result; bob
    # untouched (still a normal cached result).
    assert service.trash_cache_entry("keyA", result_cache=rc) is True
    assert rc.get("keyA") == service._REFRESH_MARKER
    assert rc.get("keyB") == "resultB"
    left = service.cache_catalog(result_cache=rc)
    assert [(r["username"], r["cache_id"]) for r in left] == [("bob", "keyB")]


def test_trashed_user_next_scan_refreshes_only_that_user(monkeypatch, tmp_path):
    _clock(monkeypatch)
    seen = {}   # username -> config.refresh seen by the pipeline

    def _fake_run(config, cache=None, progress_cb=None, index_cache=None, populate_index=True):
        seen[config.username] = config.refresh
        return RunResult(records=[_rec("a/b", 100)], secondary=[])

    monkeypatch.setattr(service, "run", _fake_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("bob", forge="github", result_cache=rc, http_cache=hc)
    # Trash alice, then re-scan both. Alice's next scan is forced-refresh; bob
    # still serves from cache (run() not called again for him).
    akey = next(r["cache_id"] for r in service.cache_catalog(result_cache=rc)
                if r["username"] == "alice")
    service.trash_cache_entry(akey, result_cache=rc)
    seen.clear()
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("bob", forge="github", result_cache=rc, http_cache=hc)
    assert seen == {"alice": True}          # only alice re-ran, and with refresh=True


def test_clear_tracked_scans_removes_tracked_entries_and_catalog(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    rc.set("keyA", "rA")
    service._catalog_record(rc, "github", "alice", "keyA")
    rc.set("keyB", "rB")
    service._catalog_record(rc, "gitlab", "bob", "keyB")
    n = service.clear_tracked_scans(result_cache=rc)
    assert n == 2
    # Trash-all: each entry becomes a one-shot refresh marker (not deleted), so the
    # next scan of each user re-fetches live instead of serving stale HTTP cache.
    assert rc.get("keyA") == service._REFRESH_MARKER
    assert rc.get("keyB") == service._REFRESH_MARKER
    assert service.cache_catalog(result_cache=rc) == []


def _stats_run(config, cache=None, progress_cb=None, index_cache=None, populate_index=True):
    return RunResult(records=[_rec("a/b", 100), _rec("c/d", 200)], secondary=[])


def test_public_stats_distinct_people_and_repos_deduped(monkeypatch, tmp_path):
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run", _stats_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("bob", forge="github", result_cache=rc, http_cache=hc)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)  # HIT
    ps = service.public_stats(result_cache=rc)
    assert ps["scans"] == 2      # two actual scans (the hit doesn't count)
    assert ps["people"] == 2     # alice, bob (distinct)
    assert ps["repos"] == 2      # a/b, c/d — same repos both scans, counted once
    assert ps["orgs"] == 2       # owners a, c (distinct)


def test_org_stat_skips_scanned_users_own_account(monkeypatch, tmp_path):
    # The scanned person's own namespace is personal, not a community — skip it;
    # keep real org owners (numpy).
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run",
                        lambda config, cache=None, progress_cb=None, index_cache=None, populate_index=True:
                        RunResult(records=[_rec("torvalds/linux", 100),
                                           _rec("numpy/numpy", 200)], secondary=[]))
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("torvalds", forge="github", result_cache=rc, http_cache=hc)
    ps = service.public_stats(result_cache=rc)
    assert ps["repos"] == 2      # both repos still counted as projects
    assert ps["orgs"] == 1       # only numpy — torvalds (own account) skipped


def test_cache_pfadd_pfcount_and_clear_protects_stats(tmp_path):
    c = Cache(tmp_path)
    c.pfadd("stats:users", "a", "b", "a")   # dup 'a' -> distinct 2
    c.pfadd("stats:users", "c")
    assert c.pfcount("stats:users") == 3
    c.set("result-x", "v")
    n = c.clear(protect_prefixes=("stats:",))
    assert n == 1                           # only the non-stats entry removed
    assert c.get("result-x") is None
    assert c.pfcount("stats:users") == 3    # stats survived the wipe


def test_reset_usage_stats_clears_stats_only(monkeypatch, tmp_path):
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run", _stats_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    assert service.public_stats(result_cache=rc)["people"] == 1
    n = service.reset_usage_stats(result_cache=rc)
    assert n >= 1
    after = service.public_stats(result_cache=rc)
    assert after == {"people": 0, "repos": 0, "orgs": 0, "scans": 0}  # all zeroed
    # The cached result itself is untouched (reset is stats-only, not a cache wipe).
    assert service.cache_catalog(result_cache=rc)      # alice's catalog row remains


def test_seed_targets_set_get_normalizes(tmp_path):
    rc = Cache(tmp_path)
    saved = service.set_seed_targets(
        " NumPy \nscipy\n\n# a comment\nscipy\npandas-dev", result_cache=rc)
    assert saved == ["numpy", "scipy", "pandas-dev"]   # trim/lower/dedup/drop blanks+comments
    assert service.get_seed_targets(result_cache=rc) == ["numpy", "scipy", "pandas-dev"]


def test_next_seed_target_prefers_unseeded_then_oldest(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    service.set_seed_targets(["a", "b", "c"], result_cache=rc)
    _row = {"repos_distinct": 1, "contributors_distinct": 1}
    service.record_seed(_row, forge="github", kind="org", target="b", result_cache=rc)
    assert service.next_seed_target(result_cache=rc) == "a"   # first never-seeded, in order
    service.record_seed(_row, forge="github", kind="org", target="a", result_cache=rc)
    service.record_seed(_row, forge="github", kind="org", target="c", result_cache=rc)
    assert service.next_seed_target(result_cache=rc) == "b"   # all seeded → oldest-updated


def test_seed_targets_status_joins_catalog(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    service.set_seed_targets(["a", "b"], result_cache=rc)
    service.record_seed({"repos_distinct": 5, "contributors_distinct": 50},
                        forge="github", kind="org", target="a", result_cache=rc)
    status = {s["org"]: s for s in service.seed_targets_status(result_cache=rc)}
    assert status["a"]["seeded"] and status["a"]["repos"] == 5
    assert status["b"]["seeded"] is False


def test_seed_targets_survive_wipe(monkeypatch, tmp_path):
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run", _stats_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.set_seed_targets(["numpy", "scipy"], result_cache=rc)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.wipe_all_cache(result_cache=rc)
    assert service.get_seed_targets(result_cache=rc) == ["numpy", "scipy"]  # config kept
    assert service.cache_catalog(result_cache=rc) == []                     # cache wiped


def test_seed_budget_default_set_and_clamp(tmp_path):
    rc = Cache(tmp_path)
    assert service.get_seed_budget(result_cache=rc) == service.SEED_CHUNK_BUDGET  # default
    assert service.set_seed_budget(120, result_cache=rc) == 120
    assert service.get_seed_budget(result_cache=rc) == 120
    assert service.set_seed_budget(9999, result_cache=rc) == 500   # clamped to max
    assert service.set_seed_budget(0, result_cache=rc) == 1        # clamped to min


def test_cache_lock_acquire_release(tmp_path):
    c = Cache(tmp_path)
    assert c.acquire_lock("seed:lock", 300, value={"source": "manual"}) is True
    assert c.acquire_lock("seed:lock", 300) is False    # held
    assert c.get("seed:lock") == {"source": "manual"}   # holder identity readable
    c.release_lock("seed:lock")
    assert c.acquire_lock("seed:lock", 300) is True      # released


def test_wipe_all_cache_preserves_usage_stats(monkeypatch, tmp_path):
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run", _stats_run)
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    before = service.public_stats(result_cache=rc)
    assert before["scans"] == 1 and before["people"] == 1 and before["repos"] == 2
    assert before["orgs"] == 2      # owners a, c
    service.wipe_all_cache(result_cache=rc)
    after = service.public_stats(result_cache=rc)
    assert after == before                  # usage stats survive a full cache wipe


def test_seed_catalog_records_authoritative_distinct(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    service.record_seed({"seeded": 3, "contributors_indexed": 120,
                         "repos_distinct": 3, "contributors_distinct": 110},
                        forge="github", kind="org", target="numpy", result_cache=rc)
    service.record_seed({"seeded": 1, "contributors_indexed": 40,
                         "repos_distinct": 1, "contributors_distinct": 40},
                        forge="github", kind="repo", target="pytorch/pytorch",
                        result_cache=rc)
    rows = service.seed_catalog(result_cache=rc)
    assert {(r["target"], r["kind"], r["repos"], r["contributors"]) for r in rows} == {
        ("numpy", "org", 3, 110), ("pytorch/pytorch", "repo", 1, 40)}
    # A later run reports the grown cumulative distinct (from the coverage set),
    # which OVERWRITES with the authoritative total (never a sum).
    service.record_seed({"seeded": 2, "contributors_indexed": 90,
                         "repos_distinct": 5, "contributors_distinct": 200},
                        forge="github", kind="org", target="numpy", result_cache=rc)
    rows = service.seed_catalog(result_cache=rc)
    assert len(rows) == 2
    numpy = next(r for r in rows if r["target"] == "numpy")
    assert numpy["repos"] == 5 and numpy["contributors"] == 200
    assert numpy["updated"] >= numpy["created"]     # created kept, updated advanced


def test_seed_catalog_noop_rerun_keeps_coverage(monkeypatch, tmp_path):
    # The reported bug: a re-seed skips already-indexed repos, but the coverage set
    # still holds the totals, so the authoritative distinct count doesn't regress.
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    service.record_seed({"seeded": 30, "contributors_indexed": 804,
                         "repos_distinct": 30, "contributors_distinct": 700},
                        forge="github", kind="org", target="scipy", result_cache=rc)
    service.record_seed({"seeded": 0, "contributors_indexed": 0,
                         "repos_distinct": 30, "contributors_distinct": 700},
                        forge="github", kind="org", target="scipy", result_cache=rc)
    scipy = next(r for r in service.seed_catalog(result_cache=rc)
                 if r["target"] == "scipy")
    assert scipy["repos"] == 30 and scipy["contributors"] == 700


def test_seed_catalog_empty_when_none(tmp_path):
    assert service.seed_catalog(result_cache=Cache(tmp_path)) == []


def test_wipe_all_cache_clears_local_dir(tmp_path):
    rc = Cache(tmp_path)
    rc.set("k1", "v1")
    rc.set("k2", "v2")
    rc.set("k3", "v3")
    n = service.wipe_all_cache(result_cache=rc)
    assert n == 3
    assert rc.get("k1") is None and rc.get("k2") is None and rc.get("k3") is None


def test_wipe_all_cache_remarks_tracked_users_for_refresh(monkeypatch, tmp_path):
    # Wipe is a clean slate BUT re-marks previously-tracked users so their next
    # scan re-fetches live (the local HTTP cache it can't reach would be stale).
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    rc.set("keyA", "rA")
    service._catalog_record(rc, "github", "alice", "keyA")
    rc.set("keyB", "rB")
    service._catalog_record(rc, "gitlab", "bob", "keyB")
    service.wipe_all_cache(result_cache=rc)
    # Catalog + founder/index gone, but each known user's key is a refresh marker.
    assert service.cache_catalog(result_cache=rc) == []
    assert rc.get("keyA") == service._REFRESH_MARKER
    assert rc.get("keyB") == service._REFRESH_MARKER


def test_scan_counters_count_actual_scans_not_cache_hits(monkeypatch, tmp_path):
    _clock(monkeypatch)
    monkeypatch.setattr(service, "run",
                        lambda config, cache=None, progress_cb=None, index_cache=None, populate_index=True:
                        RunResult(records=[_rec("a/b", 100)], secondary=[]))
    rc, hc = Cache(tmp_path), Cache(tmp_path / "h")
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)
    service.collect("bob", forge="github", result_cache=rc, http_cache=hc)
    service.collect("alice", forge="github", result_cache=rc, http_cache=hc)  # HIT
    s = service.usage_summary(result_cache=rc)
    assert s["scans_total"] == 2      # two actual scans; the hit didn't count
    assert s["scans_today"] == 2
    assert s["scans_hour"] == 2       # recent-load gauge (this UTC hour)
    assert s["tracked_scans"] == 2


def test_usage_summary_reports_cheap_stats(monkeypatch, tmp_path):
    _clock(monkeypatch)
    rc = Cache(tmp_path)
    rc.set("keyA", "rA")
    service._catalog_record(rc, "github", "alice", "keyA")
    rc.set("keyB", "rB")
    service._catalog_record(rc, "gitlab", "bob", "keyB")
    s = service.usage_summary(result_cache=rc)
    assert s["tracked_scans"] == 2
    assert s["keys"] == rc.key_count()          # counts all files in the dir
    assert s["newest"] is not None and s["oldest"] is not None
    assert s["scans_total"] is None             # no scans recorded → no counter yet


def test_feedback_links_prefill_title_body_and_labels():
    import urllib.parse
    links = service.feedback_links(
        "pearu", forge="github", version="0.3.0+gabc",
        result_text="pearu — top 1 highlights:\n- numpy/numpy (32k★) — Maintainer",
        data_opts={"cross_forge": True, "wikidata": True})
    assert [ln["label"] for ln in links] == [k["button"] for k in service.FEEDBACK_KINDS]
    fp = links[0]
    assert fp["url"].startswith("https://github.com/openteams-ai/praiser/issues/new?")
    q = urllib.parse.parse_qs(fp["url"].split("?", 1)[1])
    assert q["title"] == ["[false-positive] pearu (github)"]
    assert q["labels"] == ["needs-triage,false-positive"]          # queue + sub-type
    body = q["body"][0]
    assert "forge: `github`" in body and "praiser: `0.3.0+gabc`" in body
    assert "numpy/numpy" in body                                   # scan context embedded
    assert "cross_forge=True" in body                              # options summarized
    assert "reported by:" not in body                              # no reporter when signed out
    # EVERY feedback button queues the issue for triage (the catch-all with just
    # needs-triage, the accuracy ones with needs-triage + their sub-type).
    for ln in links:
        labels = urllib.parse.parse_qs(ln["url"].split("?", 1)[1])["labels"][0]
        assert labels.split(",")[0] == "needs-triage"
    assert urllib.parse.parse_qs(links[2]["url"].split("?", 1)[1])["labels"] == \
        ["needs-triage"]                                           # catch-all: queue only


def test_feedback_links_record_reporter_when_signed_in():
    import urllib.parse
    links = service.feedback_links("pearu", forge="github", version="v",
                                   reporter="alice")
    body = urllib.parse.parse_qs(links[0]["url"].split("?", 1)[1])["body"][0]
    assert "reported by: `alice`" in body          # follow-up attribution
    assert "@alice" not in body                    # never a bare @-mention (no spam)


def test_feedback_body_truncated_under_url_cap():
    import urllib.parse
    huge = "x" * 20000
    links = service.feedback_links("u", forge="github", version="v", result_text=huge)
    assert all(len(ln["url"]) < 8000 for ln in links)              # under GitHub's GET cap
    body = urllib.parse.parse_qs(links[0]["url"].split("?", 1)[1])["body"][0]
    assert "(truncated)" in body                                   # oversized result trimmed


def test_render_result_highlights_respects_top_n():
    result = RunResult(records=[_rec(f"o/r{i}", 1000 - i) for i in range(10)],
                        secondary=[])
    out = service.render_result(result, "u", view="highlights", highlights=3,
                                min_stars=0)
    assert out.splitlines()[0] == "u — top 3 highlights:"


def test_scan_fallback_uses_first_token_when_ok():
    from praiser.pipeline import RunResult
    calls = []
    def fake(u, token=None, **k):
        calls.append(token)
        return RunResult(records=[_rec("a/b", 100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user", "T1"), ("bot", "T2")], data_opts={}, exhausted={}, now=1000, collect_fn=fake)
    assert label == "user" and calls == ["T1"] and soonest is None


def test_scan_fallback_switches_on_rate_limit():
    from praiser.pipeline import RunResult
    from praiser.github_client import RateLimitError
    def fake(u, token=None, **k):
        if token == "T1":
            raise RateLimitError("x", reset_in=1800)
        return RunResult(records=[_rec("a/b",100)], secondary=[])
    exhausted = {}
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={}, exhausted=exhausted, now=1000, collect_fn=fake)
    assert label == "bot" and result is not None and soonest is None
    assert exhausted["user"] == 2800                # user marked exhausted until reset


def test_scan_fallback_partial_then_complete_on_next():
    from praiser.pipeline import RunResult
    def fake(u, token=None, **k):
        if token == "T1":
            return RunResult(records=[], secondary=[], partial_reset_in=600)
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
    def fake(u, token=None, **k):
        calls.append(token)
        return RunResult(records=[_rec("a/b", 100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user","T1"),("bot","T2")], data_opts={},
        exhausted={"user": 5000}, now=1000, collect_fn=fake)
    assert label == "bot" and calls == ["T2"]


def test_scan_fallback_refresh_only_on_first_attempt():
    # A --refresh scan that hits the limit on the first token must NOT re-fetch
    # everything again on the fallback token (the first pass warmed the cache).
    from praiser.pipeline import RunResult
    from praiser.github_client import RateLimitError
    seen = []
    def fake(u, token=None, refresh=False, **k):
        seen.append((token, refresh))
        if token == "T1":
            raise RateLimitError("x", reset_in=1800)
        return RunResult(records=[_rec("a/b", 100)], secondary=[])
    result, label, soonest = service.scan_with_fallback(
        "u", [("user", "T1"), ("bot", "T2")], data_opts={"refresh": True},
        exhausted={}, now=1000, collect_fn=fake)
    assert label == "bot" and result is not None
    assert seen == [("T1", True), ("T2", False)]  # refresh only on the first token


def test_diagnose_external_sources_reports_reachability():
    # Probe via praiser's real client: WDQS unreachable (throttled), others ok.
    def fake_probe(url, accept):
        if "wikidata" in url:
            return False, "unreachable (throttled/blocked/timeout)"
        return True, "1234 bytes"
    diag = service.diagnose_external_sources(probe=fake_probe)
    by = {c["name"]: c for c in diag["checks"]}
    assert by["Wikidata Query Service"]["ok"] is False
    assert "unreachable" in by["Wikidata Query Service"]["detail"]
    assert by["Wikipedia API"]["ok"] is True and by["GitHub API (baseline)"]["ok"] is True
    assert diag["user_agent"]                       # praiser's UA is reported

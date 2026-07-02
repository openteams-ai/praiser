from praiser.registry import KnownProjects


def test_seed_loads_and_has_peps():
    reg = KnownProjects.load()
    peps = reg.get("python/peps")
    assert peps is not None
    convs = peps.conventions_for("enhancement_proposals")
    assert convs and convs[0].path == "peps"
    assert convs[0].header_format == "rst"
    assert peps.min_stars_override is True


def test_seed_has_web_role_sources():
    reg = KnownProjects.load()
    numpy = reg.get("numpy/numpy")
    assert numpy is not None
    urls = [s.url for s in numpy.role_sources]
    assert any("numpy.org" in u for u in urls)
    assert all(s.role for s in numpy.role_sources)


def test_role_sources_roundtrip(tmp_path):
    reg = KnownProjects.load()
    out = tmp_path / "r.json"
    reg.save(out)
    reloaded = KnownProjects.load(extra_path=out)
    pt = reloaded.get("pytorch/pytorch")
    assert pt is not None
    assert pt.role_sources[0].role == "maintainer"


def test_add_role_sources_merges_and_dedupes_and_saves(tmp_path):
    reg = KnownProjects(projects={})
    reg.add_role_sources("acme/lib", [
        {"url": "https://acme.org/team", "role": "maintainer", "label": "Team"},
        {"url": "https://acme.org/team", "role": "maintainer"},  # dup url ignored
        {"url": "https://acme.org/gov", "role": "steering_council"},
    ])
    proj = reg.get("acme/lib")
    assert [s.url for s in proj.role_sources] == [
        "https://acme.org/team", "https://acme.org/gov"]

    out = tmp_path / "r.json"
    reg.save(out)
    reloaded = KnownProjects.load(extra_path=out)
    rp = reloaded.get("acme/lib")
    assert {s.role for s in rp.role_sources} == {"maintainer", "steering_council"}


def test_case_insensitive_and_alias_lookup():
    reg = KnownProjects.load()
    assert "Python/PEPs" in reg
    assert reg.get("PYTHON/PEPS") is not None


def test_record_popularity_creates_entry_and_roundtrips(tmp_path):
    reg = KnownProjects.load()
    reg.record_popularity("someorg/newrepo", stars=1234, forks=56)
    out = tmp_path / "reg.json"
    reg.save(out)

    reloaded = KnownProjects.load(extra_path=out)
    proj = reloaded.get("someorg/newrepo")
    assert proj is not None
    assert proj.popularity["stars"] == 1234
    assert proj.popularity["forks"] == 56


def test_contributor_count_snapshot_shipped_and_roundtrips(tmp_path):
    reg = KnownProjects.load()
    # The shipped registry carries curated totals for big seeds (>500 contribs).
    assert reg.contributor_count("numpy/numpy") and reg.contributor_count("numpy/numpy") > 500
    assert reg.contributor_count("scientific-python/specs") is None   # small: no snapshot
    assert reg.contributor_count("unknown/repo") is None

    reg.record_popularity("someorg/big", stars=9, forks=9, contributors=16432)
    out = tmp_path / "reg.json"
    reg.save(out)
    reloaded = KnownProjects.load(extra_path=out)
    assert reloaded.contributor_count("someorg/big") == 16432

    # a capped total isn't persisted (record_popularity gets None for those)
    reg.record_popularity("someorg/small", stars=9, forks=9, contributors=None)
    assert reg.contributor_count("someorg/small") is None

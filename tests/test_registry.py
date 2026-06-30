from ghrecord.registry import KnownProjects


def test_seed_loads_and_has_peps():
    reg = KnownProjects.load()
    peps = reg.get("python/peps")
    assert peps is not None
    convs = peps.conventions_for("enhancement_proposals")
    assert convs and convs[0].path == "peps"
    assert convs[0].header_format == "rst"
    assert peps.min_stars_override is True


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

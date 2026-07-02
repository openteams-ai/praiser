from praiser.discovery import keep_candidate
from praiser.models import Candidate, repo_web_url
from praiser.registry import KnownProjects

EMPTY = KnownProjects(projects={})


def test_public_non_fork_is_kept():
    c = Candidate("acme/widget")
    assert keep_candidate(c, EMPTY, include_private=False)


def test_fork_is_dropped():
    c = Candidate("someone/cpython", is_fork=True)
    assert not keep_candidate(c, EMPTY, include_private=False)


def test_private_dropped_by_default_but_kept_with_flag():
    c = Candidate("acme/secret", is_private=True)
    assert not keep_candidate(c, EMPTY, include_private=False)
    assert keep_candidate(c, EMPTY, include_private=True)


def test_registry_seed_kept_even_if_fork_or_private():
    reg = KnownProjects.load()  # ships python/peps etc.
    c = Candidate("python/peps", is_fork=True, is_private=True)
    assert keep_candidate(c, reg, include_private=False)


def test_candidate_url_follows_its_forge():
    assert Candidate("a/b").url == "https://github.com/a/b"  # default
    assert Candidate("a/b", forge="gitlab").url == "https://gitlab.com/a/b"
    assert Candidate("a/b", forge="codeberg").url == "https://codeberg.org/a/b"
    # unknown forge falls back to the GitHub host rather than crashing
    assert repo_web_url("mystery", "a/b") == "https://github.com/a/b"


def test_candidate_web_host_overrides_for_self_hosted_instances():
    # A self-hosted instance's host isn't in FORGE_WEB_HOSTS, so it's stamped
    # directly onto the candidate and takes precedence over the label map.
    c = Candidate("gnome/mutter", forge="gitlab", web_host="https://gitlab.gnome.org")
    assert c.url == "https://gitlab.gnome.org/gnome/mutter"


def test_owned_fork_adds_its_parent_as_candidate():
    # #58: a personal fork of a canonical repo bridges to a project whose old
    # contributions no person-side signal surfaces. Discovery must add the
    # fork's PARENT as a candidate (the fork itself is dropped by the filter).
    from praiser.discovery import discover
    from praiser.forge import Forge, RepoMeta
    from praiser.models import Identity

    class ForkForge(Forge):
        name = "github"
        web_base = "https://github.com"
        def web_url(self, nwo): return f"https://github.com/{nwo}"
        def get_file(self, o, r, p, ref=None): return None
        def list_dir(self, o, r, p): return []
        def repository(self, o, r): return None
        def get_url(self, url, accept="text/html"): return None
        def user_repositories(self, login):
            return [RepoMeta("hpk42/pytest", stars=0, is_fork=True,
                             parent="pytest-dev/pytest")]
        def repositories_metadata(self, names):
            # the parent resolves to a popular, non-fork canonical repo
            return {n: RepoMeta(n, stars=14000, is_fork=False) for n in names}

    cands = {c.name_with_owner: c for c in discover(
        ForkForge(), Identity(primary_login="hpk42"), EMPTY,
        include_org_repos=False, use_code_search=False)}
    assert "pytest-dev/pytest" in cands           # parent added + kept (non-fork)
    assert "fork-parent" in cands["pytest-dev/pytest"].sources
    assert "hpk42/pytest" not in cands            # the fork itself is dropped

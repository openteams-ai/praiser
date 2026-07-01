"""Interface-level tests for the Forge ABC: degradation defaults."""

from praiser.forge.base import Forge, UserRef


class MinimalForge(Forge):
    """A forge implementing only the required core — the smallest valid host."""

    name = "minimal"

    def web_url(self, name_with_owner):
        return f"https://minimal.example/{name_with_owner}"

    def get_file(self, owner, repo, path, ref=None):
        return None

    def list_dir(self, owner, repo, path):
        return []

    def repository(self, owner, repo):
        return None

    def get_url(self, url, accept="text/html"):
        return None


def test_minimal_forge_instantiates_with_only_core_methods():
    # Would raise if resolve_user / user_repositories were still abstract.
    f = MinimalForge()
    assert f.name == "minimal"


def test_resolve_user_defaults_to_echoing_the_login():
    assert MinimalForge().resolve_user("alice") == UserRef(login="alice", name=None)


def test_discovery_and_analytics_defaults_are_safe():
    f = MinimalForge()
    assert f.user_repositories("alice") == []
    assert f.user_contributed_repositories("alice") == []
    assert f.user_organizations("alice") == []
    assert f.user_commit_history("alice") == []
    assert f.repo_contributors("a", "b") is None
    assert f.search_file_mentions("alice", "CODEOWNERS") == []
    assert f.merged_pr_count("a", "b", "alice") == 0
    assert f.rate_summary() == ""


def test_get_files_default_maps_paths():
    # Even with a no-op get_file, the batch default returns a keyed dict.
    assert MinimalForge().get_files("a", "b", ["x", "y"]) == {"x": None, "y": None}

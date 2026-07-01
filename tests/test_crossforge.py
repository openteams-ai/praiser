from praiser.crossforge import parse_profile_url, resolve_cross_forge
from praiser.forge import Forge, UserRef


class FakeForge(Forge):
    def __init__(self, name, links=None, names=None, pages=None):
        self.name = name
        self._links = links or {}
        self._names = names or {}
        self._pages = pages or {}  # url -> html (for hub fetching)

    def web_url(self, nwo):
        return f"https://{self.name}.test/{nwo}"

    def get_file(self, o, r, p, ref=None):
        return None

    def list_dir(self, o, r, p):
        return []

    def repository(self, o, r):
        return None

    def get_url(self, url, accept="text/html"):
        return self._pages.get(url)

    def profile_links(self, login):
        return self._links.get(login, [])

    def resolve_user(self, login):
        return UserRef(login=login, name=self._names.get(login))


def _factory(forges):
    return lambda name: forges.get(name)


# --- parse_profile_url ----------------------------------------------------
def test_parse_profile_url_accepts_single_segment_on_known_hosts():
    assert parse_profile_url("https://gitlab.com/johnsmith") == ("gitlab", "johnsmith")
    assert parse_profile_url("https://github.com/jsmith/") == ("github", "jsmith")
    assert parse_profile_url("https://www.gitee.com/foo?tab=x") == ("gitee", "foo")


def test_parse_profile_url_rejects_repos_nonprofiles_and_unknown_hosts():
    assert parse_profile_url("https://github.com/owner/repo") is None   # repo, 2 segments
    assert parse_profile_url("https://github.com/sponsors/x") is None   # non-profile
    assert parse_profile_url("https://github.com/foo.git") is None      # file-ish
    assert parse_profile_url("https://twitter.com/someone") is None     # unknown host
    assert parse_profile_url("not-a-url") is None


# --- resolve_cross_forge --------------------------------------------------
def test_bidirectional_link_merges():
    gh = FakeForge("github", links={"jsmith": ["https://gitlab.com/johnsmith",
                                               "https://twitter.com/js"]},
                   names={"jsmith": "John Smith"})
    gl = FakeForge("gitlab", links={"johnsmith": ["https://github.com/jsmith"]})
    ident, ids = resolve_cross_forge(gh, "jsmith", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "jsmith"), ("gitlab", "johnsmith")}
    assert ident.logins == {"jsmith", "johnsmith"}
    assert "john smith" in ident.names


def test_one_way_link_does_NOT_merge():
    # github links to gitlab, but gitlab does not link back -> refuse the merge.
    gh = FakeForge("github", links={"a": ["https://gitlab.com/b"]})
    gl = FakeForge("gitlab", links={"b": ["https://gitlab.com/c"]})  # not back to a
    ident, ids = resolve_cross_forge(gh, "a", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "a")}
    assert ident.logins == {"a"}


def test_transitive_resolution_with_bidirectional_hops():
    gh = FakeForge("github", links={"A": ["https://gitlab.com/B"]})
    gl = FakeForge("gitlab", links={"B": ["https://github.com/A",
                                          "https://codeberg.org/C"]})
    cb = FakeForge("codeberg", links={"C": ["https://gitlab.com/B"]})
    _, ids = resolve_cross_forge(gh, "A", _factory({"github": gh, "gitlab": gl, "codeberg": cb}))
    assert set(ids) == {("github", "A"), ("gitlab", "B"), ("codeberg", "C")}


def test_unknown_forge_is_skipped():
    gh = FakeForge("github", links={"a": ["https://sr.ht/~a"]})  # no factory entry
    _, ids = resolve_cross_forge(gh, "a", _factory({"github": gh}))
    assert set(ids) == {("github", "a")}


HUB = "https://ondrejcertik.com/"
# a personal site linking BOTH the anchor and a GitLab account
HUB_PAGE = 'see <a href="https://github.com/certik">gh</a> and ' \
           '<a href="https://gitlab.com/certik">gl</a>'


def test_personal_site_hub_merges_same_handle():
    # certik case: GitHub bio links only his site; the site links github+gitlab;
    # gitlab profile does NOT link back — bidirectional fails, but the owned hub
    # (links back to the anchor) + same handle confirms it.
    gh = FakeForge("github", links={"certik": [HUB]}, names={"certik": "Ondřej Čertík"},
                   pages={HUB: HUB_PAGE})
    gl = FakeForge("gitlab", names={"certik": "Ondřej Čertík"})  # no back-link
    _, ids = resolve_cross_forge(gh, "certik", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "certik"), ("gitlab", "certik")}


def test_personal_site_hub_merges_on_name_when_handle_differs():
    page = 'me: <a href="https://github.com/jsmith">gh</a> ' \
           '<a href="https://gitlab.com/johnsmith">gl</a>'
    gh = FakeForge("github", links={"jsmith": ["https://me.example/"]},
                   names={"jsmith": "John Smith"}, pages={"https://me.example/": page})
    gl = FakeForge("gitlab", names={"johnsmith": "John Smith"})  # name matches
    _, ids = resolve_cross_forge(gh, "jsmith", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "jsmith"), ("gitlab", "johnsmith")}


def test_hub_does_NOT_merge_unrelated_account():
    # The site also links a collaborator with a different handle AND name -> skip.
    page = '<a href="https://github.com/certik">me</a> ' \
           '<a href="https://gitlab.com/somebodyelse">colleague</a>'
    gh = FakeForge("github", links={"certik": [HUB]}, names={"certik": "Ondřej Čertík"},
                   pages={HUB: page})
    gl = FakeForge("gitlab", names={"somebodyelse": "Somebody Else"})
    _, ids = resolve_cross_forge(gh, "certik", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "certik")}  # collaborator not merged


def test_hub_not_owned_is_ignored():
    # A site that does NOT link back to any confirmed account isn't the person's
    # hub (could be any page they linked) -> don't trust its accounts.
    page = '<a href="https://gitlab.com/certik">gl</a>'  # no link to github/certik
    gh = FakeForge("github", links={"certik": [HUB]}, names={"certik": "Ondřej Čertík"},
                   pages={HUB: page})
    gl = FakeForge("gitlab", names={"certik": "Ondřej Čertík"})
    _, ids = resolve_cross_forge(gh, "certik", _factory({"github": gh, "gitlab": gl}))
    assert set(ids) == {("github", "certik")}


def test_link_farm_hub_is_skipped():
    # A hub referencing many distinct accounts is a directory, not one identity.
    many = " ".join(f'<a href="https://gitlab.com/u{i}">x</a>' for i in range(8))
    page = '<a href="https://github.com/certik">me</a> ' + many
    gh = FakeForge("github", links={"certik": [HUB]}, names={"certik": "Ondřej Čertík"},
                   pages={HUB: page})
    factory = _factory({"github": gh, "gitlab": FakeForge("gitlab")})
    _, ids = resolve_cross_forge(gh, "certik", factory)
    assert set(ids) == {("github", "certik")}


def test_no_links_returns_just_the_anchor():
    gh = FakeForge("github", names={"solo": "Solo Dev"})
    ident, ids = resolve_cross_forge(gh, "solo", _factory({"github": gh}))
    assert ids == [("github", "solo")]
    assert ident.logins == {"solo"} and "solo dev" in ident.names

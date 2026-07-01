"""Cross-forge identity resolution (issues #18, #25).

From a single anchor account, discover the person's accounts on *other* forges,
confirming each in one of two false-merge-resistant ways:

* **Bidirectional profile links** (#18): the candidate's profile links back to an
  already-confirmed account. A false merge would need two different people to
  link to each other.
* **Personal-site hub** (#25): people often list their accounts on a personal
  site rather than in a forge bio. When a confirmed profile links to a non-forge
  URL, we fetch that page (one hop, cached) and, if it's an *owned* hub (it also
  links back to a confirmed account), accept the other forge accounts it lists —
  provided the candidate shares the handle or display name. A link-farm guard
  skips hubs that reference many distinct accounts.

Under-merge (someone who didn't cross-link at all) is safe; over-merge is
refused. The traversal is forge-agnostic (operates on ``Forge.profile_links`` +
a URL parser + ``Forge.get_url`` for the hub), so it's unit-testable with fakes.
"""

import re

from .forge import Forge
from .forge._http import extract_urls
from .models import FORGE_WEB_HOSTS, Identity

# A personal-site hub linking to more distinct forge accounts than this is
# probably a directory/link-farm, not one person's identity page — skip it.
_MAX_HUB_ACCOUNTS = 6

# host (no scheme, no www.) -> forge name
_HOST_FORGE = {
    host.split("://", 1)[1].lower(): name for name, host in FORGE_WEB_HOSTS.items()
}
# First-path-segment values that are never a user profile.
_NON_PROFILE = {
    "sponsors", "orgs", "users", "topics", "features", "about", "settings",
    "marketplace", "apps", "explore", "help", "pricing", "-", "dashboard",
}
# A profile URL is host + exactly ONE path segment (the login).
_PROFILE_RE = re.compile(r"https?://([^/\s]+)/([^/?#\s]+)/?(?:[?#].*)?$")


def parse_profile_url(url: str) -> tuple[str, str] | None:
    """``(forge_name, login)`` for a profile URL on a known host, else None.

    Only single-segment paths match (a profile), so repo/group links like
    ``github.com/owner/repo`` are ignored.
    """
    if not url:
        return None
    m = _PROFILE_RE.match(url.strip())
    if not m:
        return None
    host, seg = m.group(1).lower(), m.group(2)
    if host.startswith("www."):
        host = host[4:]
    forge = _HOST_FORGE.get(host)
    if forge is None:
        return None
    if not seg or seg.lower() in _NON_PROFILE or "." in seg:  # skip *.git, files
        return None
    return (forge, seg)


def resolve_cross_forge(
    anchor: Forge,
    anchor_login: str,
    forge_factory,
    *,
    max_accounts: int = 8,
) -> tuple[Identity, list[tuple[str, str]]]:
    """Resolve the person's accounts across forges from an anchor account.

    ``forge_factory(forge_name) -> Forge | None`` builds a forge for a name
    (None if unsupported). Returns the merged ``Identity`` and the confirmed
    ``[(forge_name, login), …]`` (anchor included), for the executor to scan.
    """
    forges: dict[str, Forge] = {anchor.name: anchor}
    confirmed: dict[tuple[str, str], str] = {(anchor.name, anchor_login.lower()): anchor_login}
    names: set[str] = set()
    visited: set[tuple[str, str]] = set()
    visited_hubs: set[str] = set()

    def links_of(forge: Forge, login: str) -> list[str]:
        # A throttled/flaky forge contributes no links rather than aborting the
        # whole resolution (best-effort, like discovery).
        try:
            return forge.profile_links(login)
        except Exception:
            return []

    def note_name(forge: Forge, login: str) -> None:
        try:
            user = forge.resolve_user(login)
        except Exception:
            return
        if user and user.name:
            names.add(user.name)

    def get_forge(fname: str) -> Forge | None:
        if fname not in forges:
            cforge = forge_factory(fname)
            if cforge is None:
                return None
            forges[fname] = cforge
        return forges[fname]

    def confirm(cforge: Forge, clogin: str) -> None:
        confirmed[(cforge.name, clogin.lower())] = clogin
        note_name(cforge, clogin)
        queue.append((cforge, clogin))

    def name_matches(cforge: Forge, clogin: str) -> bool:
        try:
            user = cforge.resolve_user(clogin)
        except Exception:
            return False
        lowered = {n.strip().lower() for n in names}
        return bool(user and user.name and user.name.strip().lower() in lowered)

    note_name(anchor, anchor_login)
    queue: list[tuple[Forge, str]] = [(anchor, anchor_login)]
    while queue and len(confirmed) < max_accounts:
        forge, login = queue.pop(0)
        if (forge.name, login.lower()) in visited:
            continue
        visited.add((forge.name, login.lower()))

        forge_targets: list[tuple[str, str]] = []
        hub_urls: list[str] = []
        for url in links_of(forge, login):
            parsed = parse_profile_url(url)
            if parsed is not None:
                forge_targets.append(parsed)
            elif url.strip().lower().startswith(("http://", "https://")):
                hub_urls.append(url.strip())

        # 1. Direct bidirectional: the candidate's profile links back.
        for fname, clogin in forge_targets:
            if (fname, clogin.lower()) in confirmed:
                continue
            cforge = get_forge(fname)
            if cforge is None:
                continue
            back = {
                (f, ll.lower()) for u2 in links_of(cforge, clogin)
                if (p := parse_profile_url(u2)) for f, ll in [p]
            }
            if set(confirmed) & back:
                confirm(cforge, clogin)

        # 2. Personal-site hub (#25): fetch the site, and if it's an owned hub
        # (links back to a confirmed account), accept the accounts it lists that
        # share the handle or display name.
        for hub in hub_urls:
            if hub in visited_hubs:
                continue
            visited_hubs.add(hub)
            try:
                page = forge.get_url(hub)
            except Exception:
                page = None
            hub_accounts = {
                p for u2 in extract_urls(page or "") if (p := parse_profile_url(u2))
            }
            if not hub_accounts or len(hub_accounts) > _MAX_HUB_ACCOUNTS:
                continue  # empty, or a link-farm — not a personal identity hub
            hub_keys = {(f, ll.lower()) for f, ll in hub_accounts}
            if not (set(confirmed) & hub_keys):
                continue  # not reached-from + links-back: not the person's own hub
            confirmed_logins = {cl for (_, cl) in confirmed}
            for fname, clogin in hub_accounts:
                if (fname, clogin.lower()) in confirmed:
                    continue
                cforge = get_forge(fname)
                if cforge is None:
                    continue
                if clogin.lower() in confirmed_logins or name_matches(cforge, clogin):
                    confirm(cforge, clogin)

    identity = Identity(
        primary_login=anchor_login,
        logins=set(confirmed.values()),
        names=names,
    )
    ids = [(fname, login) for (fname, _), login in confirmed.items()]
    return identity, ids

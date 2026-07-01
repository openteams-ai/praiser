"""Cross-forge identity resolution (issue #18).

From a single anchor account, discover the person's accounts on *other* forges
by following the links they publish on their own profiles — keeping only links
that are **confirmed bidirectionally** (the target profile links back to an
already-confirmed account). Owner-published + mutually-confirmed ⇒ a false merge
would need two different people to link to each other, which doesn't happen.
Under-merge (someone who didn't cross-link) is safe; over-merge is refused.

The traversal here is forge-agnostic — it operates on ``Forge.profile_links``
and a URL→``(forge, login)`` parser — so it's fully unit-testable with fakes.
The forge-specific part is each backend's ``profile_links`` implementation.
"""

import re

from .forge import Forge
from .models import FORGE_WEB_HOSTS, Identity

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

    note_name(anchor, anchor_login)
    queue: list[tuple[Forge, str]] = [(anchor, anchor_login)]
    while queue and len(confirmed) < max_accounts:
        forge, login = queue.pop(0)
        if (forge.name, login.lower()) in visited:
            continue
        visited.add((forge.name, login.lower()))
        for url in links_of(forge, login):
            parsed = parse_profile_url(url)
            if parsed is None:
                continue
            fname, clogin = parsed
            key = (fname, clogin.lower())
            if key in confirmed:
                continue
            cforge = forges.get(fname) or forge_factory(fname)
            if cforge is None:
                continue
            forges[fname] = cforge
            # Bidirectional: the candidate must link back to a confirmed account.
            back = {
                p for u2 in links_of(cforge, clogin)
                if (p := parse_profile_url(u2))
            }
            back_keys = {(f, ll.lower()) for f, ll in back}
            if set(confirmed) & back_keys:
                confirmed[key] = clogin
                note_name(cforge, clogin)
                queue.append((cforge, clogin))

    identity = Identity(
        primary_login=anchor_login,
        logins=set(confirmed.values()),
        names=names,
    )
    ids = [(fname, login) for (fname, _), login in confirmed.items()]
    return identity, ids

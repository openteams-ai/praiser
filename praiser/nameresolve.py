"""Resolve a full name → forge username (shared by the CLI and the web UI).

A person can be looked up by name instead of a handle. The primary, cheap signal
is the forge's own user search (profile name/login/email); an OPT-IN Wikidata
``P2037`` ("GitHub username") fallback covers *notable* people whose GitHub
profile name differs from their real name (e.g. a Travis-Oliphant-tier person),
and only runs when the primary search finds nothing. Hard cases — a pseudonymous
handle with unlinked commit emails — are allowed to fail: the caller then guides
the user to enter the exact username. Never auto-scans a low-confidence match.
"""

import json
import re
import urllib.parse

from .forge import Forge, UserRef

_WDQS = "https://query.wikidata.org/sparql"


def looks_like_name(query: str) -> bool:
    """Whether ``query`` is a full name to resolve rather than a handle to scan.
    Forge usernames never contain spaces, so a space is an unambiguous signal."""
    return " " in query.strip()


def name_matches(query: str, name: str | None) -> bool:
    """Whether a candidate's profile ``name`` genuinely matches the searched
    ``query`` — every query token present in the name (order/middle-name tolerant).
    Used to decide whether a *single* search hit is safe to auto-scan: the forge
    can rank a loose hit first even when it's the wrong person, so we only
    auto-scan when the name really lines up; otherwise the caller confirms."""
    if not name:
        return False
    q = {t for t in re.split(r"[\s.]+", query.lower()) if t}
    n = {t for t in re.split(r"[\s.]+", name.lower()) if t}
    return bool(q) and q <= n


def _wikidata_logins(forge: Forge, name: str, limit: int = 5) -> list[str]:
    """GitHub usernames (Wikidata P2037) of humans whose label equals ``name``,
    deduped in order. [] on any failure (WDQS throttles cloud IPs — a miss is
    fine, it's only a fallback). Fetched via the forge's cached HTTP client."""
    sparql = (
        "SELECT ?login WHERE { "
        "?p wdt:P31 wd:Q5 ; rdfs:label ?l ; wdt:P2037 ?login . "
        f'FILTER(LCASE(STR(?l)) = LCASE({json.dumps(name)})) }} LIMIT {limit}'
    )
    url = f"{_WDQS}?format=json&query={urllib.parse.quote(sparql)}"
    body = forge.get_url(url, accept="application/sparql-results+json")
    if not body:
        return []
    try:
        rows = (json.loads(body).get("results") or {}).get("bindings", [])
    except ValueError:
        return []
    out: list[str] = []
    for row in rows:
        login = (row.get("login") or {}).get("value")
        if login and login not in out:
            out.append(login)
    return out


def resolve_name(forge: Forge, name: str, *, use_wikidata: bool = False,
                 limit: int = 8) -> tuple[str | None, list[UserRef]]:
    """Resolve ``name`` to ``(confident_login, candidates)``.

    ``confident_login`` is set only when it's safe to scan without asking: a lone
    forge-search hit whose profile name matches, or (Wikidata fallback) a single
    authoritative P2037 mapping. Otherwise it's None and ``candidates`` is a short
    list for the caller to present (pick one), possibly empty (no match → guide
    the user to the exact username)."""
    cands = forge.search_users(name, limit=limit)
    if len(cands) == 1 and name_matches(name, cands[0].name):
        return cands[0].login, cands
    if not cands and use_wikidata:
        logins = _wikidata_logins(forge, name)
        refs = [forge.resolve_user(lg) or UserRef(login=lg) for lg in logins]
        refs = [r for r in refs if r and r.login]
        if len({r.login for r in refs}) == 1:      # one authoritative mapping
            return refs[0].login, refs
        return None, refs
    return None, cands

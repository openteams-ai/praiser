"""AUTHORS / THANKS / CONTRIBUTORS extractor.

Many projects credit founders and major authors by name in these files
(e.g. SciPy's THANKS.txt). A name match alone is moderate confidence (common
names), but it corroborates the commit-volume signal: when both fire, the
record's confidence rises. A handle match is stronger.
"""

import re

from ..models import CORE_CONTRIBUTOR, Evidence
from . import register
from .base import Extractor, ExtractContext
from .contributors import MIN_RANKED_CONTRIBUTIONS

AUTHORS_PATHS = [
    "AUTHORS", "AUTHORS.txt", "AUTHORS.rst", "AUTHORS.md",
    "THANKS", "THANKS.txt", "THANKS.rst", "THANKS.md",
    "CONTRIBUTORS", "CONTRIBUTORS.txt", "CONTRIBUTORS.md",
    "doc/source/dev/THANKS.txt",
]

_HANDLE_RE = re.compile(r"@([A-Za-z0-9-]+)")


def subcomponent_credits(line: str, subcomponents) -> list[tuple[str, str]]:
    """(role, label) for each known subcomponent whose label the credit line
    names — e.g. "… for f2py …" → the f2py subcomponent's configured role.

    This is the authorship signal the subcomponents extractor deliberately does
    NOT infer from commit volume: an explicit human credit that names the part
    (author = provided that part), not "committed to it a lot".
    """
    out: list[tuple[str, str]] = []
    for s in subcomponents:
        label = s.label or ""
        if not label:
            continue
        # Whole-token match so "f2py" doesn't match inside a larger word.
        if re.search(rf"(?<![\w.]){re.escape(label)}(?![\w.])", line, re.I):
            out.append((s.role, label))
    return out


def find_credit(text: str, names: set[str], logins: set[str]) -> tuple[str, bool] | None:
    """Return (matching line, strong) if the user is credited, else None.

    ``strong`` is True for a handle match, False for a name-only match.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        handles = {h.lower() for h in _HANDLE_RE.findall(line)}
        if handles & logins:
            return line, True
    low = text.lower()
    for name in names:
        if len(name) > 5 and name in low:  # full names only, avoid short tokens
            for raw in text.splitlines():
                if name in raw.lower():
                    return raw.strip(), False
    return None


class AuthorsExtractor(Extractor):
    name = "authors"

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        files = ctx.forge.get_files(
            candidate.owner, candidate.repo, AUTHORS_PATHS
        )
        # The user's real contribution to this repo, used to corroborate a bare
        # listing (see below). Cached — the contributors extractor fetches it too.
        contribs = ctx.contributors(candidate) or {}
        commit_count = max((contribs.get(h, 0) for h in ctx.identity.logins),
                           default=0)
        for path in AUTHORS_PATHS:
            text = files.get(path)
            if text is None:
                continue
            hit = find_credit(text, ctx.identity.names, ctx.identity.logins)
            if hit is None:
                continue
            line, strong = hit
            # A name-only credit is easily copied; only trust it when the match
            # itself is trustworthy (own/org repo, or the canonical project).
            if not strong and not ctx.trust_role_file(candidate):
                return []
            url = f"{candidate.url}/blob/HEAD/{path}"
            snippet = (line[:60] + "…") if len(line) > 60 else line
            out: list[Evidence] = []
            # A BARE listing is corroboration, not proof: AUTHORS/CONTRIBUTORS
            # files are frequently exhaustive all-contributors lists (a single
            # merged PR gets you added), so being listed there isn't an elevated
            # role on its own. Emit it only when backed by a real contribution —
            # in which case the contributors extractor also fires and this raises
            # confidence. Otherwise skip it (kills the "one PR → listed → core"
            # false positive).
            if commit_count >= MIN_RANKED_CONTRIBUTIONS:
                out.append(Evidence(
                    source=self.name, role=CORE_CONTRIBUTOR, url=url,
                    confidence=0.7 if strong else 0.5,
                    detail=f"credited in {path}: “{snippet}”",
                ))
            # A credit that NAMES a known subcomponent ("… for f2py …") is a
            # specific authorship attribution — a genuine standalone signal
            # (issues #48/#49), kept regardless of commit volume.
            known = ctx.known(candidate.name_with_owner)
            if known:
                for role, label in subcomponent_credits(line, known.subcomponents):
                    out.append(Evidence(
                        source=self.name, role=role, url=url,
                        confidence=0.8 if strong else 0.65,
                        detail=f"credited for {label} in {path}: “{snippet}”",
                        qualifier=label,
                    ))
            return out
        return []


register(AuthorsExtractor())

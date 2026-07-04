"""Guards for the user-facing role glossary (#127).

The glossary in ``models.ROLE_GLOSSARY`` is the single source of truth, rendered
into the web app and the README. These tests keep it honest:
- it must define every *elevated* role praiser can report (no glossary gap when a
  role is added — the same enforce-don't-document lesson as the extractor registry);
- the README's embedded copy must stay byte-for-byte in sync with the renderer.
"""

import pathlib
import re

from praiser.models import ROLE_GLOSSARY, ROLE_WEIGHTS, WEAK_ROLES
from praiser.render import ROLE_LABELS, render_role_glossary

_README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def test_glossary_covers_exactly_the_elevated_roles():
    glossary_roles = {role for role, _, _ in ROLE_GLOSSARY}
    elevated = set(ROLE_WEIGHTS) - WEAK_ROLES
    missing = elevated - glossary_roles
    extra = glossary_roles - elevated
    assert not missing, f"roles missing a glossary entry: {sorted(missing)}"
    assert not extra, f"glossary entries that aren't elevated roles: {sorted(extra)}"


def test_every_glossary_role_has_a_render_label():
    for role, _, _ in ROLE_GLOSSARY:
        assert role in ROLE_LABELS, f"no display label for role {role!r}"


def test_readme_glossary_is_in_sync_with_the_renderer():
    text = _README.read_text(encoding="utf-8")
    m = re.search(
        r"<!-- ROLE-GLOSSARY:START[^>]*-->\n(.*)\n<!-- ROLE-GLOSSARY:END -->",
        text, re.S)
    assert m, "README is missing the ROLE-GLOSSARY markers"
    assert m.group(1).strip() == render_role_glossary().strip(), (
        "README role glossary is stale — regenerate it from "
        "praiser.render.render_role_glossary() between the markers")

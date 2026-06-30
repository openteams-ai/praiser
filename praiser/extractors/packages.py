"""Package-registry maintainer/author extractor.

Two sources of evidence, both proven elsewhere to be the user's:

* the per-repo package index built during discovery (npm/crates packages the
  user maintains, keyed onto the repos they ship from), and
* a reverse PyPI probe of this candidate's name, kept only when the user is the
  named author/maintainer (see ``registries.pypi_ref_for_repo``).

Being a listed maintainer/owner/author of a published package is a genuine
elevated-role signal, and the registry page is a clickable proof link.
"""

from functools import partial

from ..models import AUTHOR, MAINTAINER, Evidence
from ..registries import JSON_ACCEPT, pypi_ref_for_repo
from . import register
from .base import Extractor, ExtractContext

# crates.io logins are GitHub logins (the only sign-in), so that handle match is
# authoritative; PyPI/npm usernames are merely *likely* the same person, hence a
# touch lower. The package→repo source link (required for a match) underwrites
# all of these, so even the floor is solid.
_BASE_CONFIDENCE = {"crates": 0.85, "pypi": 0.8, "npm": 0.78}
_REGISTRY_LABEL = {"crates": "crates.io", "pypi": "PyPI", "npm": "npm"}


class PackagesExtractor(Extractor):
    name = "packages"

    def applicable(self, candidate, ctx: ExtractContext) -> bool:
        # Index hit (npm/crates) or a PyPI reverse probe is worth a look. The
        # PyPI probe needs a known name to match an author against and a client.
        return bool(ctx.package_index) or bool(ctx.identity.names and ctx.client)

    def extract(self, candidate, ctx: ExtractContext) -> list[Evidence]:
        refs = list(ctx.package_index.get(candidate.name_with_owner.lower(), []))
        if ctx.identity.names and ctx.client is not None:
            fetch = partial(ctx.client.get_url, accept=JSON_ACCEPT)
            pypi = pypi_ref_for_repo(
                fetch, candidate.name_with_owner, ctx.identity
            )
            if pypi is not None:
                refs.append(pypi)
        if not refs:
            return []
        evidence: list[Evidence] = []
        for ref in refs:
            label = _REGISTRY_LABEL.get(ref.registry, ref.registry)
            role = AUTHOR if ref.author_match else MAINTAINER
            verb = "author" if ref.author_match else "maintainer"
            confidence = _BASE_CONFIDENCE.get(ref.registry, 0.75)
            if ref.author_match:
                confidence = min(0.9, confidence + 0.05)
            evidence.append(Evidence(
                source=ref.registry,
                role=role,
                url=ref.url,
                confidence=confidence,
                detail=f"{label} {verb} of package “{ref.name}”",
            ))
        return evidence


register(PackagesExtractor())

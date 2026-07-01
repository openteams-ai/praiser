"""Phase 0 — identity resolution.

Builds the set of {logins, names, emails} believed to belong to one person from
their forge profile. Handle/email matches are high confidence downstream;
name-only matches are weak. We deliberately keep this conservative — over-eager
name aliasing causes common-name false positives.
"""

from .forge import Forge
from .models import Identity

# Note: the profile email usually needs an extra scope and is null anyway, so we
# don't request it here — emails are instead matched from the files we parse
# (CODEOWNERS, manifests, proposal headers).


def resolve_identity(forge: Forge, login: str) -> Identity:
    user = forge.resolve_user(login)
    names = {user.name} if user and user.name else set()
    return Identity(
        primary_login=(user.login if user else None) or login,
        logins={login},
        names=names,
        emails=set(),
    )

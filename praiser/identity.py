"""Phase 0 — identity resolution.

Builds the set of {logins, names, emails} believed to belong to one person from
their GitHub profile. Handle/email matches are high confidence downstream;
name-only matches are weak. We deliberately keep this conservative — over-eager
name aliasing causes common-name false positives.
"""

from .github_client import GitHubClient
from .models import Identity

# Note: the profile ``email`` field needs the user:email / read:user scope and
# is usually null anyway, so we don't request it here — emails are instead
# matched from the files we parse (CODEOWNERS, manifests, proposal headers).
USER_QUERY = """
query($login:String!) {
  user(login:$login) {
    login
    name
  }
}
"""


def resolve_identity(client: GitHubClient, login: str) -> Identity:
    data = client.graphql(USER_QUERY, {"login": login})
    user = (data or {}).get("user") or {}
    names = {user["name"]} if user.get("name") else set()
    return Identity(
        primary_login=user.get("login") or login,
        logins={login},
        names=names,
        emails=set(),
    )

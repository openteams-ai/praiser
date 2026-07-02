"""praiser — record the popular projects a GitHub user has an elevated role in.

Elevated roles are maintainer / code owner / steering-council member /
standards author. Plain contributors are intentionally excluded.
"""

_BASE_VERSION = "0.3.0"


def _git_revision() -> str | None:
    """Short commit hash (``g<hash>`` / ``g<hash>.dirty``) when running from a
    git checkout — so a dev/editable install reports the exact tree it ran from.

    Returns ``None`` for a released install (no ``.git`` alongside the package,
    or git unavailable), leaving ``__version__`` as the plain release string.
    """
    import os
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isdir(os.path.join(root, ".git")):
        return None
    try:
        rev = subprocess.run(
            ["git", "-C", root, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if rev.returncode != 0 or not rev.stdout.strip():
            return None
        tag = "g" + rev.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            tag += ".dirty"
        return tag
    except (OSError, subprocess.SubprocessError):
        return None


def _version() -> str:
    rev = _git_revision()
    # PEP 440 local-version suffix: e.g. "0.2.2+g1a2b3c4" (or ".dirty").
    return f"{_BASE_VERSION}+{rev}" if rev else _BASE_VERSION


__version__ = _version()

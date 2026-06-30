"""Extractor registry.

Importing this package registers every built-in extractor. Add a new
convention by writing a module that calls ``register(MyExtractor())`` at import
time and listing it in ``_BUILTIN_MODULES``.
"""

from .base import Extractor, ExtractContext

_REGISTRY: list[Extractor] = []

_BUILTIN_MODULES = [
    "codeowners",
    "maintainers",
    "manifests",
    "enhancement_proposals",
    "governance",
]


def register(extractor: Extractor) -> Extractor:
    _REGISTRY.append(extractor)
    return extractor


def all_extractors() -> list[Extractor]:
    _ensure_loaded()
    return list(_REGISTRY)


_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from importlib import import_module

    for mod in _BUILTIN_MODULES:
        import_module(f"{__name__}.{mod}")


__all__ = ["Extractor", "ExtractContext", "register", "all_extractors"]

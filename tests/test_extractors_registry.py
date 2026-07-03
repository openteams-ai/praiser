"""Guard: every extractor module is actually wired into the pipeline.

The bug this prevents (#124): ``wikipedia.py`` and ``releases.py`` each called
``register(...)`` at import time and had passing unit tests — but they were
missing from ``_BUILTIN_MODULES``, so ``all_extractors()`` never imported them
and the pipeline never ran them. Direct-import tests hid it. This asserts that
every module that registers an extractor is loaded by ``all_extractors()``.
"""

import pathlib

from praiser.extractors import _BUILTIN_MODULES, all_extractors

_PKG_DIR = pathlib.Path(__file__).resolve().parent.parent / "praiser" / "extractors"


def _modules_that_register() -> set[str]:
    out = set()
    for path in _PKG_DIR.glob("*.py"):
        if path.stem in ("__init__", "base"):
            continue
        if "register(" in path.read_text():
            out.add(path.stem)
    return out


def test_every_registering_module_is_in_builtin_modules():
    missing = _modules_that_register() - set(_BUILTIN_MODULES)
    assert not missing, (
        f"extractor module(s) {sorted(missing)} call register() but are absent "
        "from _BUILTIN_MODULES, so all_extractors() never loads them")


def test_wikipedia_and_release_extractors_run_in_the_pipeline():
    names = {e.name for e in all_extractors()}
    assert "wikipedia_authors" in names
    assert "release_manager" in names

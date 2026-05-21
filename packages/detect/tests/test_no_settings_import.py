"""Guard test (lesson #17 in polyecho-known-gaps):

Importing :mod:`polyecho.detect.app` MUST NOT transitively load
:mod:`polyecho.shared.config`.  The Settings module boots ``Settings`` and
runs the DSN validators that would crash inside a detect container which
has no ``POLYECHO_DB_URL`` / ``POLYECHO_DB_URL_DRIZZLE`` set.

If this test ever goes red, the most likely cause is somebody added an
import like ``from polyecho.shared import something`` that pulled in
``shared/__init__.py`` (which historically tried to import config) or
imported a shared sub-module that itself imports config.  Fix the
import, do NOT loosen this test.
"""

from __future__ import annotations

import importlib
import sys


def test_detect_app_does_not_import_settings() -> None:
    """Fresh import of polyecho.detect.app leaves shared.config unloaded."""
    # Drop any cached module so the import path is exercised cleanly.
    for mod in list(sys.modules):
        if mod == "polyecho.shared.config" or mod.startswith("polyecho.detect"):
            del sys.modules[mod]

    importlib.import_module("polyecho.detect.app")

    assert "polyecho.shared.config" not in sys.modules, (
        "polyecho.detect.app must NOT transitively import polyecho.shared.config — "
        "see lesson #17 in polyecho-known-gaps. The detect container has no DB "
        "vars set and Settings validators will crash it on boot."
    )

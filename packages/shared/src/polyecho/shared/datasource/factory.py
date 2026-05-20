"""
polyecho.shared.datasource.factory
=====================================
``get_datasource()`` — creates the correct ``DataSource`` implementation
based on the ``POLYECHO_DATASOURCE`` environment variable.

  POLYECHO_DATASOURCE=fixture  →  FixtureDataSource
  POLYECHO_DATASOURCE=live     →  LiveDataSource

P1-4 fail-fast contract
-----------------------
When ``settings`` is omitted AND ``POLYECHO_DATASOURCE`` is not set in the
process environment, ``get_datasource()`` RAISES ``RuntimeError`` rather
than silently returning a ``FixtureDataSource``.  This forbids the silent
fixture-fallback that previously masked configuration mistakes
(production launching against bundled JSON fixtures).

Tests / CI must export ``POLYECHO_DATASOURCE=fixture`` explicitly
(see ``packages/shared/tests/conftest.py``).  Callers that already pass an
explicit ``settings`` object (e.g. unit tests, lifecycle harnesses) are
unaffected.

Usage::

    from polyecho.shared.datasource import get_datasource

    ds = get_datasource()   # requires POLYECHO_DATASOURCE in env
    async with ds:          # no-op for fixture; opens httpx client for live
        leaders = await ds.get_leaderboard()
"""

from __future__ import annotations

import os
from typing import Any

from polyecho.shared.datasource.fixture import FixtureDataSource
from polyecho.shared.datasource.live import LiveDataSource
from polyecho.shared.datasource.protocol import DataSource


def get_datasource(settings: Any = None) -> DataSource:
    """Return a ``DataSource`` implementation selected by settings.

    Args:
        settings: Optional ``polyecho.shared.config.Settings`` object.
                  When ``None``, the process-wide singleton is used AND the
                  ``POLYECHO_DATASOURCE`` env var MUST be set — otherwise
                  ``RuntimeError`` is raised (fail-fast, P1-4).

    Returns:
        A ``FixtureDataSource`` when ``POLYECHO_DATASOURCE=fixture``, or a
        ``LiveDataSource`` when ``POLYECHO_DATASOURCE=live``.

    Raises:
        RuntimeError: When ``settings`` is omitted and
            ``POLYECHO_DATASOURCE`` is unset (no silent fixture-fallback).
        ValueError: If ``POLYECHO_DATASOURCE`` is set to an unrecognised
            value (e.g. ``"kafka"``).
    """
    if settings is None:
        # Fail-fast: production code paths must declare datasource explicitly.
        # Tests / CI export POLYECHO_DATASOURCE=fixture in conftest.py.
        if "POLYECHO_DATASOURCE" not in os.environ:
            raise RuntimeError(
                "POLYECHO_DATASOURCE not set; explicit 'live' or 'fixture' "
                "required. Export it in your .env / docker-compose env, or "
                "in tests via monkeypatch.setenv('POLYECHO_DATASOURCE', "
                "'fixture'). No silent fixture-fallback in live deployments."
            )

        from polyecho.shared.config import get_settings  # lazy to avoid circular import

        settings = get_settings()

    mode: str = settings.datasource.lower()

    if mode == "fixture":
        return FixtureDataSource()
    elif mode == "live":
        return LiveDataSource(settings=settings)
    else:
        raise ValueError(
            f"Unknown POLYECHO_DATASOURCE={settings.datasource!r}. "
            "Valid values: 'fixture', 'live'."
        )

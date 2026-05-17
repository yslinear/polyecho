"""
polyecho.shared.datasource.factory
=====================================
``get_datasource()`` — creates the correct ``DataSource`` implementation
based on the ``POLYECHO_DATASOURCE`` environment variable.

  POLYECHO_DATASOURCE=fixture  →  FixtureDataSource  (default)
  POLYECHO_DATASOURCE=live     →  LiveDataSource

Usage::

    from polyecho.shared.datasource import get_datasource

    ds = get_datasource()   # FixtureDataSource by default
    async with ds:          # no-op for fixture; opens httpx client for live
        leaders = await ds.get_leaderboard()
"""

from __future__ import annotations

from typing import Any

from polyecho.shared.datasource.fixture import FixtureDataSource
from polyecho.shared.datasource.live import LiveDataSource
from polyecho.shared.datasource.protocol import DataSource


def get_datasource(settings: Any = None) -> DataSource:
    """Return a ``DataSource`` implementation selected by settings.

    Args:
        settings: Optional ``polyecho.shared.config.Settings`` object.
                  When ``None``, the process-wide singleton is used.

    Returns:
        A ``FixtureDataSource`` when ``POLYECHO_DATASOURCE`` is ``"fixture"``
        (or unset), or a ``LiveDataSource`` when set to ``"live"``.

    Raises:
        ValueError: If ``POLYECHO_DATASOURCE`` is set to an unrecognised value.
    """
    if settings is None:
        from polyecho.shared.config import get_settings  # lazy to avoid circular import

        settings = get_settings()

    mode: str = settings.datasource.lower()

    if mode == "fixture":
        return FixtureDataSource()  # type: ignore[return-value]
    elif mode == "live":
        return LiveDataSource(settings=settings)  # type: ignore[return-value]
    else:
        raise ValueError(
            f"Unknown POLYECHO_DATASOURCE={settings.datasource!r}. "
            "Valid values: 'fixture' (default), 'live'."
        )

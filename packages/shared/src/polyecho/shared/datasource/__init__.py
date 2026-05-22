"""
polyecho.shared.datasource
==========================
Public API for the PolyEcho data-source abstraction layer.

Quick start::

    from polyecho.shared.datasource import get_datasource, DataSource
    from polyecho.shared.datasource import (
        LeaderboardEntry,
        ActivityRecord,
        PositionRecord,
        MarketSummary,
        FixtureDataSource,
        LiveDataSource,
    )

    ds = get_datasource()                       # FixtureDataSource by default
    leaders = await ds.get_leaderboard(limit=12)

Runtime selection
-----------------
Set ``POLYECHO_DATASOURCE`` env var:

* ``fixture`` (default) — deterministic JSON fixtures, no network.
* ``live`` — Polymarket Data API (https://data-api.polymarket.com).

For ``live``, use as an async context manager::

    async with LiveDataSource() as ds:
        leaders = await ds.get_leaderboard()
"""

from polyecho.shared.datasource.dto import (
    ActivityRecord,
    LeaderboardEntry,
    MarketSummary,
    PositionRecord,
)
from polyecho.shared.datasource.factory import get_datasource
from polyecho.shared.datasource.fixture import FixtureDataSource
from polyecho.shared.datasource.live import CircuitOpenError, LiveDataSource
from polyecho.shared.datasource.market_cache import (
    MarketMetadata,
    MarketMetadataCache,
)
from polyecho.shared.datasource.protocol import DataSource

__all__ = [
    # DTOs
    "LeaderboardEntry",
    "ActivityRecord",
    "PositionRecord",
    "MarketSummary",
    "MarketMetadata",
    # Protocol
    "DataSource",
    # Implementations
    "FixtureDataSource",
    "LiveDataSource",
    # Cache (C4, PolyHermes deep-dive)
    "MarketMetadataCache",
    # Errors
    "CircuitOpenError",
    # Factory
    "get_datasource",
]

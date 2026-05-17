"""
polyecho.shared.datasource.protocol
====================================
``DataSource`` structural Protocol — the binding contract for Wave 2's
discovery package.  Implementations must satisfy every method exactly as
typed here.  Use ``typing.runtime_checkable`` for ``isinstance`` guards in
tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from polyecho.shared.datasource.dto import (
    ActivityRecord,
    LeaderboardEntry,
    MarketSummary,
    PositionRecord,
)


@runtime_checkable
class DataSource(Protocol):
    """Async read interface for Polymarket analytics data.

    All implementations must be usable as async context managers via
    ``async with`` *or* via explicit ``__aenter__``/``__aexit__`` lifecycle
    (for ``LiveDataSource``).  ``FixtureDataSource`` has no I/O lifecycle
    but satisfies the protocol by providing the same method signatures.

    BINDING SIGNATURES (Wave 2 depends on these verbatim):
    -------------------------------------------------------
    """

    async def get_leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 200,
    ) -> list[LeaderboardEntry]:
        """Return the top-``limit`` leaderboard entries.

        Implementations that call the live API must paginate internally
        (Polymarket caps at 50 per page, so ``limit=200`` requires 4 pages).

        Args:
            category:    One of OVERALL | POLITICS | SPORTS | CRYPTO |
                         CULTURE | MENTIONS | WEATHER | ECONOMICS | TECH |
                         FINANCE.
            time_period: One of DAY | WEEK | MONTH | ALL.  ``MONTH`` ≈ 30 d.
            order_by:    ``"PNL"`` or ``"VOL"``.
            limit:       Maximum number of entries to return (1–200 for live;
                         fixture slices deterministically).
        """
        ...

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        """Return activity records for *wallet*.

        Args:
            wallet:        Lowercase ``0x…`` proxy-wallet address.
            start:         Include only events at or after this UTC datetime.
            end:           Include only events before or at this UTC datetime.
            activity_type: Filter by type (``"TRADE"``, ``"REDEEM"`` …),
                           or ``None`` to return all types.
            limit:         Maximum records to return; ``None`` means all
                           available within the window.
        """
        ...

    async def get_positions(
        self,
        wallet: str,
        *,
        size_threshold: float = 1.0,
    ) -> list[PositionRecord]:
        """Return open positions for *wallet*.

        Args:
            wallet:         Lowercase ``0x…`` proxy-wallet address.
            size_threshold: Minimum position size; smaller positions are
                            excluded (mirrors the ``sizeThreshold`` API param).
        """
        ...

    async def get_markets_by_volume(
        self,
        *,
        min_volume_24h: float = 100_000.0,
        limit: int = 200,
    ) -> list[MarketSummary]:
        """Return markets ordered by 24-hour volume descending.

        Args:
            min_volume_24h: Exclude markets below this 24 h volume (USD).
            limit:          Maximum number of markets to return.
        """
        ...

    async def get_market_top_holders(
        self,
        condition_id: str,
        *,
        limit: int = 10,
    ) -> list[PositionRecord]:
        """Return the top-``limit`` position holders for a market.

        Args:
            condition_id: The market's condition ID.
            limit:        Maximum number of holders to return.

        Note:
            ``LiveDataSource`` raises ``NotImplementedError`` with a clear
            message because this endpoint is not in the verified Polymarket
            Data API docs (marked ``# TODO(verify)``).
            ``FixtureDataSource`` returns a full fixture-backed result.
        """
        ...

"""Discovery worker: finds and tracks leader candidates on Polymarket.

PRD references: §5.2 (discovery sources), §5.3 (frequency analysis).

Four data sources with different refresh cadences:
- Leaderboard (daily): top 200 by 30-day profit.
- Hot-market top holders (daily): top-10 holders for each market with
  24h volume > $100K.
- Large new positions (hourly): wallets that opened > $5K positions in the
  past 24 hours.
- Known smart-money list (weekly, manual): community-identified addresses.
"""

from __future__ import annotations

import logging
from typing import Any

from polyecho.shared.db import session_scope
from polyecho.shared.polymarket.client import PolymarketClient

from polyecho.discovery.scoring import final_score
from polyecho.discovery.frequency import classify_strategy, detect_drift

logger = logging.getLogger(__name__)


class DiscoveryWorker:
    """Orchestrates all four leader-discovery pipelines.

    PRD §5.2 — each source is implemented as an independent async method so
    they can be scheduled at different cadences by the service loop.
    """

    # ------------------------------------------------------------------
    # Source 1: Polymarket Leaderboard (daily)
    # ------------------------------------------------------------------

    async def fetch_leaderboard(self) -> list[dict[str, Any]]:
        """Fetch the top-200 traders from the Polymarket leaderboard.

        PRD §5.2 — leaderboard source, run daily.
        Retrieves 30-day profit ranking and upserts into ``candidate_leaders``.

        Reference: https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings
        # TODO(verify): confirm pagination parameters and response schema
        # against py-clob-client-v2 leaderboard endpoint.

        Returns:
            List of raw leaderboard entry dicts (wallet_address + stats).

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    # ------------------------------------------------------------------
    # Source 2: Hot-market top holders (daily)
    # ------------------------------------------------------------------

    async def fetch_hot_market_holders(self) -> list[dict[str, Any]]:
        """Fetch top-10 holders for each market with 24h volume > $100K.

        PRD §5.2 — hot-market source, run daily.
        Identifies active traders in high-liquidity markets.

        Reference: https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
        # TODO(verify): confirm markets endpoint filter for 24h volume and
        # positions endpoint response schema with py-clob-client-v2.

        Returns:
            List of position dicts keyed by wallet address and market.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    # ------------------------------------------------------------------
    # Source 3: Large new positions (hourly)
    # ------------------------------------------------------------------

    async def fetch_large_new_positions(self) -> list[dict[str, Any]]:
        """Fetch wallets that opened positions > $5K in the past 24 hours.

        PRD §5.2 — large-position source, run hourly.
        Surfaces sudden smart-money activity before it appears on leaderboards.

        Reference: https://docs.polymarket.com/api-reference/core/get-user-activity
        # TODO(verify): confirm activity endpoint's size/timestamp filter
        # params with py-clob-client-v2.

        Returns:
            List of trade dicts with wallet address, market, and size.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    # ------------------------------------------------------------------
    # Source 4: Known smart-money list (weekly, manual)
    # ------------------------------------------------------------------

    async def sync_smart_money_list(self) -> list[dict[str, Any]]:
        """Upsert the manually curated smart-money address list.

        PRD §5.2 — manual/weekly source.
        Addresses are stored in the DB (settings key or dedicated table) and
        refreshed by the operator.  This method reads the current list and
        ensures all addresses exist in ``candidate_leaders``.

        Returns:
            List of address dicts that were upserted.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run_daily(self) -> None:
        """Run the daily discovery pipeline (sources 1 + 2 + scoring).

        PRD §5.2 — daily cadence:
        1. Fetch leaderboard.
        2. Fetch hot-market holders.
        3. Compute scores for all discovered wallets.
        4. Upsert ``candidate_leaders`` + append ``candidate_score_history``.
        5. Detect strategy drift for all watching/following leaders.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    async def run_hourly(self) -> None:
        """Run the hourly discovery pipeline (source 3).

        PRD §5.2 — hourly cadence:
        1. Fetch large new positions.
        2. Compute scores for newly discovered wallets.
        3. Upsert ``candidate_leaders``.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

    async def run(self) -> None:
        """Start the discovery-worker service loop.

        Schedules daily, hourly, and weekly tasks according to PRD §5.2.

        Raises:
            NotImplementedError: until Phase 1.
        """
        raise NotImplementedError("Phase 1")

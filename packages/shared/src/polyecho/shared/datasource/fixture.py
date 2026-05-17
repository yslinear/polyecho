"""
polyecho.shared.datasource.fixture
====================================
``FixtureDataSource`` — deterministic, no-network data source backed by
JSON fixtures bundled inside the package (``datasource/fixtures/``).

All methods are fully implemented, including ``get_market_top_holders``.
This is the **default** runtime source (``POLYECHO_DATASOURCE=fixture``).

Fixture files loaded
---------------------
``leaderboard.json``
    List of leaderboard entry objects.

``activity/<wallet>.json``
    Per-wallet activity arrays.  The filename must be the lowercase wallet
    address (e.g. ``0xabc123….json``).

``positions/<wallet>.json``
    Per-wallet position arrays.

``markets.json``
    Markets-by-volume array.

``market_holders/<condition_id>.json``
    Per-market top-holder arrays.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from polyecho.shared.datasource.dto import (
    ActivityRecord,
    LeaderboardEntry,
    MarketSummary,
    PositionRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution — fixtures shipped inside the wheel
# ---------------------------------------------------------------------------
_FIXTURES_PKG = "polyecho.shared.datasource.fixtures"


def _load_json(relative_path: str) -> Any:
    """Load a JSON fixture by path relative to the fixtures package.

    Supports both installed wheels (via ``importlib.resources.files``) and
    editable installs / source trees (fallback to ``Path(__file__)``).
    Uses the modern ``importlib.resources.files()`` API (Python 3.9+, no
    deprecation warning on 3.12).
    """
    parts = relative_path.split("/")
    if len(parts) == 1:
        filename = parts[0]
        sub_pkg = _FIXTURES_PKG
    else:
        sub_pkg = _FIXTURES_PKG + "." + ".".join(parts[:-1])
        filename = parts[-1]

    try:
        ref = resources.files(sub_pkg).joinpath(filename)
        return json.loads(ref.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, TypeError, AttributeError):
        # Fallback: resolve relative to this file (editable installs / CI)
        base = Path(__file__).parent / "fixtures"
        full = base / Path(relative_path)
        if full.exists():
            return json.loads(full.read_text(encoding="utf-8"))
        raise FileNotFoundError(
            f"Fixture file not found: {relative_path!r}.  "
            "Ensure fixture JSON files are present in "
            "packages/shared/src/polyecho/shared/datasource/fixtures/."
        )


def _load_json_optional(relative_path: str) -> Any | None:
    try:
        return _load_json(relative_path)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_utc(value: int | float | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _require_utc(value: int | float | str | None) -> datetime:
    dt = _parse_utc(value)
    if dt is None:
        return datetime.now(tz=timezone.utc)
    return dt


def _addr(value: str | None) -> str:
    if not value:
        return ""
    return value.lower()


# ---------------------------------------------------------------------------
# FixtureDataSource
# ---------------------------------------------------------------------------

class FixtureDataSource:
    """Deterministic data source backed by bundled JSON fixtures.

    No network I/O.  Suitable for tests and the default runtime mode.
    Can be used as a plain object (no async context manager required).
    """

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 200,
    ) -> list[LeaderboardEntry]:
        rows: list[dict[str, Any]] = _load_json("leaderboard.json")
        entries: list[LeaderboardEntry] = []
        for i, row in enumerate(rows):
            entries.append(
                LeaderboardEntry(
                    rank=int(row.get("rank", i + 1)),
                    wallet=_addr(row.get("wallet") or row.get("proxyWallet")),
                    username=row.get("username") or row.get("userName") or None,
                    pnl=float(row.get("pnl", 0.0)),
                    volume=float(row.get("volume") or row.get("vol", 0.0)),
                    verified=bool(row.get("verified") or row.get("verifiedBadge", False)),
                )
            )
        # Apply limit slicing deterministically
        return entries[:limit]

    # ------------------------------------------------------------------
    # Activity
    # ------------------------------------------------------------------

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        wallet_lc = wallet.lower()
        data = _load_json_optional(f"activity/{wallet_lc}.json")
        if data is None:
            logger.debug("fixture_activity_not_found", extra={"wallet": wallet_lc})
            return []

        rows: list[dict[str, Any]] = data if isinstance(data, list) else []
        results: list[ActivityRecord] = []

        for row in rows:
            ts = _require_utc(row.get("timestamp"))

            # Time-window filter
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue

            # Type filter
            rtype = str(row.get("type") or row.get("activity_type") or "UNKNOWN")
            if activity_type is not None and rtype != activity_type:
                continue

            record = ActivityRecord(
                wallet=_addr(row.get("wallet") or row.get("proxyWallet") or wallet_lc),
                activity_type=rtype,
                condition_id=row.get("conditionId") or row.get("condition_id") or None,
                side=row.get("side") or None,
                size=float(row.get("size", 0.0)),
                price=float(row["price"]) if row.get("price") is not None else None,
                usdc_size=float(row["usdcSize"]) if row.get("usdcSize") is not None else None,
                timestamp=ts,
                raw=dict(row),
            )
            results.append(record)

        if limit is not None:
            return results[:limit]
        return results

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(
        self,
        wallet: str,
        *,
        size_threshold: float = 1.0,
    ) -> list[PositionRecord]:
        wallet_lc = wallet.lower()
        data = _load_json_optional(f"positions/{wallet_lc}.json")
        if data is None:
            return []

        rows: list[dict[str, Any]] = data if isinstance(data, list) else []
        results: list[PositionRecord] = []

        for row in rows:
            size = float(row.get("size", 0.0))
            if size < size_threshold:
                continue
            record = PositionRecord(
                wallet=_addr(row.get("wallet") or row.get("proxyWallet") or wallet_lc),
                condition_id=str(row.get("conditionId") or row.get("condition_id") or ""),
                asset=str(row.get("asset", "")),
                size=size,
                avg_price=float(row.get("avgPrice") or row.get("avg_price", 0.0)),
                current_value=float(row.get("currentValue") or row.get("current_value", 0.0)),
                cash_pnl=float(row.get("cashPnl") or row.get("cash_pnl", 0.0)),
                percent_pnl=float(row.get("percentPnl") or row.get("percent_pnl", 0.0)),
                title=row.get("title") or None,
                outcome=row.get("outcome") or None,
                raw=dict(row),
            )
            results.append(record)

        return results

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    async def get_markets_by_volume(
        self,
        *,
        min_volume_24h: float = 100_000.0,
        limit: int = 200,
    ) -> list[MarketSummary]:
        rows: list[dict[str, Any]] = _load_json("markets.json")
        results: list[MarketSummary] = []

        for row in rows:
            vol = float(row.get("volume_24h") or row.get("volume_24hr", 0.0))
            if vol < min_volume_24h:
                continue
            end_dt = _parse_utc(row.get("end_date"))
            summary = MarketSummary(
                condition_id=str(row.get("condition_id", "")),
                market_id=str(row.get("market_id", "")),
                question=str(row.get("question", "")),
                volume_24h=vol,
                end_date=end_dt,
                tokens=list(row.get("tokens") or []),
                raw=dict(row),
            )
            results.append(summary)

        # Already sorted by fixture (descending volume); slice to limit
        return results[:limit]

    # ------------------------------------------------------------------
    # Market top holders
    # ------------------------------------------------------------------

    async def get_market_top_holders(
        self,
        condition_id: str,
        *,
        limit: int = 10,
    ) -> list[PositionRecord]:
        data = _load_json_optional(f"market_holders/{condition_id}.json")
        if data is None:
            logger.debug(
                "fixture_market_holders_not_found",
                extra={"condition_id": condition_id},
            )
            return []

        rows: list[dict[str, Any]] = data if isinstance(data, list) else []
        results: list[PositionRecord] = []

        for row in rows:
            record = PositionRecord(
                wallet=_addr(row.get("wallet") or row.get("proxyWallet") or ""),
                condition_id=str(row.get("conditionId") or row.get("condition_id") or condition_id),
                asset=str(row.get("asset", "")),
                size=float(row.get("size", 0.0)),
                avg_price=float(row.get("avgPrice") or row.get("avg_price", 0.0)),
                current_value=float(row.get("currentValue") or row.get("current_value", 0.0)),
                cash_pnl=float(row.get("cashPnl") or row.get("cash_pnl", 0.0)),
                percent_pnl=float(row.get("percentPnl") or row.get("percent_pnl", 0.0)),
                title=row.get("title") or None,
                outcome=row.get("outcome") or None,
                raw=dict(row),
            )
            results.append(record)

        return results[:limit]

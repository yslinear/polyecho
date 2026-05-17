"""
polyecho.shared.datasource.live
================================
``LiveDataSource`` — async httpx client against the Polymarket Data API.

Features
--------
* Internal pagination: ``get_leaderboard(limit=200)`` makes 4 paged calls.
* Exponential-backoff retry (3 attempts, base 1 s, cap 30 s).
* Simple circuit breaker: after 5 consecutive failures the breaker opens and
  subsequent calls raise ``CircuitOpenError`` immediately for 60 s.
* Defensive JSON→DTO mapping: unknown / missing fields are handled gracefully.
* Secrets (API keys, private keys) are never logged.

Usage::

    async with LiveDataSource() as ds:
        leaders = await ds.get_leaderboard(limit=200)

Or supply a custom Settings object (useful in tests)::

    ds = LiveDataSource(settings=my_settings)
    await ds.__aenter__()
    ...
    await ds.__aexit__(None, None, None)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from polyecho.shared.datasource.dto import (
    ActivityRecord,
    LeaderboardEntry,
    MarketSummary,
    PositionRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PAGE_SIZE = 50          # Polymarket max records per page
_RETRY_ATTEMPTS = 3      # total attempts per call
_RETRY_BASE_S = 1.0      # exponential-backoff base sleep (seconds)
_RETRY_CAP_S = 30.0      # max sleep between retries
_CB_THRESHOLD = 5        # failures before circuit opens
_CB_RESET_S = 60.0       # seconds the circuit stays open


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open."""


# ---------------------------------------------------------------------------
# LiveDataSource
# ---------------------------------------------------------------------------
class LiveDataSource:
    """Reads Polymarket Data API with retry + circuit breaker.

    Args:
        settings: A ``polyecho.shared.config.Settings`` instance.
                  Defaults to the process-wide singleton from
                  ``polyecho.shared.config.get_settings()``.
        timeout:  httpx request timeout in seconds (default 30).
    """

    def __init__(
        self,
        settings: Any = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        if settings is None:
            from polyecho.shared.config import get_settings  # lazy import

            settings = get_settings()
        self._base_url: str = settings.polymarket_data_api_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

        # Circuit-breaker state
        self._cb_failures: int = 0
        self._cb_open_until: float = 0.0  # epoch seconds

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> LiveDataSource:
        # Routed through the optional operator proxy (settings DB → env);
        # byte-identical to a bare httpx.AsyncClient when no proxy is set.
        from polyecho.shared.http import make_async_client_resolved

        self._client = await make_async_client_resolved(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "LiveDataSource must be used as an async context manager "
                "(`async with LiveDataSource() as ds: ...`)"
            )
        return self._client

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _check_circuit(self) -> None:
        if self._cb_open_until > time.monotonic():
            raise CircuitOpenError(
                f"Circuit breaker is OPEN. Retry after "
                f"{self._cb_open_until - time.monotonic():.0f}s."
            )

    def _record_success(self) -> None:
        self._cb_failures = 0

    def _record_failure(self) -> None:
        self._cb_failures += 1
        if self._cb_failures >= _CB_THRESHOLD:
            self._cb_open_until = time.monotonic() + _CB_RESET_S
            logger.warning(
                "circuit_breaker_opened",
                extra={"failures": self._cb_failures, "reset_s": _CB_RESET_S},
            )

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        """GET *path* with retry + circuit breaker; returns parsed JSON."""
        self._check_circuit()
        client = self._ensure_client()
        last_exc: Exception | None = None

        for attempt in range(_RETRY_ATTEMPTS):
            try:
                resp = await client.get(path, params=params)
                resp.raise_for_status()
                self._record_success()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_exc = exc
                self._record_failure()
                if attempt < _RETRY_ATTEMPTS - 1:
                    sleep = min(
                        _RETRY_CAP_S,
                        _RETRY_BASE_S * math.pow(2, attempt),
                    )
                    logger.debug(
                        "http_retry",
                        extra={
                            "path": path,
                            "attempt": attempt + 1,
                            "sleep_s": sleep,
                        },
                    )
                    await asyncio.sleep(sleep)

        raise RuntimeError(
            f"LiveDataSource: GET {path} failed after {_RETRY_ATTEMPTS} "
            f"attempts: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # DTO mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_utc(value: int | float | str | None) -> datetime | None:
        """Parse epoch-seconds or ISO-8601 string to UTC-aware datetime."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        try:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            return None

    @staticmethod
    def _addr(value: str | None) -> str:
        if not value:
            return ""
        return value.lower()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 200,
    ) -> list[LeaderboardEntry]:
        """Return up to *limit* leaderboard entries, paginating internally."""
        results: list[LeaderboardEntry] = []
        offset = 0
        remaining = limit

        while remaining > 0:
            page_size = min(_PAGE_SIZE, remaining)
            data = await self._get(
                "/v1/leaderboard",
                params={
                    "category": category,
                    "timePeriod": time_period,
                    "orderBy": order_by,
                    "limit": page_size,
                    "offset": offset,
                },
            )

            rows: list[dict[str, Any]] = data if isinstance(data, list) else []
            if not rows:
                break

            for i, row in enumerate(rows):
                entry = LeaderboardEntry(
                    rank=int(row.get("rank") or offset + i + 1),
                    wallet=self._addr(row.get("proxyWallet")),
                    username=row.get("userName") or None,
                    pnl=float(row.get("pnl") or 0.0),
                    volume=float(row.get("vol") or 0.0),
                    verified=bool(row.get("verifiedBadge", False)),
                )
                results.append(entry)

            offset += len(rows)
            remaining -= len(rows)

            if len(rows) < page_size:
                break  # server returned fewer than requested — last page

        return results[:limit]

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        """Return activity for *wallet* with optional time-window filtering."""
        PAGE = 500  # API max
        results: list[ActivityRecord] = []
        offset = 0

        params: dict[str, Any] = {
            "user": wallet.lower(),
            "limit": PAGE,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if activity_type is not None:
            params["type"] = activity_type
        if start is not None:
            params["start"] = int(start.timestamp())
        if end is not None:
            params["end"] = int(end.timestamp())

        max_offset = 10_000  # API hard cap

        while True:
            params["offset"] = offset
            data = await self._get("/activity", params=params)
            rows: list[dict[str, Any]] = data if isinstance(data, list) else []
            if not rows:
                break

            for row in rows:
                ts = self._parse_utc(row.get("timestamp"))
                if ts is None:
                    ts = datetime.now(tz=UTC)
                record = ActivityRecord(
                    wallet=self._addr(row.get("proxyWallet") or wallet),
                    activity_type=str(row.get("type") or "UNKNOWN"),
                    condition_id=row.get("conditionId") or None,
                    side=row.get("side") or None,
                    size=float(row.get("size") or 0.0),
                    price=float(row["price"]) if row.get("price") is not None else None,
                    usdc_size=float(row["usdcSize"]) if row.get("usdcSize") is not None else None,
                    timestamp=ts,
                    raw=dict(row),
                )
                results.append(record)

            offset += len(rows)
            if limit is not None and len(results) >= limit:
                break
            if len(rows) < PAGE:
                break
            if offset >= max_offset:
                break

        if limit is not None:
            return results[:limit]
        return results

    async def get_positions(
        self,
        wallet: str,
        *,
        size_threshold: float = 1.0,
    ) -> list[PositionRecord]:
        """Return open positions for *wallet*."""
        PAGE = 500
        results: list[PositionRecord] = []
        offset = 0

        while True:
            data = await self._get(
                "/positions",
                params={
                    "user": wallet.lower(),
                    "sizeThreshold": size_threshold,
                    "limit": PAGE,
                    "offset": offset,
                },
            )
            rows: list[dict[str, Any]] = data if isinstance(data, list) else []
            if not rows:
                break

            for row in rows:
                record = PositionRecord(
                    wallet=self._addr(row.get("proxyWallet") or wallet),
                    condition_id=str(row.get("conditionId") or ""),
                    asset=str(row.get("asset") or ""),
                    size=float(row.get("size") or 0.0),
                    avg_price=float(row.get("avgPrice") or 0.0),
                    current_value=float(row.get("currentValue") or 0.0),
                    cash_pnl=float(row.get("cashPnl") or 0.0),
                    percent_pnl=float(row.get("percentPnl") or 0.0),
                    title=row.get("title") or None,
                    outcome=row.get("outcome") or None,
                    raw=dict(row),
                )
                results.append(record)

            offset += len(rows)
            if len(rows) < PAGE:
                break

        return results

    async def get_markets_by_volume(
        self,
        *,
        min_volume_24h: float = 100_000.0,
        limit: int = 200,
    ) -> list[MarketSummary]:
        """Return markets sorted by 24 h volume (highest first)."""
        results: list[MarketSummary] = []
        next_cursor: str | None = None

        while len(results) < limit:
            params: dict[str, Any] = {
                "order_by": "volume_24hr",
                "order_direction": "desc",
                "limit": 100,
            }
            if next_cursor is not None:
                params["next_cursor"] = next_cursor

            data = await self._get("/markets", params=params)
            rows: list[dict[str, Any]] = data.get("data", []) if isinstance(data, dict) else []

            for row in rows:
                vol = float(row.get("volume_24hr") or row.get("volume24hr") or 0.0)
                if vol < min_volume_24h:
                    # Markets are sorted descending; once below threshold we're done.
                    return results[:limit]

                end_dt = self._parse_utc(row.get("end_date"))
                tokens = row.get("tokens") or []
                summary = MarketSummary(
                    condition_id=str(row.get("condition_id") or ""),
                    market_id=str(row.get("market_id") or ""),
                    question=str(row.get("question") or ""),
                    volume_24h=vol,
                    end_date=end_dt,
                    tokens=list(tokens),
                    raw=dict(row),
                )
                results.append(summary)

            # Pagination
            nc = data.get("next_cursor") if isinstance(data, dict) else None
            if nc == "LTE=" or not nc or not rows:
                break
            next_cursor = nc

        return results[:limit]

    async def get_market_top_holders(
        self,
        condition_id: str,
        *,
        limit: int = 10,
    ) -> list[PositionRecord]:
        """Return up to *limit* top holders for *condition_id*.

        Implemented against the confirmed Polymarket data-api endpoint
        (verification 2026-05-17, context7
        ``/websites/polymarket_api-reference`` →
        https://docs.polymarket.com/api-reference/core/get-top-holders-for-markets):

        ``GET /holders``
            * ``market``     — required, comma-separated **condition IDs**.
            * ``limit``      — optional, **server-capped at 20** (per token);
                               we forward ``min(limit, 20)`` and slice locally
                               so callers asking for fewer get exactly that.
            * ``minBalance`` — optional (default 1); left at the server
                               default.

        Response shape (defensively handled)::

            [
              {"token": "0x…",
               "holders": [
                 {"proxyWallet": "0x…", "amount": <number>,
                  "asset": "…", "outcomeIndex": <int>, "name": "…", …},
                 …]},
              …  # one object per token (Yes/No) of the market
            ]

        Each holder is mapped to a :class:`PositionRecord` (``size`` ←
        ``amount``; the holders endpoint carries no avg-price / PnL fields, so
        the numeric PnL/value fields default to ``0.0`` and the full holder
        dict is preserved in ``raw``).  Holders across all returned tokens are
        flattened, the strongest holders first (the API already returns each
        token's holders largest-first), and the combined list is sliced to
        *limit*.  Goes through the shared ``_get`` helper, so retry +
        circuit-breaker behaviour is identical to every other endpoint.

        Args:
            condition_id: The market condition ID (``0x…`` 64-hex).
            limit:        Max holders to return overall (server caps the
                          per-token page at 20).

        Returns:
            A list of :class:`PositionRecord` (length ≤ ``limit``).
        """
        if limit <= 0:
            return []

        # Server caps `limit` at 20 per token; never request more than that.
        page_limit = min(limit, 20)
        data = await self._get(
            "/holders",
            params={
                "market": condition_id,
                "limit": page_limit,
            },
        )

        # Response is a list of {token, holders:[...]} objects (one per
        # outcome token).  Be defensive: also accept a bare holders list or a
        # single object, and skip anything malformed without raising.
        token_groups: list[dict[str, Any]]
        if isinstance(data, list):
            # A token-group response has dict items carrying a "holders" key.
            # If *any* item looks like that, treat the whole thing as
            # token-groups (skipping malformed entries).  Only when *no*
            # item is a holders-group dict do we treat the list as an
            # already-flat holders array.
            if any(
                isinstance(x, dict) and "holders" in x for x in data
            ):
                token_groups = [
                    x
                    for x in data
                    if isinstance(x, dict) and "holders" in x
                ]
            else:
                token_groups = [{"token": "", "holders": data}]
        elif isinstance(data, dict):
            token_groups = [data] if "holders" in data else []
        else:
            token_groups = []

        results: list[PositionRecord] = []
        for group in token_groups:
            token = str(group.get("token") or "")
            holders = group.get("holders")
            if not isinstance(holders, list):
                continue
            for row in holders:
                if not isinstance(row, dict):
                    continue
                # The holders endpoint exposes `amount` (token quantity) and
                # `outcomeIndex`; it carries no avg-price / mark / PnL data,
                # so those PositionRecord fields default to 0.0.  The raw
                # holder dict (bio, pseudonym, profileImage, …) is preserved.
                outcome_idx = row.get("outcomeIndex")
                record = PositionRecord(
                    wallet=self._addr(row.get("proxyWallet")),
                    condition_id=condition_id,
                    asset=str(row.get("asset") or token or ""),
                    size=float(row.get("amount") or 0.0),
                    avg_price=0.0,
                    current_value=0.0,
                    cash_pnl=0.0,
                    percent_pnl=0.0,
                    title=row.get("name") or row.get("pseudonym") or None,
                    outcome=(
                        str(outcome_idx) if outcome_idx is not None else None
                    ),
                    raw=dict(row),
                )
                results.append(record)

        return results[:limit]

"""Discovery-owned hot-market source (live path only).

WHY THIS EXISTS
---------------
The frozen ``LiveDataSource.get_markets_by_volume`` targets the Polymarket
**data-api** ``GET /markets`` endpoint, which is a hard ``404`` (the
data-api exposes ``/holders``/``/activity``/``/positions`` but *not* a
volume-ranked ``/markets`` listing).  The volume-ranked market list lives
on the **gamma-api** (``https://gamma-api.polymarket.com``) under
``GET /markets`` with a *different* parameter / response shape (a BARE
JSON list, not the data-api ``{"data": [...]}`` envelope).

``shared/datasource/live.py`` is FROZEN, so the fix is caller-side: the
discovery worker branches on the datasource type and, on the live path
only, sources hot markets through this module instead of the broken
frozen method.  The fixture path is untouched (byte-identical).

This module is intentionally discovery-owned (NOT shared) and goes
through the FROZEN proxy seam ``make_async_client_resolved`` so the
operator /settings proxy still applies, exactly like the frozen live
datasource does.

Fail-soft contract: this NEVER raises.  Any transport / HTTP / parse
error degrades to ``[]`` plus a single ``logger.warning`` (mirroring the
discovery worker's existing best-effort error style), so a gamma-api
outage degrades the hot-market source to empty rather than aborting the
daily sweep.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from polyecho.shared.http import make_async_client_resolved

logger = logging.getLogger(__name__)

# gamma-api is a SEPARATE host from the data-api; the volume-ranked market
# listing only exists here.  Hard-coded (discovery-owned) on purpose — it
# is not the operator-configurable data-api URL.
_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# gamma-api ``/markets`` is server-paged; 100 is the conventional page
# size used elsewhere against Polymarket and keeps offset round-trips
# bounded.
_GAMMA_PAGE = 100

# Defensive upper bound on pagination so a misbehaving / unbounded API
# response can never spin this loop forever even before the ``limit`` /
# short-page stop conditions fire.
_MAX_PAGES = 50


async def fetch_hot_markets_gamma(
    min_vol: float,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Return up to *limit* hot markets from the gamma-api, fail-soft.

    Queries ``GET {gamma}/markets`` ordered by ``volume24hr`` descending
    (``active=true, closed=false``), paginating by ``offset`` until a
    short page, the ``limit``, or the descending early-stop is hit.

    Because the list is sorted by ``volume24hr`` **descending**, the first
    row whose ``volume24hr`` is below ``min_vol`` means every subsequent
    row is too — we early-return immediately rather than scanning the
    long low-volume tail.

    Args:
        min_vol: Minimum 24 h volume (USD) a market must clear to be
            included.  The descending sort makes this a hard early stop.
        limit:   Maximum number of markets to return.

    Returns:
        A list of ``(condition_id, raw_row)`` tuples, newest-volume
        first, length ≤ ``limit``.  **Always** a list — on any error
        this returns ``[]`` (and logs one warning); it never raises.
    """
    if limit <= 0:
        return []

    results: list[tuple[str, dict[str, Any]]] = []
    try:
        client = await make_async_client_resolved(
            base_url=_GAMMA_BASE_URL,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )
        async with client:
            offset = 0
            for _page in range(_MAX_PAGES):
                params: dict[str, Any] = {
                    "order": "volume24hr",
                    "ascending": "false",
                    "active": "true",
                    "closed": "false",
                    "limit": _GAMMA_PAGE,
                    "offset": offset,
                }
                resp = await client.get("/markets", params=params)
                resp.raise_for_status()
                payload = resp.json()

                # gamma-api returns a BARE JSON list (NOT the data-api
                # ``{"data": [...]}`` envelope).  Anything else → treat
                # as an empty/exhausted page (fail-soft, no raise).
                rows: list[dict[str, Any]] = (
                    payload if isinstance(payload, list) else []
                )
                if not rows:
                    break

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        vol = float(row.get("volume24hr") or 0.0)
                    except (TypeError, ValueError):
                        vol = 0.0
                    # Sorted volume-desc → first sub-threshold row means
                    # the entire remaining tail is below threshold.
                    if vol < min_vol:
                        return results[:limit]

                    condition_id = str(row.get("conditionId") or "")
                    if not condition_id:
                        # No usable condition id → skip this row but
                        # keep scanning (vol still ≥ threshold).
                        continue
                    results.append((condition_id, dict(row)))
                    if len(results) >= limit:
                        return results[:limit]

                # Pagination: a short page means the listing is
                # exhausted.
                if len(rows) < _GAMMA_PAGE:
                    break
                offset += len(rows)

        return results[:limit]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # Fail-soft: gamma-api outage / bad payload degrades the
        # hot-market source to empty (mirrors the worker's existing
        # best-effort error style — log + continue, never propagate).
        logger.warning(
            "discovery_hot_markets_gamma_unavailable",
            extra={"error": str(exc), "got": len(results)},
        )
        return []

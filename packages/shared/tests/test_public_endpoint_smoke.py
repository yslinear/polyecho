"""
packages/shared/tests/test_public_endpoint_smoke.py
====================================================
Integration *smoke* tests that hit the REAL public Polymarket / Polygon
endpoints (no auth, no money, no operator EOA) and verify the live response
shape still parses with our typed clients.

Goals
-----
1. Catch upstream JSON-schema drift (Polymarket / drpc / CLOB) before
   it silently breaks ``LiveDataSource`` parsers in production.
2. Verify the outbound network path (optionally via gluetun
   ``polymarket_proxy_url``) is reachable from the developer host.

Design
------
* Every test is gated by ``@pytest.mark.network`` AND
  ``@pytest.mark.skipif(POLYECHO_RUN_NETWORK_TESTS != "1")`` so the
  byte-mirror CI / default test runs SKIP them automatically.  This keeps
  the suite hermetic and avoids leaking developer IPs or flaking on
  upstream hiccups.
* Assertions are SHAPE-ONLY: ``isinstance(x, list)``, ``"result" in d``,
  field types — NEVER hardcoded IDs / counts / prices.  Production data
  changes minute-to-minute; the test must only check the contract.
* No auth: zero-address wallet for /positions and /activity probes; CLOB
  hits ``/markets?limit=1`` and ``get_server_time()`` (L0, no signer).
* No operator secrets: the optional proxy is read from
  ``Settings.polymarket_proxy_url``; if absent, the test still runs but
  goes direct.

Enable with::

    POLYECHO_RUN_NETWORK_TESTS=1 uv run --package polyecho-shared \
        pytest packages/shared/tests/test_public_endpoint_smoke.py -v --no-cov
"""

from __future__ import annotations

import os

import httpx
import pytest

from polyecho.shared.config import get_settings
from polyecho.shared.datasource.dto import (
    ActivityRecord,
    LeaderboardEntry,
    PositionRecord,
)
from polyecho.shared.datasource.live import LiveDataSource

# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
_NETWORK_ENABLED = os.environ.get("POLYECHO_RUN_NETWORK_TESTS") == "1"
_SKIP_REASON = (
    "network tests skipped — set POLYECHO_RUN_NETWORK_TESTS=1 to enable"
)

# Module-level mark applied to every test in this file so both:
#   * `pytest -m "not network"` (default CI) skips them
#   * `POLYECHO_RUN_NETWORK_TESTS=1` is required at runtime
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not _NETWORK_ENABLED, reason=_SKIP_REASON),
]

# A zero EOA address: the data-api accepts it and returns an empty list,
# which is what we want — we are checking SHAPE, not data.  Using the
# zero address means no operator wallet is ever leaked into a test log.
_ZERO_WALLET = "0x0000000000000000000000000000000000000000"

# drpc was confirmed in the ops runbook as the working Polygon RPC after
# polygon-rpc.com became tenant-disabled.  drpc is reachable WITHOUT the
# gluetun proxy (it is not geo-blocked).
_DRPC_URL = "https://polygon.drpc.org"

_HTTPX_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _proxy() -> str | None:
    """Return the configured Polymarket proxy URL, or None."""
    return get_settings().polymarket_proxy_url


def _data_api_base() -> str:
    """Canonical Polymarket data-api base, from settings."""
    return get_settings().polymarket_data_api_url.rstrip("/")


def _clob_base() -> str:
    return get_settings().polymarket_clob_url.rstrip("/")


# ---------------------------------------------------------------------------
# 1. Data API — positions
# ---------------------------------------------------------------------------
def test_data_api_positions_smoke() -> None:
    """GET /positions?user=0x000…0 returns parseable JSON list."""
    url = f"{_data_api_base()}/positions"
    with httpx.Client(proxy=_proxy(), timeout=_HTTPX_TIMEOUT) as client:
        r = client.get(url, params={"user": _ZERO_WALLET})
    assert r.status_code == 200, (
        f"data-api /positions returned {r.status_code}: {r.text[:200]}"
    )
    data = r.json()
    assert isinstance(data, list), (
        f"expected JSON array, got {type(data).__name__}"
    )

    # If non-empty (very unlikely for the zero address but defensively
    # tolerant), verify the keys our parser actually reads.  Shape only.
    for row in data[:3]:
        assert isinstance(row, dict)
        # PositionRecord parser tolerates missing keys but expects dict shape


# ---------------------------------------------------------------------------
# 2. Data API — activity
# ---------------------------------------------------------------------------
def test_data_api_activity_smoke() -> None:
    """GET /activity?user=0x000…0 returns parseable JSON list."""
    url = f"{_data_api_base()}/activity"
    with httpx.Client(proxy=_proxy(), timeout=_HTTPX_TIMEOUT) as client:
        r = client.get(
            url,
            params={
                "user": _ZERO_WALLET,
                "limit": 10,
                "type": "TRADE",
            },
        )
    assert r.status_code == 200, (
        f"data-api /activity returned {r.status_code}: {r.text[:200]}"
    )
    data = r.json()
    assert isinstance(data, list)


# ---------------------------------------------------------------------------
# 3. Data API — leaderboard
# ---------------------------------------------------------------------------
def test_data_api_leaderboard_smoke() -> None:
    """GET /v1/leaderboard returns rows with the fields our parser reads."""
    url = f"{_data_api_base()}/v1/leaderboard"
    with httpx.Client(proxy=_proxy(), timeout=_HTTPX_TIMEOUT) as client:
        r = client.get(
            url,
            params={
                "category": "OVERALL",
                "timePeriod": "MONTH",
                "orderBy": "PNL",
                "limit": 5,
                "offset": 0,
            },
        )
    assert r.status_code == 200, (
        f"data-api /v1/leaderboard returned {r.status_code}: {r.text[:200]}"
    )
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 1, "leaderboard returned empty — upstream may be down"

    # Spot-check at least one row has the fields LiveDataSource reads,
    # without asserting their values.  Some fields are tolerated as
    # missing (the parser uses .get with defaults), so we only assert
    # the row is dict-shaped.
    head = data[0]
    assert isinstance(head, dict)
    # These are the keys LiveDataSource.get_leaderboard reads.  We don't
    # require them all to be present (parser is defensive), but at least
    # `proxyWallet` should be there for a real row.
    assert "proxyWallet" in head, (
        f"leaderboard row missing proxyWallet — schema drift? keys={list(head)}"
    )


# ---------------------------------------------------------------------------
# 4. Polygon RPC — eth_blockNumber via drpc
# ---------------------------------------------------------------------------
def test_polygon_drpc_eth_block_number_smoke() -> None:
    """POST {jsonrpc:eth_blockNumber} to drpc returns hex block number."""
    # drpc is not geo-blocked → no proxy needed.
    with httpx.Client(timeout=_HTTPX_TIMEOUT) as client:
        r = client.post(
            _DRPC_URL,
            json={
                "jsonrpc": "2.0",
                "method": "eth_blockNumber",
                "params": [],
                "id": 1,
            },
        )
    assert r.status_code == 200, (
        f"drpc returned {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert isinstance(body, dict)
    assert "result" in body, f"drpc response missing 'result': {body}"
    result = body["result"]
    assert isinstance(result, str) and result.startswith("0x"), (
        f"expected hex-encoded block number, got {result!r}"
    )
    # Sanity: parses as int > 0 (Polygon mainnet is many millions of blocks).
    block_no = int(result, 16)
    assert block_no > 0, f"expected positive block height, got {block_no}"


# ---------------------------------------------------------------------------
# 5. CLOB — markets list
# ---------------------------------------------------------------------------
def test_clob_markets_smoke() -> None:
    """GET /markets?limit=1 returns the cursor+data envelope CLOB v2 ships."""
    url = f"{_clob_base()}/markets"
    with httpx.Client(proxy=_proxy(), timeout=_HTTPX_TIMEOUT) as client:
        r = client.get(url)
    assert r.status_code == 200, (
        f"CLOB /markets returned {r.status_code}: {r.text[:200]}"
    )
    data = r.json()
    # CLOB v2 wraps the page in {"data": [...], "next_cursor": "...", ...}.
    # Be defensive: also accept a bare list (legacy / preview).
    if isinstance(data, dict):
        assert "data" in data, (
            f"CLOB /markets dict missing 'data' key: {list(data)}"
        )
        rows = data["data"]
    else:
        rows = data
    assert isinstance(rows, list)
    if rows:
        head = rows[0]
        assert isinstance(head, dict)
        # `condition_id` is the join key the rest of the app relies on.
        assert "condition_id" in head, (
            f"CLOB market row missing condition_id — schema drift? "
            f"keys={list(head)[:10]}"
        )


# ---------------------------------------------------------------------------
# 6. py-clob-client-v2 — ClobClient.get_server_time (L0, no auth)
# ---------------------------------------------------------------------------
def test_clob_client_get_server_time_smoke() -> None:
    """ClobClient(host, chain_id).get_server_time() succeeds at L0."""
    from py_clob_client_v2.client import ClobClient

    client = ClobClient(host=_clob_base(), chain_id=137)
    # L0: no signer / creds — this method must work without auth.
    t = client.get_server_time()
    # The endpoint historically returns either a bare integer (epoch sec),
    # a numeric string, or a dict like {"time": <int>}.  Accept all.
    assert t is not None
    if isinstance(t, dict):
        assert ("time" in t) or ("timestamp" in t), (
            f"CLOB time response missing time/timestamp key: {t}"
        )
    elif isinstance(t, (int, float)):
        assert t > 0
    elif isinstance(t, str):
        # numeric-string form
        assert t.strip().isdigit() or float(t) > 0
    else:  # pragma: no cover
        raise AssertionError(
            f"unexpected get_server_time return type: {type(t).__name__}={t!r}"
        )


# ---------------------------------------------------------------------------
# 7. LiveDataSource end-to-end parse — proves the typed client aligns
#    with the production response, not just that the HTTP succeeds.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_datasource_parses_real_leaderboard() -> None:
    """LiveDataSource.get_leaderboard returns parsed LeaderboardEntry objects."""
    async with LiveDataSource() as ds:
        entries = await ds.get_leaderboard(limit=5)
    assert isinstance(entries, list)
    # The OVERALL/MONTH/PNL leaderboard always has rows in production; an
    # empty list here would indicate upstream drift or rate-limiting.
    assert len(entries) >= 1, "live leaderboard parsed to zero entries"
    for entry in entries:
        assert isinstance(entry, LeaderboardEntry)
        assert isinstance(entry.wallet, str)
        # Wallets are normalised to lowercase 0x… by the parser.
        assert entry.wallet == "" or entry.wallet.startswith("0x")
        assert isinstance(entry.pnl, float)
        assert isinstance(entry.volume, float)


@pytest.mark.asyncio
async def test_live_datasource_parses_real_positions() -> None:
    """LiveDataSource.get_positions for zero-address parses to a list."""
    async with LiveDataSource() as ds:
        positions = await ds.get_positions(_ZERO_WALLET)
    assert isinstance(positions, list)
    # Zero address has no positions; the test here is that parsing did
    # not raise on whatever the API returned.
    for pos in positions:
        assert isinstance(pos, PositionRecord)


@pytest.mark.asyncio
async def test_live_datasource_parses_real_activity() -> None:
    """LiveDataSource.get_user_activity for zero-address parses to a list."""
    async with LiveDataSource() as ds:
        acts = await ds.get_user_activity(_ZERO_WALLET, limit=5)
    assert isinstance(acts, list)
    for act in acts:
        assert isinstance(act, ActivityRecord)


# NOTE: there is intentionally NO offline "sentinel" test in this module.
# Pytest's module-level ``pytestmark`` cannot be selectively cleared on a
# single function (per-function ``pytestmark`` is UNION-ed with the module
# list, not used to replace it), and we want the offline default to look
# like "0 ran, N skipped" so it is unmistakable that no real endpoint was
# hit.  Module-import alone is exercised by collection: a syntax / stale-
# import bug here would fail collection, not be silently skipped.

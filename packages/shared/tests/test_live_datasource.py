"""
tests/test_live_datasource.py
=============================
Behavioural coverage for :class:`polyecho.shared.datasource.live.LiveDataSource`
using ``httpx.MockTransport`` only — **no network, fully deterministic**.

Covers the resilience / correctness logic that ``test_datasource.py`` only
smoke-tests:

* Internal pagination assembling **more than one page** and respecting
  ``limit`` (leaderboard 50/page; activity 500/page; positions 500/page).
* ``get_markets_by_volume`` cursor pagination + descending-volume early stop.
* Exponential-backoff **retry** that eventually succeeds.
* Retry exhaustion → ``RuntimeError`` chaining the last httpx error.
* HTTP status errors (404/429/500) mapped to retry→RuntimeError.
* Transport errors (``ConnectError``) mapped the same way.
* Circuit breaker: opens after 5 consecutive failures, then short-circuits
  with ``CircuitOpenError`` and resets after the cool-down window.
* Wallet lowercasing on every DTO and ``raw`` dict preservation.
* ``_ensure_client`` guard when used without ``async with``.

``asyncio.sleep`` is monkeypatched to a no-op so backoff adds no wall time
and ``time.monotonic`` is controlled where the circuit timer matters.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest

from polyecho.shared.datasource.live import (
    _CB_RESET_S,
    _CB_THRESHOLD,
    _RETRY_ATTEMPTS,
    CircuitOpenError,
    LiveDataSource,
)

from .conftest import run


class _LiveSettings:
    datasource = "live"
    polymarket_data_api_url = "https://data-api.polymarket.com"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make exponential backoff instant & deterministic."""

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("polyecho.shared.datasource.live.asyncio.sleep", _fake_sleep)


def _ds_with_handler(
    handler: Any, *, timeout: float = 5.0
) -> LiveDataSource:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="https://data-api.polymarket.com",
        transport=transport,
        timeout=timeout,
    )
    ds = LiveDataSource(settings=_LiveSettings())
    ds._client = client
    return ds


def _lb_row(rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "proxyWallet": f"0xWALLET{rank:034d}",
        "userName": f"user{rank}",
        "vol": float(rank * 1000),
        "pnl": float(rank * 10),
        "verifiedBadge": rank % 2 == 0,
    }


# ---------------------------------------------------------------------------
# Pagination — assembling > one page
# ---------------------------------------------------------------------------


class TestLeaderboardPagination:
    def test_assembles_multiple_pages_up_to_limit(self) -> None:
        """limit=120 with a 50-row page size → 3 requests (50+50+20)."""
        seen_params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen_params.append(params)
            offset = int(params["offset"])
            page_size = int(params["limit"])
            rows = [_lb_row(offset + i + 1) for i in range(page_size)]
            return httpx.Response(200, json=rows)

        ds = _ds_with_handler(handler)
        entries = run(ds.get_leaderboard(limit=120))

        assert len(entries) == 120
        assert [e.rank for e in entries] == list(range(1, 121))
        # 50 + 50 + 20 → three pages, last page requested only 20.
        assert [int(p["limit"]) for p in seen_params] == [50, 50, 20]
        assert [int(p["offset"]) for p in seen_params] == [0, 50, 100]

    def test_stops_when_server_returns_short_page(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(dict(request.url.params)["offset"])
            if offset == 0:
                return httpx.Response(200, json=[_lb_row(i + 1) for i in range(30)])
            return httpx.Response(200, json=[])

        ds = _ds_with_handler(handler)
        entries = run(ds.get_leaderboard(limit=200))
        assert len(entries) == 30  # only one short page available

    def test_query_params_forwarded(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, json=[_lb_row(1)])

        ds = _ds_with_handler(handler)
        run(
            ds.get_leaderboard(
                category="POLITICS", time_period="WEEK", order_by="VOL", limit=1
            )
        )
        assert captured["category"] == "POLITICS"
        assert captured["timePeriod"] == "WEEK"
        assert captured["orderBy"] == "VOL"


class TestActivityPagination:
    WALLET = "0xABCDEF0000000000000000000000000000000001"

    def test_paginates_and_respects_limit(self) -> None:
        """500/page; request limit=700 → 2 pages, sliced to 700."""

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(dict(request.url.params)["offset"])
            if offset >= 1000:
                return httpx.Response(200, json=[])
            rows = [
                {
                    "proxyWallet": self.WALLET,
                    "type": "TRADE",
                    "conditionId": f"0xc{offset + i}",
                    "side": "BUY",
                    "size": 1.0,
                    "price": 0.5,
                    "usdcSize": 0.5,
                    "timestamp": 1_700_000_000 + offset + i,
                }
                for i in range(500)
            ]
            return httpx.Response(200, json=rows)

        ds = _ds_with_handler(handler)
        records = run(
            ds.get_user_activity(self.WALLET, activity_type=None, limit=700)
        )
        assert len(records) == 700
        assert all(r.wallet == self.WALLET.lower() for r in records)

    def test_no_limit_returns_all_pages(self) -> None:
        pages = {0: 500, 500: 123}

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(dict(request.url.params)["offset"])
            n = pages.get(offset, 0)
            rows = [
                {
                    "proxyWallet": self.WALLET,
                    "type": "TRADE",
                    "size": 1.0,
                    "timestamp": 1_700_000_000 + offset,
                }
                for _ in range(n)
            ]
            return httpx.Response(200, json=rows)

        ds = _ds_with_handler(handler)
        records = run(ds.get_user_activity(self.WALLET, activity_type=None))
        assert len(records) == 623  # 500 + 123, then short page stops loop


class TestPositionsPagination:
    WALLET = "0xABCDEF0000000000000000000000000000000002"

    def test_paginates_full_then_short_page(self) -> None:
        pages = {0: 500, 500: 7}

        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(dict(request.url.params)["offset"])
            n = pages.get(offset, 0)
            rows = [
                {
                    "proxyWallet": self.WALLET,
                    "conditionId": "0xc",
                    "asset": "tok",
                    "size": 10.0,
                    "avgPrice": 0.4,
                    "currentValue": 5.0,
                    "cashPnl": 1.0,
                    "percentPnl": 2.0,
                }
                for _ in range(n)
            ]
            return httpx.Response(200, json=rows)

        ds = _ds_with_handler(handler)
        positions = run(ds.get_positions(self.WALLET))
        assert len(positions) == 507
        assert all(p.wallet == self.WALLET.lower() for p in positions)


class TestMarketsCursorPagination:
    def test_cursor_paginates_until_lte_sentinel(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            cursor = params.get("next_cursor")
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "next_cursor": "PAGE2",
                        "data": [
                            {
                                "condition_id": "0xc1",
                                "market_id": "m1",
                                "question": "Q1",
                                "volume_24hr": 900_000.0,
                                "tokens": [],
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "next_cursor": "LTE=",  # end sentinel
                    "data": [
                        {
                            "condition_id": "0xc2",
                            "market_id": "m2",
                            "question": "Q2",
                            "volume_24hr": 800_000.0,
                            "tokens": [],
                        }
                    ],
                },
            )

        ds = _ds_with_handler(handler)
        markets = run(ds.get_markets_by_volume(min_volume_24h=0.0, limit=100))
        assert [m.condition_id for m in markets] == ["0xc1", "0xc2"]

    def test_descending_volume_early_stop(self) -> None:
        """Once a row is below ``min_volume_24h`` the rest are skipped."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "next_cursor": "MORE",
                    "data": [
                        {
                            "condition_id": "0xbig",
                            "market_id": "m",
                            "question": "Q",
                            "volume_24hr": 500_000.0,
                            "tokens": [],
                        },
                        {
                            "condition_id": "0xsmall",
                            "market_id": "m",
                            "question": "Q",
                            "volume_24hr": 1_000.0,  # below threshold
                            "tokens": [],
                        },
                    ],
                },
            )

        ds = _ds_with_handler(handler)
        markets = run(ds.get_markets_by_volume(min_volume_24h=100_000.0))
        assert [m.condition_id for m in markets] == ["0xbig"]


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    def test_retry_then_success(self) -> None:
        """Two 500s then a 200 → 3 attempts, returns mapped DTO."""
        calls = itertools.count()

        def handler(request: httpx.Request) -> httpx.Response:
            n = next(calls)
            if n < 2:
                return httpx.Response(500, json={"error": "server"})
            return httpx.Response(200, json=[_lb_row(1)])

        ds = _ds_with_handler(handler)
        entries = run(ds.get_leaderboard(limit=1))
        assert len(entries) == 1
        assert next(calls) == 3  # exactly 3 handler invocations consumed

    def test_retry_exhausted_raises_runtimeerror(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        ds = _ds_with_handler(handler)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            run(ds.get_leaderboard(limit=1))

    def test_runtimeerror_chains_last_httpx_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        ds = _ds_with_handler(handler)
        with pytest.raises(RuntimeError) as exc:
            run(ds.get_leaderboard(limit=1))
        assert isinstance(exc.value.__cause__, httpx.HTTPStatusError)


class TestHttpErrorMapping:
    @pytest.mark.parametrize("status", [404, 429, 500, 502, 503])
    def test_status_errors_become_runtimeerror(self, status: int) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": "x"})

        ds = _ds_with_handler(handler)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            run(ds.get_positions("0xabc"))

    def test_transport_error_becomes_runtimeerror(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        ds = _ds_with_handler(handler)
        with pytest.raises(RuntimeError, match="failed after 3 attempts"):
            run(ds.get_positions("0xabc"))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_opens_after_threshold_consecutive_failures(self) -> None:
        """Failure accounting is per *attempt*, not per call: each failing
        call records ``_RETRY_ATTEMPTS`` (3) failures. With
        ``_CB_THRESHOLD == 5`` the breaker stays closed after the first
        failing call (3 < 5), opens during the second (6 >= 5), and the
        third call short-circuits with ``CircuitOpenError`` performing no
        HTTP at all."""
        assert _RETRY_ATTEMPTS == 3 and _CB_THRESHOLD == 5  # contract guard
        http_calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            http_calls["n"] += 1
            return httpx.Response(500)

        ds = _ds_with_handler(handler)

        # Call 1: 3 attempts → 3 failures, breaker still closed.
        with pytest.raises(RuntimeError):
            run(ds.get_leaderboard(limit=1))
        assert ds._cb_failures == _RETRY_ATTEMPTS
        assert ds._cb_failures < _CB_THRESHOLD
        assert http_calls["n"] == 3

        # Call 2: 3 more attempts → 6 failures, crosses threshold → opens.
        with pytest.raises(RuntimeError):
            run(ds.get_leaderboard(limit=1))
        assert ds._cb_failures >= _CB_THRESHOLD
        assert http_calls["n"] == 6

        # Call 3: breaker OPEN → no HTTP attempted, raises CircuitOpenError.
        with pytest.raises(CircuitOpenError, match="OPEN"):
            run(ds.get_leaderboard(limit=1))
        assert http_calls["n"] == 6  # unchanged — short-circuited

    def test_breaker_resets_after_cooldown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(
            "polyecho.shared.datasource.live.time.monotonic",
            lambda: clock["t"],
        )

        state = {"fail": True}

        def handler(_request: httpx.Request) -> httpx.Response:
            if state["fail"]:
                return httpx.Response(500)
            return httpx.Response(200, json=[_lb_row(1)])

        ds = _ds_with_handler(handler)
        # Two failing calls (3 attempts each) cross _CB_THRESHOLD and open it.
        with pytest.raises(RuntimeError):
            run(ds.get_leaderboard(limit=1))
        with pytest.raises(RuntimeError):
            run(ds.get_leaderboard(limit=1))

        # Breaker is open now → short-circuits with the static clock.
        with pytest.raises(CircuitOpenError):
            run(ds.get_leaderboard(limit=1))

        # Advance clock past the reset window; server now healthy.
        clock["t"] += _CB_RESET_S + 1.0
        state["fail"] = False
        entries = run(ds.get_leaderboard(limit=1))
        assert len(entries) == 1
        assert ds._cb_failures == 0  # success resets the counter


# ---------------------------------------------------------------------------
# Mapping invariants & lifecycle
# ---------------------------------------------------------------------------


class TestMappingInvariants:
    def test_wallet_lowercased_everywhere(self) -> None:
        upper = "0xABCDEF0000000000000000000000000000000099"

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/v1/leaderboard"):
                return httpx.Response(
                    200, json=[{"rank": 1, "proxyWallet": upper, "pnl": 1, "vol": 1}]
                )
            if path.startswith("/activity"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "proxyWallet": upper,
                            "type": "TRADE",
                            "size": 1,
                            "timestamp": 1_700_000_000,
                        }
                    ],
                )
            if path.startswith("/positions"):
                return httpx.Response(
                    200, json=[{"proxyWallet": upper, "size": 1.0}]
                )
            return httpx.Response(404)

        ds = _ds_with_handler(handler)
        lb = run(ds.get_leaderboard(limit=1))
        act = run(ds.get_user_activity(upper, activity_type=None))
        pos = run(ds.get_positions(upper))
        assert lb[0].wallet == upper.lower()
        assert act[0].wallet == upper.lower()
        assert pos[0].wallet == upper.lower()

    def test_raw_dict_preserved_and_copied(self) -> None:
        payload = [
            {
                "proxyWallet": "0xabc",
                "type": "REDEEM",
                "size": 3.0,
                "timestamp": 1_700_000_000,
                "extra_field": {"nested": True},
            }
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        ds = _ds_with_handler(handler)
        records = run(ds.get_user_activity("0xabc", activity_type=None))
        raw = records[0].raw
        assert raw["extra_field"] == {"nested": True}
        # ``raw=dict(row)`` → a copy, not the literal payload object.
        assert raw is not payload[0]

    def test_market_top_holders_raises_not_implemented(self) -> None:
        ds = _ds_with_handler(lambda _r: httpx.Response(200, json=[]))
        with pytest.raises(NotImplementedError, match="get_market_top_holders"):
            run(ds.get_market_top_holders("0xcond"))

    def test_missing_optional_fields_default_safely(self) -> None:
        """Sparse rows must not raise — numeric coercion handles ``None``."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"proxyWallet": "0xabc"}])

        ds = _ds_with_handler(handler)
        pos = run(ds.get_positions("0xabc"))
        assert pos[0].size == 0.0
        assert pos[0].avg_price == 0.0
        assert pos[0].title is None


class TestLifecycle:
    def test_use_without_context_manager_raises(self) -> None:
        ds = LiveDataSource(settings=_LiveSettings())
        with pytest.raises(RuntimeError, match="async context manager"):
            run(ds.get_leaderboard(limit=1))

    def test_aenter_aexit_manage_client(self) -> None:
        ds = LiveDataSource(settings=_LiveSettings())

        async def _flow() -> tuple[bool, bool]:
            async with ds as opened:
                has_client = opened._client is not None
            return has_client, ds._client is None

        opened_ok, closed_ok = run(_flow())
        assert opened_ok is True
        assert closed_ok is True

    def test_base_url_trailing_slash_stripped(self) -> None:
        class _S:
            datasource = "live"
            polymarket_data_api_url = "https://data-api.polymarket.com/"

        ds = LiveDataSource(settings=_S())
        assert ds._base_url == "https://data-api.polymarket.com"

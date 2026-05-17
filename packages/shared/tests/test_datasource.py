"""
tests/test_datasource.py
========================
Tests for polyecho.shared.datasource.

Coverage:
- FixtureDataSource returns correct DTO types and counts.
- Leaderboard limit slicing.
- Activity time-window filtering.
- Factory returns FixtureDataSource by default.
- LiveDataSource maps sample JSON payloads via httpx MockTransport (no network).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from polyecho.shared.datasource import (
    ActivityRecord,
    DataSource,
    FixtureDataSource,
    LeaderboardEntry,
    LiveDataSource,
    MarketSummary,
    PositionRecord,
    get_datasource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# FixtureDataSource — basic DTO types & counts
# ---------------------------------------------------------------------------

class TestFixtureLeaderboard:
    def test_returns_list_of_leaderboard_entries(self) -> None:
        ds = FixtureDataSource()
        entries = run(ds.get_leaderboard())
        assert isinstance(entries, list)
        assert len(entries) >= 1
        assert all(isinstance(e, LeaderboardEntry) for e in entries)

    def test_entry_fields(self) -> None:
        ds = FixtureDataSource()
        e = run(ds.get_leaderboard())[0]
        assert isinstance(e.rank, int)
        assert e.wallet.startswith("0x")
        assert isinstance(e.pnl, float)
        assert isinstance(e.volume, float)
        assert isinstance(e.verified, bool)

    def test_limit_slicing(self) -> None:
        ds = FixtureDataSource()
        full = run(ds.get_leaderboard(limit=200))
        sliced = run(ds.get_leaderboard(limit=3))
        assert len(sliced) == 3
        assert sliced[0].rank == full[0].rank  # deterministic order preserved

    def test_limit_larger_than_fixture(self) -> None:
        ds = FixtureDataSource()
        entries = run(ds.get_leaderboard(limit=9999))
        # Should return all fixture entries, not raise
        assert len(entries) >= 12  # fixture has ≥12


class TestFixtureActivity:
    SWING = "0xswing1000000000000000000000000000000001"
    UNKNOWN = "0x0000000000000000000000000000000000000000"

    def test_returns_activity_records(self) -> None:
        ds = FixtureDataSource()
        records = run(ds.get_user_activity(self.SWING))
        assert isinstance(records, list)
        assert len(records) > 0
        assert all(isinstance(r, ActivityRecord) for r in records)

    def test_record_fields(self) -> None:
        ds = FixtureDataSource()
        r = run(ds.get_user_activity(self.SWING))[0]
        assert r.wallet.startswith("0x")
        assert r.activity_type in {"TRADE", "REDEEM", "SPLIT", "MERGE", "REWARD", "UNKNOWN"}
        assert isinstance(r.timestamp, datetime)
        assert r.timestamp.tzinfo is not None  # tz-aware

    def test_timestamp_is_utc_aware(self) -> None:
        ds = FixtureDataSource()
        records = run(ds.get_user_activity(self.SWING))
        for r in records[:10]:
            assert r.timestamp.tzinfo == timezone.utc

    def test_time_window_filter_start(self) -> None:
        ds = FixtureDataSource()
        all_records = run(ds.get_user_activity(self.SWING, activity_type=None))
        if not all_records:
            pytest.skip("No fixture records")
        midpoint = all_records[len(all_records) // 2].timestamp
        filtered = run(ds.get_user_activity(self.SWING, start=midpoint, activity_type=None))
        assert all(r.timestamp >= midpoint for r in filtered)
        assert len(filtered) < len(all_records)

    def test_time_window_filter_end(self) -> None:
        ds = FixtureDataSource()
        all_records = run(ds.get_user_activity(self.SWING, activity_type=None))
        if not all_records:
            pytest.skip("No fixture records")
        midpoint = all_records[len(all_records) // 2].timestamp
        filtered = run(ds.get_user_activity(self.SWING, end=midpoint, activity_type=None))
        assert all(r.timestamp <= midpoint for r in filtered)

    def test_time_window_30d_vs_7d_differ(self) -> None:
        """Drifted fixture should have more activity in 30d window than 7d."""
        DRIFTED = "0xdrifted500000000000000000000000000000005"
        ds = FixtureDataSource()
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)
        w7 = run(ds.get_user_activity(DRIFTED, start=now - timedelta(days=7), activity_type=None))
        w30 = run(ds.get_user_activity(DRIFTED, start=now - timedelta(days=30), activity_type=None))
        assert len(w30) > len(w7)

    def test_activity_type_filter(self) -> None:
        INSIDER = "0xinsider600000000000000000000000000000006"
        ds = FixtureDataSource()
        trades = run(ds.get_user_activity(INSIDER, activity_type="TRADE"))
        redeems = run(ds.get_user_activity(INSIDER, activity_type="REDEEM"))
        all_acts = run(ds.get_user_activity(INSIDER, activity_type=None))
        assert all(r.activity_type == "TRADE" for r in trades)
        assert all(r.activity_type == "REDEEM" for r in redeems)
        assert len(all_acts) == len(trades) + len(redeems)

    def test_limit_applied(self) -> None:
        ds = FixtureDataSource()
        records = run(ds.get_user_activity(self.SWING, limit=5, activity_type=None))
        assert len(records) <= 5

    def test_unknown_wallet_returns_empty(self) -> None:
        ds = FixtureDataSource()
        records = run(ds.get_user_activity(self.UNKNOWN))
        assert records == []


class TestFixturePositions:
    SWING = "0xswing1000000000000000000000000000000001"
    POSITION = "0xposition400000000000000000000000000000004"

    def test_returns_position_records(self) -> None:
        ds = FixtureDataSource()
        positions = run(ds.get_positions(self.SWING))
        assert isinstance(positions, list)
        assert len(positions) >= 1
        assert all(isinstance(p, PositionRecord) for p in positions)

    def test_position_holder_has_multiple(self) -> None:
        ds = FixtureDataSource()
        positions = run(ds.get_positions(self.POSITION))
        assert len(positions) >= 4

    def test_size_threshold_filters(self) -> None:
        ds = FixtureDataSource()
        all_pos = run(ds.get_positions(self.POSITION, size_threshold=0.0))
        big_pos = run(ds.get_positions(self.POSITION, size_threshold=9999.0))
        assert len(big_pos) <= len(all_pos)

    def test_wallet_normalised(self) -> None:
        ds = FixtureDataSource()
        positions = run(ds.get_positions(self.SWING))
        for p in positions:
            assert p.wallet == p.wallet.lower()


class TestFixtureMarkets:
    def test_returns_market_summaries(self) -> None:
        ds = FixtureDataSource()
        markets = run(ds.get_markets_by_volume())
        assert isinstance(markets, list)
        assert len(markets) >= 1
        assert all(isinstance(m, MarketSummary) for m in markets)

    def test_sorted_by_volume_desc(self) -> None:
        ds = FixtureDataSource()
        markets = run(ds.get_markets_by_volume(min_volume_24h=0.0))
        volumes = [m.volume_24h for m in markets]
        assert volumes == sorted(volumes, reverse=True)

    def test_min_volume_filter(self) -> None:
        ds = FixtureDataSource()
        markets = run(ds.get_markets_by_volume(min_volume_24h=1_000_000.0))
        assert all(m.volume_24h >= 1_000_000.0 for m in markets)

    def test_limit(self) -> None:
        ds = FixtureDataSource()
        markets = run(ds.get_markets_by_volume(limit=2, min_volume_24h=0.0))
        assert len(markets) <= 2

    def test_end_date_is_utc_aware_or_none(self) -> None:
        ds = FixtureDataSource()
        for m in run(ds.get_markets_by_volume(min_volume_24h=0.0)):
            if m.end_date is not None:
                assert m.end_date.tzinfo is not None


class TestFixtureMarketTopHolders:
    COND = "0xcond_politics_potus_2028"

    def test_returns_position_records(self) -> None:
        ds = FixtureDataSource()
        holders = run(ds.get_market_top_holders(self.COND))
        assert isinstance(holders, list)
        assert len(holders) >= 1
        assert all(isinstance(h, PositionRecord) for h in holders)

    def test_limit(self) -> None:
        ds = FixtureDataSource()
        holders = run(ds.get_market_top_holders(self.COND, limit=3))
        assert len(holders) <= 3

    def test_unknown_condition_returns_empty(self) -> None:
        ds = FixtureDataSource()
        holders = run(ds.get_market_top_holders("0xnonexistent"))
        assert holders == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_default_returns_fixture(self) -> None:
        ds = get_datasource()
        assert isinstance(ds, FixtureDataSource)

    def test_fixture_mode_explicit(self) -> None:
        class _FakeSettings:
            datasource = "fixture"
            polymarket_data_api_url = "https://data-api.polymarket.com"

        ds = get_datasource(settings=_FakeSettings())
        assert isinstance(ds, FixtureDataSource)

    def test_live_mode(self) -> None:
        class _FakeSettings:
            datasource = "live"
            polymarket_data_api_url = "https://data-api.polymarket.com"

        ds = get_datasource(settings=_FakeSettings())
        assert isinstance(ds, LiveDataSource)

    def test_invalid_mode_raises(self) -> None:
        class _FakeSettings:
            datasource = "bogus"
            polymarket_data_api_url = "https://data-api.polymarket.com"

        with pytest.raises(ValueError, match="bogus"):
            get_datasource(settings=_FakeSettings())

    def test_satisfies_protocol(self) -> None:
        ds = get_datasource()
        assert isinstance(ds, DataSource)


# ---------------------------------------------------------------------------
# LiveDataSource — MockTransport (no network)
# ---------------------------------------------------------------------------

def _make_transport(routes: dict[str, Any]) -> httpx.MockTransport:
    """Build an httpx MockTransport that returns canned JSON for each path."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for prefix, payload in routes.items():
            if path.startswith(prefix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


class _LiveSettings:
    datasource = "live"
    polymarket_data_api_url = "https://data-api.polymarket.com"


_LEADERBOARD_PAYLOAD = [
    {
        "rank": "1",
        "proxyWallet": "0xABC000000000000000000000000000000000001",
        "userName": "TestUser",
        "vol": 500000.0,
        "pnl": 12345.67,
        "profileImage": "",
        "xUsername": "testuser",
        "verifiedBadge": True,
    }
]

_ACTIVITY_PAYLOAD = [
    {
        "proxyWallet": "0xABC000000000000000000000000000000000001",
        "type": "TRADE",
        "conditionId": "0xcond_test",
        "side": "BUY",
        "size": 100.0,
        "price": 0.55,
        "usdcSize": 55.0,
        "timestamp": 1715000000,
        "title": "Test market",
        "outcome": "Yes",
    }
]

_POSITIONS_PAYLOAD = [
    {
        "proxyWallet": "0xABC000000000000000000000000000000000001",
        "conditionId": "0xcond_test",
        "asset": "tok-test-yes",
        "size": 200.0,
        "avgPrice": 0.50,
        "currentValue": 110.0,
        "cashPnl": 10.0,
        "percentPnl": 10.0,
        "title": "Test market",
        "outcome": "Yes",
    }
]

_MARKETS_PAYLOAD = {
    "limit": 100,
    "count": 1,
    "next_cursor": "LTE=",
    "data": [
        {
            "condition_id": "0xcond_live_test",
            "market_id": "mkt-live-test",
            "question": "Will X happen?",
            "volume_24hr": 500000.0,
            "end_date": "2026-12-31T23:59:00+00:00",
            "tokens": [
                {"token_id": "tok-x-yes", "outcome": "Yes", "price": 0.60},
                {"token_id": "tok-x-no",  "outcome": "No",  "price": 0.40},
            ],
        }
    ],
}


class TestLiveDataSourceMock:
    """Tests that LiveDataSource correctly maps raw API JSON → DTOs."""

    def _make_ds(self, routes: dict[str, Any]) -> LiveDataSource:
        transport = _make_transport(routes)
        client = httpx.AsyncClient(
            base_url="https://data-api.polymarket.com",
            transport=transport,
            timeout=5.0,
        )
        ds = LiveDataSource(settings=_LiveSettings())
        ds._client = client  # inject mock client directly
        return ds

    def test_leaderboard_maps_to_dto(self) -> None:
        ds = self._make_ds({"/v1/leaderboard": _LEADERBOARD_PAYLOAD})
        entries = run(ds.get_leaderboard(limit=1))
        assert len(entries) == 1
        e = entries[0]
        assert isinstance(e, LeaderboardEntry)
        assert e.wallet == "0xabc000000000000000000000000000000000001"
        assert e.username == "TestUser"
        assert e.pnl == pytest.approx(12345.67)
        assert e.volume == pytest.approx(500000.0)
        assert e.verified is True

    def test_activity_maps_to_dto(self) -> None:
        ds = self._make_ds({"/activity": _ACTIVITY_PAYLOAD})
        records = run(ds.get_user_activity("0xabc000000000000000000000000000000000001", activity_type=None))
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, ActivityRecord)
        assert r.condition_id == "0xcond_test"
        assert r.side == "BUY"
        assert r.size == pytest.approx(100.0)
        assert r.timestamp.tzinfo == timezone.utc

    def test_positions_maps_to_dto(self) -> None:
        ds = self._make_ds({"/positions": _POSITIONS_PAYLOAD})
        positions = run(ds.get_positions("0xabc000000000000000000000000000000000001"))
        assert len(positions) == 1
        p = positions[0]
        assert isinstance(p, PositionRecord)
        assert p.wallet == "0xabc000000000000000000000000000000000001"
        assert p.size == pytest.approx(200.0)
        assert p.cash_pnl == pytest.approx(10.0)

    def test_markets_maps_to_dto(self) -> None:
        ds = self._make_ds({"/markets": _MARKETS_PAYLOAD})
        markets = run(ds.get_markets_by_volume(min_volume_24h=0.0, limit=10))
        assert len(markets) == 1
        m = markets[0]
        assert isinstance(m, MarketSummary)
        assert m.condition_id == "0xcond_live_test"
        assert m.volume_24h == pytest.approx(500000.0)
        assert m.end_date is not None
        assert m.end_date.tzinfo is not None

    def test_market_top_holders_raises_not_implemented(self) -> None:
        ds = self._make_ds({})
        with pytest.raises(NotImplementedError, match="get_market_top_holders"):
            run(ds.get_market_top_holders("0xcond_anything"))

    def test_wallet_normalised_to_lowercase(self) -> None:
        ds = self._make_ds({"/v1/leaderboard": _LEADERBOARD_PAYLOAD})
        entries = run(ds.get_leaderboard(limit=1))
        assert entries[0].wallet == entries[0].wallet.lower()

    def test_raw_dict_preserved(self) -> None:
        ds = self._make_ds({"/activity": _ACTIVITY_PAYLOAD})
        records = run(ds.get_user_activity("0xabc000000000000000000000000000000000001", activity_type=None))
        assert isinstance(records[0].raw, dict)
        assert "type" in records[0].raw

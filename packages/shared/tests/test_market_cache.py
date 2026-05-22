"""
tests/test_market_cache.py
==========================
Unit tests for ``MarketMetadataCache`` (C4, PolyHermes deep-dive).

The cache layers cache -> markets DB table -> remote.  These tests run
fully in-process: the remote tier is a fake datasource exposing the frozen
``get_markets_by_volume`` signature, and the DB tier is exercised against a
fake async session (a dict-backed stand-in monkeypatched over
``market_cache.session_scope``) so no Postgres is required.

Covered (per the C4 verify checklist):
* a cache MISS falls through to remote and writes back to the DB + cache;
* a cache HIT (in-process and DB) skips the remote call;
* a SETTLED/resolved market is served from cache with no remote call,
  ignoring the freshness window entirely;
* fail-soft: a DB error degrades to a remote fetch, never raises.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from polyecho.shared.datasource import market_cache as mc
from polyecho.shared.datasource.dto import MarketSummary
from polyecho.shared.datasource.market_cache import (
    MarketMetadata,
    MarketMetadataCache,
)

from .conftest import run

_CID = "0x" + "ab" * 32
_CID2 = "0x" + "cd" * 32


# ---------------------------------------------------------------------------
# Fake remote datasource — exposes only the frozen get_markets_by_volume.
# ---------------------------------------------------------------------------
class _FakeDataSource:
    """A minimal datasource: returns canned ``MarketSummary`` rows.

    ``calls`` counts ``get_markets_by_volume`` invocations so a test can
    assert the remote tier was (or was not) reached.
    """

    def __init__(self, rows: list[MarketSummary]) -> None:
        self._rows = rows
        self.calls = 0

    async def get_markets_by_volume(
        self, *, min_volume_24h: float = 0.0, limit: int = 200
    ) -> list[MarketSummary]:
        self.calls += 1
        return list(self._rows)


def _summary(condition_id: str, *, resolved: bool = False) -> MarketSummary:
    """Build a MarketSummary with a metadata-rich ``raw`` payload."""
    raw: dict[str, Any] = {
        "condition_id": condition_id,
        "question": "Will it rain?",
        "slug": "will-it-rain",
        "neg_risk": True,
        "tick_size": "0.001",
        "tokens": [{"token_id": "t1", "outcome": "Yes"}],
    }
    if resolved:
        raw["resolved"] = True
        raw["settled_price"] = 1.0
    return MarketSummary(
        condition_id=condition_id,
        market_id="m1",
        question="Will it rain?",
        volume_24h=500_000.0,
        end_date=datetime(2030, 1, 1, tzinfo=UTC),
        tokens=[{"token_id": "t1", "outcome": "Yes"}],
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Fake async DB session — a dict-backed stand-in for the markets table.
# ---------------------------------------------------------------------------
class _FakeMarketRow:
    """A stand-in for the ``Market`` ORM row (only the read attributes)."""

    def __init__(self, meta: MarketMetadata, updated_at: datetime) -> None:
        self.condition_id = meta.condition_id
        self.market_id = meta.market_id
        self.question = meta.question
        self.slug = meta.slug
        self.neg_risk = meta.neg_risk
        self.tick_size = meta.tick_size
        self.end_date = meta.end_date
        self.outcomes = list(meta.outcomes)
        self.is_resolved = meta.is_resolved
        self.settled_price = meta.settled_price
        self.raw = dict(meta.raw)
        self.updated_at = updated_at


class _FakeSession:
    """Dict-backed async session: ``get`` reads, ``execute`` upserts.

    ``store`` maps condition_id -> ``_FakeMarketRow``; ``updated_at`` is
    controllable so a test can simulate a stale unresolved row.
    """

    def __init__(self, store: dict[str, _FakeMarketRow]) -> None:
        self._store = store

    async def get(self, _model: Any, pk: str) -> _FakeMarketRow | None:
        return self._store.get(pk)

    async def execute(self, stmt: Any) -> None:
        # The cache builds a pg_insert(...).on_conflict_do_update(...); pull
        # the inserted values back out and upsert into the dict store.
        params = dict(stmt.compile().params)
        cid = params["condition_id"]
        meta = MarketMetadata(
            condition_id=cid,
            market_id=params.get("market_id"),
            question=params.get("question"),
            slug=params.get("slug"),
            neg_risk=params.get("neg_risk"),
            tick_size=params.get("tick_size"),
            end_date=params.get("end_date"),
            outcomes=list(params.get("outcomes") or []),
            is_resolved=bool(params.get("is_resolved")),
            settled_price=params.get("settled_price"),
            raw=dict(params.get("raw") or {}),
        )
        self._store[cid] = _FakeMarketRow(meta, datetime.now(tz=UTC))


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    store: dict[str, _FakeMarketRow],
    *,
    fail: bool = False,
) -> None:
    """Monkeypatch ``market_cache.session_scope`` with a fake session.

    When ``fail`` is True the context manager raises on entry so the
    cache's fail-soft DB handling can be exercised.
    """

    @contextlib.asynccontextmanager
    async def _scope() -> Any:
        if fail:
            raise RuntimeError("simulated DB outage")
        yield _FakeSession(store)

    monkeypatch.setattr(mc, "session_scope", _scope)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestCacheMissWritesBack:
    def test_miss_falls_through_to_remote_and_writes_db_and_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store: dict[str, _FakeMarketRow] = {}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID)])
        cache = MarketMetadataCache(ds)

        meta = run(cache.get_market(_CID))

        assert meta is not None
        assert meta.condition_id == _CID
        assert meta.question == "Will it rain?"
        assert meta.neg_risk is True
        assert meta.tick_size == "0.001"
        # Remote was reached exactly once on the miss.
        assert ds.calls == 1
        # Write-back: the row landed in the DB store AND the in-process LRU.
        assert _CID in store
        assert cache._mem_get(_CID) is not None


class TestCacheHitSkipsRemote:
    def test_inprocess_hit_skips_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store: dict[str, _FakeMarketRow] = {}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID)])
        cache = MarketMetadataCache(ds)

        first = run(cache.get_market(_CID))
        second = run(cache.get_market(_CID))

        assert first == second
        # Second read is served from the in-process LRU — remote stays at 1.
        assert ds.calls == 1
        assert cache.mem_hits == 1

    def test_db_hit_skips_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pre-seed the DB store with a FRESH unresolved row, empty cache.
        seeded = MarketMetadata(
            condition_id=_CID2,
            question="Pre-seeded",
            neg_risk=False,
            tick_size="0.01",
        )
        store = {_CID2: _FakeMarketRow(seeded, datetime.now(tz=UTC))}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID2)])
        cache = MarketMetadataCache(ds)

        meta = run(cache.get_market(_CID2))

        assert meta is not None
        assert meta.question == "Pre-seeded"
        # Served from the DB tier — remote never reached.
        assert ds.calls == 0
        assert cache.db_hits == 1


class TestSettledMarketImmutable:
    def test_resolved_db_row_served_without_remote_ignoring_staleness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A RESOLVED row whose updated_at is far older than the freshness
        # window — it must STILL be served from cache (immutable tier).
        resolved = MarketMetadata(
            condition_id=_CID,
            question="Settled market",
            is_resolved=True,
            settled_price=1.0,
        )
        stale_ts = datetime.now(tz=UTC) - timedelta(days=365)
        store = {_CID: _FakeMarketRow(resolved, stale_ts)}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID, resolved=True)])
        cache = MarketMetadataCache(ds, fresh_window=timedelta(minutes=1))

        meta = run(cache.get_market(_CID))

        assert meta is not None
        assert meta.is_resolved is True
        assert meta.settled_price == 1.0
        # Immutable tier: a year-stale resolved row is served with NO
        # remote call.
        assert ds.calls == 0
        assert cache.db_hits == 1

    def test_resolved_inprocess_hit_skips_remote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store: dict[str, _FakeMarketRow] = {}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID, resolved=True)])
        # A zero-length window would normally make every in-process row
        # stale instantly; a resolved row must ignore the window entirely.
        cache = MarketMetadataCache(ds, fresh_window=timedelta(0))

        run(cache.get_market(_CID))  # miss -> remote, caches a resolved row
        run(cache.get_market(_CID))  # resolved -> served from cache

        assert ds.calls == 1
        assert cache.mem_hits == 1


class TestStaleUnresolvedRefreshes:
    def test_stale_unresolved_db_row_triggers_remote_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An UNRESOLVED row older than the window is a miss -> remote.
        stale = MarketMetadata(condition_id=_CID, question="Old")
        stale_ts = datetime.now(tz=UTC) - timedelta(hours=2)
        store = {_CID: _FakeMarketRow(stale, stale_ts)}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([_summary(_CID)])
        cache = MarketMetadataCache(ds, fresh_window=timedelta(minutes=15))

        meta = run(cache.get_market(_CID))

        assert meta is not None
        # The stale unresolved row forced a remote refresh + write-back.
        assert ds.calls == 1
        assert meta.question == "Will it rain?"


class TestFailSoft:
    def test_db_error_degrades_to_remote_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store: dict[str, _FakeMarketRow] = {}
        _patch_session(monkeypatch, store, fail=True)
        ds = _FakeDataSource([_summary(_CID)])
        cache = MarketMetadataCache(ds)

        # The DB tier raises on every access; the read must still succeed
        # by degrading to the remote tier.
        meta = run(cache.get_market(_CID))

        assert meta is not None
        assert meta.condition_id == _CID
        assert ds.calls == 1

    def test_unknown_market_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store: dict[str, _FakeMarketRow] = {}
        _patch_session(monkeypatch, store)
        ds = _FakeDataSource([])  # remote knows nothing
        cache = MarketMetadataCache(ds)

        assert run(cache.get_market(_CID)) is None
        assert run(cache.get_market("")) is None

    def test_no_datasource_db_only_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A DB+cache-only deployment (datasource=None): a DB hit is served,
        # a miss simply returns None (no remote tier).
        seeded = MarketMetadata(condition_id=_CID, question="DB only")
        store = {_CID: _FakeMarketRow(seeded, datetime.now(tz=UTC))}
        _patch_session(monkeypatch, store)
        cache = MarketMetadataCache(None)

        assert run(cache.get_market(_CID)) is not None
        assert run(cache.get_market(_CID2)) is None

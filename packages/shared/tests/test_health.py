"""
tests/test_health.py
=====================
Coverage for ``polyecho.shared.health`` — the FROZEN cross-language
status/health contract.

No network, no Postgres:
* HTTP probes are driven through ``httpx.MockTransport`` by monkeypatching
  ``make_async_client_resolved`` (the shared chokepoint) to a transport-backed
  client — identical technique to ``test_live_datasource.py``.
* DB writers/reader are exercised against a mocked async ``session_scope`` so
  the upsert/select path is covered with zero Postgres.

Asserts the contract the 6 dependent agents build against: model coercion of
the EXISTING ``trader_heartbeat`` row (legacy top-level counters) AND the new
nested-``counters`` shape, the ``{"value": ...}`` envelope tolerance, the
fail-soft "zero vs unavailable" probe rule, stale-liveness verdict parity with
the web (300 s == 5 min), and the JSON shapes.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from polyecho.shared import health
from polyecho.shared.health import (
    HEARTBEAT_STALE_SECONDS,
    INTEGRATION_STATUS_KEY,
    TRADER_HEARTBEAT_KEY,
    WORKER_HEARTBEAT_KEYS,
    IntegrationSnapshot,
    ProbeResult,
    StatusSnapshot,
    WorkerHeartbeat,
    get_status,
    probe_config,
    run_all_probes,
    write_heartbeat,
    write_integration_status,
)

from .conftest import run


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Route health's HTTP probes through a MockTransport (no network)."""

    async def _factory(*, session: Any = None, **overrides: Any) -> httpx.AsyncClient:
        overrides.pop("proxy", None)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=overrides.get("timeout", 5.0),
        )

    monkeypatch.setattr(
        "polyecho.shared.http.make_async_client_resolved", _factory
    )


class _FakeResult:
    """Captures executed statements; mimics ``.scalars().all()``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult(self.rows)

    async def commit(self) -> None:  # session_scope commits on clean exit
        return None

    async def rollback(self) -> None:
        return None


def _patch_session(
    monkeypatch: pytest.MonkeyPatch, session: _FakeSession
) -> None:
    @contextlib.asynccontextmanager
    async def _scope() -> Any:
        yield session

    monkeypatch.setattr("polyecho.shared.db.session_scope", _scope)


class _Row:
    def __init__(self, key: str, value: Any) -> None:
        self.key = key
        self.value = value


# ---------------------------------------------------------------------------
# Contract constants — frozen; the 6 agents pin these
# ---------------------------------------------------------------------------
class TestContractConstants:
    def test_keys_are_exactly_frozen(self) -> None:
        assert TRADER_HEARTBEAT_KEY == "trader_heartbeat"  # MUST match core
        assert WORKER_HEARTBEAT_KEYS == {
            "trader": "trader_heartbeat",
            "discovery": "discovery_heartbeat",
            "backtest": "backtest_heartbeat",
            "push": "push_heartbeat",
        }
        assert INTEGRATION_STATUS_KEY == "integration_status"

    def test_stale_window_matches_web_five_minutes(self) -> None:
        # web lib/settings-keys.ts HEARTBEAT_STALE_MINUTES = 5
        assert HEARTBEAT_STALE_SECONDS == 300.0


# ---------------------------------------------------------------------------
# WorkerHeartbeat coercion — the load-bearing back-compat with core
# ---------------------------------------------------------------------------
class TestWorkerHeartbeatCoercion:
    def test_parses_existing_core_trader_heartbeat_shape(self) -> None:
        """The EXACT dict ``Trader._write_heartbeat`` writes today (counters
        at top level, NOT nested) must parse and fold into ``counters``."""
        legacy = {
            "ts": "2026-05-19T12:00:00+00:00",
            "phase": "supervised",
            "datasource": "fixture",
            "live_trading": False,
            "detected": 3,
            "copied": 1,
            "skipped": 2,
            "placed": 1,
            "followed_leaders": ["0xabc", "0xdef"],
        }
        hb = WorkerHeartbeat.from_settings(legacy)
        assert hb is not None
        assert hb.phase == "supervised"
        assert hb.datasource == "fixture"
        assert hb.live_trading is False
        assert hb.counters["detected"] == 3
        assert hb.counters["followed_leaders"] == ["0xabc", "0xdef"]
        # live_trading bool must NOT leak into numeric counters
        assert "live_trading" not in hb.counters

    def test_parses_new_nested_counters_shape(self) -> None:
        hb = WorkerHeartbeat.from_settings(
            {
                "ts": "2026-05-19T12:00:00+00:00",
                "phase": "idle",
                "counters": {"scanned": 10, "scored": 4},
            }
        )
        assert hb is not None
        assert hb.counters == {"scanned": 10, "scored": 4}

    def test_value_envelope_tolerated(self) -> None:
        hb = WorkerHeartbeat.from_settings(
            {"value": {"ts": "2026-05-19T12:00:00+00:00", "phase": "x"}}
        )
        assert hb is not None and hb.phase == "x"

    @pytest.mark.parametrize(
        "bad", [None, {}, {"phase": "no-ts"}, {"ts": "not-a-date"}, "garbage", 42]
    )
    def test_absent_or_garbage_is_none(self, bad: Any) -> None:
        assert WorkerHeartbeat.from_settings(bad) is None

    def test_is_stale_verdict(self) -> None:
        fresh = datetime.now(tz=UTC).isoformat()
        old = (datetime.now(tz=UTC) - timedelta(seconds=600)).isoformat()
        assert WorkerHeartbeat(ts=fresh).is_stale() is False
        assert WorkerHeartbeat(ts=old).is_stale() is True
        # unparseable ts → treated stale (fail-safe, never "healthy")
        assert WorkerHeartbeat(ts=None).is_stale() is True

    def test_naive_ts_assumed_utc(self) -> None:
        naive = datetime.now(tz=UTC).replace(tzinfo=None).isoformat()
        assert WorkerHeartbeat(ts=naive).is_stale() is False


# ---------------------------------------------------------------------------
# IntegrationSnapshot / ProbeResult JSON shape
# ---------------------------------------------------------------------------
class TestIntegrationSnapshotModel:
    def test_probe_result_json_shape_frozen(self) -> None:
        pr = ProbeResult(
            ok=True, detail="HTTP 200", latency_ms=12.5, checked_at="2026-05-19T0:0:0+00:00"
        )
        d = pr.model_dump(mode="json")
        assert set(d) == {"ok", "detail", "latency_ms", "checked_at"}
        assert d["ok"] is True and d["latency_ms"] == 12.5

    def test_from_settings_roundtrip_and_envelope(self) -> None:
        raw = {
            "value": {
                "checked_at": "2026-05-19T12:00:00+00:00",
                "probes": {
                    "database": {"ok": True, "detail": "SELECT 1", "latency_ms": 2.0},
                    "polygon_rpc": {"ok": False, "detail": "timed out"},
                    "junk": "not-a-dict",
                },
            }
        }
        snap = IntegrationSnapshot.from_settings(raw)
        assert snap is not None
        assert snap.probes["database"].ok is True
        assert snap.probes["polygon_rpc"].ok is False
        assert "junk" not in snap.probes  # malformed probe skipped, not raised

    @pytest.mark.parametrize("bad", [None, 1, "x", {"probes": "nope"}])
    def test_garbage_tolerated(self, bad: Any) -> None:
        snap = IntegrationSnapshot.from_settings(bad)
        # None for non-dicts; an empty-probes snapshot for a dict w/o probes
        assert snap is None or snap.probes == {}


# ---------------------------------------------------------------------------
# Probes — fail-soft, never raise (brief §4 "zero vs unavailable")
# ---------------------------------------------------------------------------
class TestProbesFailSoft:
    def test_probe_config_ok_with_minimal_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/db"
        )
        res = probe_config()
        assert res.ok is True
        assert "driver=postgresql+asyncpg" in res.detail
        assert res.latency_ms is not None
        assert res.checked_at is not None

    def test_http_probe_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(
            monkeypatch, lambda r: httpx.Response(200, json={"now": 1})
        )
        res = run(health.probe_polymarket_clob())
        assert res.ok is True
        assert res.detail == "HTTP 200"
        assert res.latency_ms is not None

    def test_http_probe_non_2xx_is_not_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(monkeypatch, lambda r: httpx.Response(503))
        res = run(health.probe_polymarket_data_api())
        assert res.ok is False
        assert "HTTP 503" in res.detail

    def test_http_probe_connect_error_is_fail_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        _mock_http(monkeypatch, boom)
        res = run(health.probe_polymarket_clob())
        assert res.ok is False
        assert "unreachable" in res.detail  # did NOT raise

    def test_http_probe_timeout_is_fail_soft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_r: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        _mock_http(monkeypatch, boom)
        res = run(health.probe_vpn_egress())
        assert res.ok is False and "timed out" in res.detail

    def test_polygon_rpc_json_rpc_error_body_is_not_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(
            monkeypatch,
            lambda r: httpx.Response(200, json={"error": {"code": -32000}}),
        )
        res = run(health.probe_polygon_rpc())
        assert res.ok is False
        assert "RPC error" in res.detail  # 2xx but logical error surfaced

    def test_polygon_rpc_success_reports_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(
            monkeypatch,
            lambda r: httpx.Response(200, json={"result": "0x1a2b"}),
        )
        res = run(health.probe_polygon_rpc())
        assert res.ok is True and "0x1a2b" in res.detail

    def test_vpn_egress_reports_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(
            monkeypatch,
            lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}),
        )
        res = run(health.probe_vpn_egress())
        assert res.ok is True and "1.2.3.4" in res.detail

    def test_run_all_probes_returns_all_names_and_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_http(monkeypatch, lambda r: httpx.Response(200, json={}))
        monkeypatch.setenv(
            "POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/db"
        )
        out = run(run_all_probes())
        assert set(out) == {
            "config",
            "database",
            "polymarket_clob",
            "polymarket_data_api",
            "polygon_rpc",
            "vpn_egress",
        }
        assert all(isinstance(v, ProbeResult) for v in out.values())


# ---------------------------------------------------------------------------
# Writers — best-effort upsert (mirrors wiring.make_heartbeat_writer)
# ---------------------------------------------------------------------------
class TestWriters:
    def test_write_heartbeat_upserts_frozen_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess = _FakeSession()
        _patch_session(monkeypatch, sess)
        ok = run(
            write_heartbeat(
                "discovery",
                phase="scanning",
                datasource="fixture",
                live_trading=False,
                counters={"scanned": 7},
            )
        )
        assert ok is True
        assert len(sess.executed) == 1  # one upsert issued

    def test_write_heartbeat_unknown_kind_raises(self) -> None:
        # Producer-side typo must fail loudly (reader side stays fail-soft).
        with pytest.raises(ValueError, match="unknown heartbeat kind"):
            run(write_heartbeat("nope", phase="x"))

    def test_write_heartbeat_accepts_full_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess = _FakeSession()
        _patch_session(monkeypatch, sess)
        assert run(write_heartbeat("push_heartbeat", phase="idle")) is True

    def test_write_heartbeat_swallows_db_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @contextlib.asynccontextmanager
        async def _broken() -> Any:
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr("polyecho.shared.db.session_scope", _broken)
        # Best-effort: returns False, does NOT raise (liveness must not crash).
        assert run(write_heartbeat("trader", phase="supervised")) is False

    def test_write_integration_status_persists_and_returns_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sess = _FakeSession()
        _patch_session(monkeypatch, sess)
        probes = {"database": ProbeResult(ok=True, detail="SELECT 1")}
        snap = run(write_integration_status(probes))
        assert isinstance(snap, IntegrationSnapshot)
        assert snap.probes["database"].ok is True
        assert snap.checked_at is not None
        assert len(sess.executed) == 1


# ---------------------------------------------------------------------------
# Aggregator get_status — the single read web + cli render
# ---------------------------------------------------------------------------
class TestGetStatus:
    def test_aggregates_heartbeats_and_integration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            _Row(
                "trader_heartbeat",
                {
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "phase": "supervised",
                    "detected": 5,
                },
            ),
            _Row(
                "discovery_heartbeat",
                {"ts": datetime.now(tz=UTC).isoformat(), "phase": "idle"},
            ),
            _Row(
                INTEGRATION_STATUS_KEY,
                {
                    "checked_at": datetime.now(tz=UTC).isoformat(),
                    "probes": {"database": {"ok": True, "detail": "SELECT 1"}},
                },
            ),
        ]
        _patch_session(monkeypatch, _FakeSession(rows))
        snap = run(get_status())
        assert isinstance(snap, StatusSnapshot)
        assert snap.workers["trader"] is not None
        assert snap.workers["trader"].counters["detected"] == 5
        assert snap.workers["discovery"] is not None
        # absent producers → None (web renders "missing", never errors)
        assert snap.workers["backtest"] is None
        assert snap.workers["push"] is None
        assert snap.integration is not None
        assert snap.integration.probes["database"].ok is True

    def test_fail_soft_when_db_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @contextlib.asynccontextmanager
        async def _broken() -> Any:
            raise RuntimeError("no db")
            yield  # pragma: no cover

        monkeypatch.setattr("polyecho.shared.db.session_scope", _broken)
        snap = run(get_status())
        # Empty-but-valid snapshot, NOT an exception (contract parity with
        # the web coerceHeartbeat → null path).
        assert all(v is None for v in snap.workers.values())
        assert snap.integration is None
        assert snap.generated_at  # still stamped

    def test_garbage_rows_coerce_to_none_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            _Row("trader_heartbeat", "totally-not-json-object"),
            _Row(INTEGRATION_STATUS_KEY, 12345),
        ]
        _patch_session(monkeypatch, _FakeSession(rows))
        snap = run(get_status())
        assert snap.workers["trader"] is None
        assert snap.integration is None

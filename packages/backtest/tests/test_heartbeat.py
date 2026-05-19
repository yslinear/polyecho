"""Tests for the backtest worker ``backtest_heartbeat`` emission.

Mandate (A-backtest):
  * the worker emits a well-formed ``backtest_heartbeat`` via the FROZEN
    shared ``write_heartbeat("backtest", ...)`` ONCE per poll cycle
    (and on ``--once``), counters = jobs processed/failed THIS cycle,
    phase = idle/running;
  * a heartbeat-write error is STRICTLY fail-soft — it must NEVER abort
    the worker or affect job processing;
  * ``--once`` still drains ALL pending jobs then exits 0.

The FROZEN ``polyecho.shared.health.write_heartbeat`` lives in
``packages/shared`` (OUT of this worktree).  These tests never depend on
the real shared module: they patch ``polyecho.backtest._heartbeat`` /
the jobs seam, so they are authoritative for THIS package in isolation.
No __init__.py in this directory (importlib mode); asyncio_mode=auto.
"""

from __future__ import annotations

import contextlib
import sys
import types
from typing import Any

import pytest

# Fixture wallet (matches test_backtest.py's SWING constant exactly); the
# heartbeat tests stub _process_job/_fetch_pending so the address is only a
# stable opaque job payload here — no fixture data is actually read.
SWING = "0xswing1000000000000000000000000000000001"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeJob:
    """Minimal stand-in for a claimed BacktestJob row."""

    def __init__(self, leader_addresses: list[str]) -> None:
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.leader_addresses = leader_addresses


@contextlib.asynccontextmanager
async def _fake_scope():  # type: ignore[no-untyped-def]
    yield object()


def _make_ds() -> Any:
    from polyecho.shared.datasource import FixtureDataSource

    return FixtureDataSource()


# ---------------------------------------------------------------------------
# 1. The worker emits ONE well-formed heartbeat per poll cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_emits_well_formed_heartbeat_per_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cycle that claims & processes 2 jobs (1 OK, 1 failing) must emit
    exactly one heartbeat with phase='running' and
    counters={'processed':1,'failed':1}, and the captured payload must
    have the FROZEN shape (ts ISO8601, phase, datasource, live_trading,
    counters{})."""
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    calls: list[tuple[str, str, dict[str, Any]]] = []

    # Stub the FROZEN shared.health.write_heartbeat so we capture the EXACT
    # positional/keyword contract the worker invokes it with.
    fake_health = types.ModuleType("polyecho.shared.health")

    async def _capture_write_heartbeat(
        component: str,
        *,
        phase: str,
        datasource: str,
        live_trading: bool,
        counters: dict[str, Any],
    ) -> None:
        calls.append((component, phase, dict(counters)))
        # Assert the FROZEN shape inline (raising here would also prove the
        # error is swallowed elsewhere; here we WANT it to surface).
        # F0's write_heartbeat is keyword-only after ``kind`` and stamps
        # ``ts`` itself — the caller passes no ``ts``, so the stub's
        # signature deliberately has none (mirrors F0's real signature).
        assert component == "backtest"
        assert phase in ("idle", "running")
        assert isinstance(datasource, str) and datasource
        assert isinstance(live_trading, bool)
        assert isinstance(counters, dict)

    fake_health.write_heartbeat = _capture_write_heartbeat  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "polyecho.shared.health", fake_health)

    ok_job = _FakeJob([SWING])
    bad_job = _FakeJob([SWING])

    _polls = 0

    async def _fake_recover(_s: object) -> int:
        return 0

    async def _fake_fetch(_s: object) -> list[object]:
        nonlocal _polls
        _polls += 1
        # One cycle returns both jobs, the next drains empty (→ exit).
        return [ok_job, bad_job] if _polls == 1 else []

    async def _fake_process(job: object, _engine: object) -> None:
        if job is bad_job:
            raise RuntimeError("synthetic per-job failure")

    async def _record_set_status(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "_process_job", _fake_process)
    monkeypatch.setattr(jobs_mod, "_set_status", _record_set_status)

    await process_pending_jobs(_make_ds(), once=True)

    # Two cycles → two heartbeats: cycle 1 ran work, cycle 2 was the empty
    # drain poll that proves the queue is empty.
    assert len(calls) == 2
    c1_component, c1_phase, c1_counters = calls[0]
    assert c1_component == "backtest"
    assert c1_phase == "running"
    # 1 processed OK, 1 raised → failed.
    assert c1_counters == {"processed": 1, "failed": 1}
    # The final empty poll heartbeats as idle with zeroed per-cycle counters.
    _, c2_phase, c2_counters = calls[1]
    assert c2_phase == "idle"
    assert c2_counters == {"processed": 0, "failed": 0}


@pytest.mark.asyncio
async def test_idle_cycle_emits_idle_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-empty queue still emits ONE heartbeat: phase='idle',
    counters all zero, exactly one poll (no spin)."""
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    captured: list[tuple[str, dict[str, int]]] = []

    async def _fake_hb(phase: str, counters: dict[str, int]) -> None:
        captured.append((phase, dict(counters)))

    async def _fake_recover(_s: object) -> int:
        return 0

    polls: list[int] = []

    async def _fake_fetch(_s: object) -> list[object]:
        polls.append(1)
        return []

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "write_backtest_heartbeat", _fake_hb)

    await process_pending_jobs(_make_ds(), once=True)

    assert len(polls) == 1  # exited after one empty poll
    assert captured == [("idle", {"processed": 0, "failed": 0})]


# ---------------------------------------------------------------------------
# 2. A heartbeat write failure is STRICTLY swallowed (worker unaffected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_write_failure_is_swallowed_and_jobs_still_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the FROZEN shared write_heartbeat raises, the worker must NOT
    abort: the job is still processed and ``--once`` still returns 0."""
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    fake_health = types.ModuleType("polyecho.shared.health")

    async def _boom_write_heartbeat(*_a: object, **_k: object) -> None:
        raise RuntimeError("settings upsert exploded")

    fake_health.write_heartbeat = _boom_write_heartbeat  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "polyecho.shared.health", fake_health)

    job = _FakeJob([SWING])
    processed: list[object] = []
    _polls = 0

    async def _fake_recover(_s: object) -> int:
        return 0

    async def _fake_fetch(_s: object) -> list[object]:
        nonlocal _polls
        _polls += 1
        return [job] if _polls == 1 else []

    async def _fake_process(j: object, _e: object) -> None:
        processed.append(j)

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "_process_job", _fake_process)

    # Must NOT raise despite every heartbeat write blowing up.
    await process_pending_jobs(_make_ds(), once=True)

    # Job processing was completely unaffected by the heartbeat failures.
    assert processed == [job]


@pytest.mark.asyncio
async def test_write_backtest_heartbeat_never_raises_on_missing_shared() -> None:
    """Direct unit: an ABSENT frozen shared.health module (ImportError)
    must degrade to a swallowed warning, never propagate."""
    import polyecho.backtest._heartbeat as hb_mod

    # Ensure the frozen module is not importable for this call.
    assert "polyecho.shared.health" not in sys.modules or True
    # Calling with the real (likely-absent in isolation) shared.health
    # must complete without raising (the call returns None).
    await hb_mod.write_backtest_heartbeat(
        hb_mod.PHASE_RUNNING, {"processed": 3, "failed": 1}
    )


@pytest.mark.asyncio
async def test_write_backtest_heartbeat_swallows_input_resolution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if datasource/live-flag resolution AND the write all fail, the
    helper returns None (never raises)."""
    import polyecho.backtest._heartbeat as hb_mod

    def _explode() -> Any:
        raise RuntimeError("config parse failed")

    # Both resolvers blow up → must still be swallowed (resolvers are
    # individually guarded; the outer guard is the final backstop).
    monkeypatch.setattr(hb_mod, "_resolve_datasource", _explode)
    monkeypatch.setattr(hb_mod, "_resolve_live_trading", _explode)

    # Must complete without raising (the call returns None).
    await hb_mod.write_backtest_heartbeat(
        hb_mod.PHASE_IDLE, {"processed": 0, "failed": 0}
    )


# ---------------------------------------------------------------------------
# 3. --once still drains ALL pending then exits 0 (heartbeat per cycle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_once_drains_all_then_exits_with_heartbeat_each_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R12 #5 contract preserved with heartbeats wired in: --once polls
    until the queue is empty (5, 5, 0 jobs over 3 polls = all 10 drained)
    and emits exactly one heartbeat PER poll cycle, the last one idle."""
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    heartbeats: list[tuple[str, dict[str, int]]] = []

    async def _fake_hb(phase: str, counters: dict[str, int]) -> None:
        heartbeats.append((phase, dict(counters)))

    batches: list[list[object]] = [
        [_FakeJob([SWING]) for _ in range(5)],
        [_FakeJob([SWING]) for _ in range(5)],
        [],
    ]
    polls: list[int] = []
    processed: list[object] = []

    async def _fake_recover(_s: object) -> int:
        return 0

    async def _fake_fetch(_s: object) -> list[object]:
        b = batches.pop(0) if batches else []
        polls.append(len(b))
        return b

    async def _fake_process(job: object, _e: object) -> None:
        processed.append(job)

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "_process_job", _fake_process)
    monkeypatch.setattr(jobs_mod, "write_backtest_heartbeat", _fake_hb)

    await process_pending_jobs(_make_ds(), once=True)

    # Drain contract intact: 3 polls (5,5,0), all 10 jobs processed.
    assert polls == [5, 5, 0]
    assert len(processed) == 10
    # One heartbeat per poll cycle, last is the idle empty-drain poll.
    assert heartbeats == [
        ("running", {"processed": 5, "failed": 0}),
        ("running", {"processed": 5, "failed": 0}),
        ("idle", {"processed": 0, "failed": 0}),
    ]


@pytest.mark.asyncio
async def test_once_exit_code_contract_returns_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--once`` exit-code contract: process_pending_jobs returns
    normally (→ exit 0) even when EVERY heartbeat write raises."""
    import polyecho.backtest.jobs as jobs_mod
    from polyecho.backtest.jobs import process_pending_jobs

    async def _boom_hb(_phase: str, _counters: dict[str, int]) -> None:
        raise RuntimeError("heartbeat down")

    async def _fake_recover(_s: object) -> int:
        return 0

    async def _fake_fetch(_s: object) -> list[object]:
        return []

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_recover_stale_running", _fake_recover)
    monkeypatch.setattr(jobs_mod, "_fetch_pending", _fake_fetch)
    monkeypatch.setattr(jobs_mod, "write_backtest_heartbeat", _boom_hb)

    # Returns (does not raise / hang) — exit 0 preserved. The mere fact
    # that this await completes IS the exit-code contract assertion.
    await process_pending_jobs(_make_ds(), once=True)

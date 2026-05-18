"""
Wave D-metrics — backtest job-duration observability (PRD §7.1).

Asserts ``backtest_seconds`` appears in the ``jobs.done`` structured log
for a completed job, and that a forced exception inside the timing helper
does NOT propagate (best-effort).  NO behaviour / status change.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import date
from typing import Any

import pytest

from polyecho.backtest import _perf
from polyecho.backtest.engine import BacktestEngine
from polyecho.shared.datasource import FixtureDataSource

SWING = "0xswing1000000000000000000000000000000001"
START = date(2026, 2, 16)
END = date(2026, 4, 28)


def _make_engine() -> BacktestEngine:
    return BacktestEngine(datasource=FixtureDataSource())


class _FakeJob:
    def __init__(self, leader_addresses: list[str]) -> None:
        self.id = uuid.uuid4()
        self.leader_addresses = leader_addresses
        self.template_params: dict[str, object] | None = {
            "copy_ratio": 0.05,
            "min_trade_size_usd": 1.0,
        }
        self.start_date = START
        self.end_date = END
        self.initial_capital_usd = 1000.0
        self.slippage_bps = 150
        self.execution_delay_seconds = 30
        self.status = "pending"
        self.progress = 0.0
        self.result: dict[str, object] | None = None


def _patch_status_recorder(
    monkeypatch: pytest.MonkeyPatch, transitions: list[dict[str, object]]
) -> None:
    from polyecho.backtest import jobs as jobs_mod

    @contextlib.asynccontextmanager
    async def _fake_scope():  # type: ignore[no-untyped-def]
        yield object()

    async def _record_set_status(
        session: object,
        job_id: object,
        status: str,
        *,
        progress: float | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        transitions.append({"status": status})

    monkeypatch.setattr(jobs_mod, "session_scope", _fake_scope)
    monkeypatch.setattr(jobs_mod, "_set_status", _record_set_status)


def _done_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "jobs.done"]


@pytest.mark.asyncio
async def test_backtest_seconds_field_present(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A completed job emits backtest_seconds on the jobs.done log line."""
    from polyecho.backtest.jobs import _process_job

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)
    job = _FakeJob([SWING])

    with caplog.at_level(logging.INFO, logger="polyecho.backtest.jobs"):
        await _process_job(job, _make_engine())  # type: ignore[arg-type]

    # Behaviour unchanged: pending→running→completed lifecycle intact.
    assert [t["status"] for t in transitions][-1] == "completed"

    recs = _done_records(caplog)
    assert recs, "expected a jobs.done log line"
    rec = recs[-1]
    secs = rec.backtest_seconds  # type: ignore[attr-defined]
    assert isinstance(secs, (int, float))
    assert secs >= 0
    assert (
        rec.backtest_target_seconds  # type: ignore[attr-defined]
        == _perf.BACKTEST_TARGET_SECONDS
    )
    assert rec.n_leaders == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_timing_helper_exception_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced clock failure must NOT break the completed job."""
    from polyecho.backtest.jobs import _process_job

    def _boom() -> float:
        raise RuntimeError("forced clock failure")

    monkeypatch.setattr(
        "polyecho.backtest._perf.time.perf_counter", _boom
    )

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)
    job = _FakeJob([SWING])

    await _process_job(job, _make_engine())  # type: ignore[arg-type]  # must NOT raise

    # The job still reaches 'completed' despite the sabotaged clock.
    assert [t["status"] for t in transitions][-1] == "completed"


@pytest.mark.asyncio
async def test_done_log_swallows_timing_arithmetic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad elapsed value must not break the jobs.done emission path."""
    from polyecho.backtest.jobs import _process_job

    transitions: list[dict[str, object]] = []
    _patch_status_recorder(monkeypatch, transitions)
    job = _FakeJob([SWING])

    # Sentinel (-1) elapsed → the helper must SKIP the seconds fields but
    # still emit jobs.done and reach 'completed'.
    class _BadTimer:
        def __enter__(self) -> list[Any]:
            return [object()]  # non-numeric → comparison raises, swallowed

        def __exit__(self, *exc: object) -> None:
            return None

    import polyecho.backtest.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "perf_timer", lambda: _BadTimer())

    await _process_job(job, _make_engine())  # type: ignore[arg-type]  # must NOT raise

    assert [t["status"] for t in transitions][-1] == "completed"

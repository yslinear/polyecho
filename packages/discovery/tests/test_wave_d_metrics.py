"""
Wave D-metrics — discovery cycle-duration observability (PRD §7.1).

Asserts ``discovery_cycle_seconds`` + ``candidate_count`` appear in the
emitted structured log at cycle end, and that a forced exception inside
the timing helper does NOT propagate (best-effort).  NO behaviour change.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polyecho.discovery import _perf
from polyecho.discovery.worker import DiscoveryWorker
from polyecho.shared.datasource import FixtureDataSource

W_SWING = "0xswing1000000000000000000000000000000001"
W_SCALPER = "0xscalper200000000000000000000000000000002"


def _mock_scope() -> MagicMock:
    mock_scope = MagicMock()
    sess = AsyncMock()
    sess.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    sess.add = MagicMock()
    mock_scope.return_value.__aenter__ = AsyncMock(return_value=sess)
    mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_scope


def _cycle_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == "discovery_cycle_complete"]


def test_discovery_cycle_seconds_field_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_hourly emits discovery_cycle_seconds + candidate_count."""

    async def _run() -> None:
        ds = FixtureDataSource()
        worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)
        worker._known_wallets = {W_SWING, W_SCALPER}
        with patch(
            "polyecho.discovery.worker.session_scope", _mock_scope()
        ):
            with caplog.at_level(
                logging.INFO, logger="polyecho.discovery.worker"
            ):
                await worker.run_hourly()

    asyncio.get_event_loop().run_until_complete(_run())

    recs = _cycle_records(caplog)
    assert recs, "expected a discovery_cycle_complete log line"
    rec = recs[-1]
    secs = rec.discovery_cycle_seconds  # type: ignore[attr-defined]
    assert isinstance(secs, (int, float))
    assert secs >= 0
    assert rec.cycle == "hourly"  # type: ignore[attr-defined]
    assert (
        rec.discovery_cycle_target_seconds  # type: ignore[attr-defined]
        == _perf.DISCOVERY_CYCLE_TARGET_SECONDS
    )
    cc = rec.candidate_count  # type: ignore[attr-defined]
    assert isinstance(cc, int)
    assert cc >= 2  # at least the two seeded known wallets


def test_timing_helper_exception_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced clock failure must NOT break the discovery pass."""

    def _boom() -> float:
        raise RuntimeError("forced clock failure")

    monkeypatch.setattr(
        "polyecho.discovery._perf.time.perf_counter", _boom
    )

    async def _run() -> None:
        ds = FixtureDataSource()
        worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)
        with patch(
            "polyecho.discovery.worker.session_scope", _mock_scope()
        ):
            # Must NOT raise despite the sabotaged monotonic clock.
            await worker.run_hourly()

    asyncio.get_event_loop().run_until_complete(_run())


def test_log_cycle_duration_swallows_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logger that raises must not propagate out of the helper."""
    ds = FixtureDataSource()
    worker = DiscoveryWorker(ds, follower_capital_usd=1000.0)

    import polyecho.discovery.worker as worker_mod

    class _BoomLogger:
        def info(self, *a: object, **k: object) -> None:
            raise RuntimeError("logger exploded")

        def debug(self, *a: object, **k: object) -> None:
            pass

    monkeypatch.setattr(worker_mod, "logger", _BoomLogger())
    # Direct call with a valid duration — info() raises, helper swallows.
    worker._log_cycle_duration("hourly", 1.25)

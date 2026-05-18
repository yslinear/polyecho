"""
Wave D-metrics — copy-trade latency observability (PRD §7.1 / §2.3).

Asserts the timing fields appear in the emitted structured logs for a
``run_once`` pass and that a forced exception inside the timing helper
does NOT propagate (best-effort).  NO behaviour / exit-code change is
asserted alongside (placed == 0, stats unchanged).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest

from polyecho.core import _perf
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.copytrading import WalletBinding

LEADER = "0xswing1000000000000000000000000000000001"

ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.acct.waved")
BINDING = WalletBinding(
    signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False
)


def _follow() -> FollowSpec:
    from polyecho.core.evaluator import TemplateConfig

    tpl = TemplateConfig(
        copy_ratio=Decimal("0.05"),
        max_position_usd=Decimal("1000"),
        min_trade_size_usd=Decimal("1"),
        max_concurrent_positions=10,
        min_price=Decimal("0.02"),
        max_price=Decimal("0.98"),
    )
    return FollowSpec(
        account_id=ACCT, leader_address=LEADER, template=tpl, binding=BINDING
    )


def _records(caplog: pytest.LogCaptureFixture, msg: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.message == msg]


@pytest.mark.asyncio
async def test_copy_latency_fields_present_on_run_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run_once pass emits ``copy_latency_ms`` (+ ``detect_lag_ms``)
    per processed trade and aggregate latency on ``run_once_complete``.
    """
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    core = TraderCore(follows=[_follow()])
    with caplog.at_level(logging.INFO, logger="polyecho.core.trader"):
        stats = await core.run_once()

    # Behaviour unchanged: still detects, still places nothing (live lock).
    assert stats.detected > 0
    assert stats.placed == 0

    processed = _records(caplog, "copy_trade_processed")
    assert processed, "expected at least one copy_trade_processed log line"
    for rec in processed:
        lat = rec.copy_latency_ms  # type: ignore[attr-defined]
        assert isinstance(lat, (int, float))
        assert lat >= 0
        assert rec.copy_latency_target_ms == _perf.COPY_LATENCY_TARGET_MS  # type: ignore[attr-defined]
        # The fixture detected-trade carries an integer source timestamp,
        # so detect_lag_ms must be present, numeric and non-negative.
        lag = rec.detect_lag_ms  # type: ignore[attr-defined]
        assert isinstance(lag, (int, float))
        assert lag >= 0

    complete = _records(caplog, "run_once_complete")
    assert complete, "expected a run_once_complete log line"
    summary = complete[-1]
    assert summary.copy_latency_samples >= 1  # type: ignore[attr-defined]
    assert summary.copy_latency_ms_avg is not None  # type: ignore[attr-defined]
    assert summary.copy_latency_ms_avg >= 0  # type: ignore[attr-defined]
    assert summary.copy_latency_ms_max >= 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_timing_helper_exception_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced exception inside the timing path must NOT break the pass.

    We sabotage ``time.perf_counter`` used by the core perf helper; the
    run_once pass must still complete and place nothing.
    """
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    def _boom() -> float:
        raise RuntimeError("forced clock failure")

    monkeypatch.setattr("polyecho.core._perf.time.perf_counter", _boom)

    core = TraderCore(follows=[_follow()])
    stats = await core.run_once()  # must NOT raise

    assert stats.detected > 0
    assert stats.placed == 0


@pytest.mark.asyncio
async def test_log_copy_latency_swallows_logging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a logger that raises must not propagate out of the helper."""
    core = TraderCore(follows=[_follow()])

    import polyecho.core.trader as trader_mod

    class _BoomLogger:
        def info(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("logger exploded")

        def debug(self, *a: Any, **k: Any) -> None:
            # debug is the best-effort fallback — it must NOT raise here
            pass

    monkeypatch.setattr(trader_mod, "logger", _BoomLogger())

    # Direct call with a valid latency — the info() raises, the helper
    # swallows it via its debug() fallback; no exception escapes.
    core._log_copy_latency({"timestamp": 1_700_000_000}, LEADER, "0xtx", 12.5)

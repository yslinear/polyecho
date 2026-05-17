"""Backtest job-queue worker.

Polls the ``backtest_jobs`` table for pending jobs and dispatches them to
BacktestEngine.  Contract (FROZEN, shared with CLI agent):

  - Polls rows WHERE status='pending'  (FOR UPDATE SKIP LOCKED when possible)
  - Sets status→'running' + progress=0 before starting
  - Runs BacktestEngine.run() for every leader in leader_addresses
  - Writes result JSONB (per-leader triple-scenario) + status→'done'
  - On any exception: status→'failed', result={'error': str(exc)}
  - No secrets in logs (no private keys, no DB URLs)
"""

from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polyecho.shared.datasource import DataSource
from polyecho.shared.db import session_scope
from polyecho.shared.models import BacktestJob

from polyecho.backtest.engine import BacktestEngine, BacktestParams
from polyecho.backtest.scenarios import run_triple

logger = logging.getLogger(__name__)

# Polling interval for the continuous loop
_POLL_INTERVAL_SECONDS = 10


def _build_params(job: BacktestJob, leader_address: str) -> BacktestParams:
    """Construct BacktestParams from a BacktestJob row.

    template_params JSONB (if present) can carry any BacktestParams field.
    Top-level job columns (start_date, end_date, initial_capital_usd,
    slippage_bps, execution_delay_seconds) always win over template_params.
    """
    tp: dict[str, Any] = job.template_params or {}

    return BacktestParams(
        leader_address=leader_address,
        start_date=job.start_date
        if isinstance(job.start_date, date)
        else date.fromisoformat(str(job.start_date)),
        end_date=job.end_date
        if isinstance(job.end_date, date)
        else date.fromisoformat(str(job.end_date)),
        initial_capital_usd=float(job.initial_capital_usd),
        copy_ratio=float(tp.get("copy_ratio", 0.001)),
        fixed_size_usd=float(tp.get("fixed_size_usd", 0.0)),
        max_position_usd=float(tp.get("max_position_usd", 100.0)),
        min_trade_size_usd=float(tp.get("min_trade_size_usd", 5.0)),
        daily_loss_limit_usd=float(tp.get("daily_loss_limit_usd", 50.0)),
        max_concurrent_positions=int(tp.get("max_concurrent_positions", 10)),
        slippage_bps=int(job.slippage_bps),
        execution_delay_seconds=int(job.execution_delay_seconds),
        category_whitelist=tp.get("category_whitelist") or None,
    )


async def _set_status(
    session: AsyncSession,
    job_id: uuid.UUID,
    status: str,
    *,
    progress: float | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Update job status (and optionally progress/result) in place."""
    values: dict[str, Any] = {"status": status}
    if progress is not None:
        values["progress"] = progress
    if result is not None:
        values["result"] = result
    await session.execute(
        update(BacktestJob).where(BacktestJob.id == job_id).values(**values)
    )
    await session.commit()


async def _process_job(job: BacktestJob, engine: BacktestEngine) -> None:
    """Run all leaders for one job and write results back to DB."""
    job_id = job.id
    leaders: list[str] = job.leader_addresses or []

    logger.info("jobs.start", extra={"job_id": str(job_id), "n_leaders": len(leaders)})

    # Mark running
    async with session_scope() as session:
        await _set_status(session, job_id, "running", progress=0.0)

    all_results: dict[str, Any] = {}
    n = len(leaders)

    for i, leader in enumerate(leaders):
        try:
            params = _build_params(job, leader)
            triple = await run_triple(engine, params, leader)
            all_results[leader] = triple
        except Exception as exc:
            logger.warning(
                "jobs.leader_error",
                extra={"job_id": str(job_id), "leader": leader, "error": type(exc).__name__},
            )
            all_results[leader] = {"error": type(exc).__name__ + ": " + str(exc)[:200]}

        # Update progress (0→1 range, per PRD §6.1)
        progress = (i + 1) / n if n else 1.0
        async with session_scope() as session:
            await _set_status(session, job_id, "running", progress=progress * 100.0)

    async with session_scope() as session:
        await _set_status(
            session,
            job_id,
            "done",
            progress=100.0,
            result={"leaders": all_results},
        )
    logger.info("jobs.done", extra={"job_id": str(job_id)})


async def _fetch_pending(session: AsyncSession) -> list[BacktestJob]:
    """Fetch pending jobs.  Uses FOR UPDATE SKIP LOCKED when available."""
    try:
        result = await session.execute(
            select(BacktestJob)
            .where(BacktestJob.status == "pending")
            .with_for_update(skip_locked=True)
            .limit(5)  # process up to 5 at a time
        )
        return list(result.scalars().all())
    except Exception:
        # FOR UPDATE SKIP LOCKED may not be supported in all test DBs; fall back
        result = await session.execute(
            select(BacktestJob).where(BacktestJob.status == "pending").limit(5)
        )
        return list(result.scalars().all())


async def process_pending_jobs(datasource: DataSource, *, once: bool = False) -> None:
    """Main worker loop: poll and process pending backtest jobs.

    Args:
        datasource: Configured DataSource (fixture or live).
        once:       If True, process pending jobs then return immediately.
                    If False (default), loop forever polling every
                    _POLL_INTERVAL_SECONDS seconds.
    """
    engine = BacktestEngine(datasource=datasource)

    while True:
        try:
            async with session_scope() as session:
                jobs = await _fetch_pending(session)
                # Detach jobs from session before using them in sub-tasks
                job_snapshots = list(jobs)

            for job in job_snapshots:
                try:
                    await _process_job(job, engine)
                except Exception as exc:
                    logger.error(
                        "jobs.job_failed",
                        extra={
                            "job_id": str(job.id),
                            "error": type(exc).__name__,
                        },
                    )
                    # Write failure status (best-effort)
                    try:
                        async with session_scope() as session:
                            await _set_status(
                                session,
                                job.id,
                                "failed",
                                progress=0.0,
                                result={"error": type(exc).__name__ + ": " + str(exc)[:500]},
                            )
                    except Exception:
                        logger.exception("jobs.status_write_failed")

        except Exception:
            logger.exception("jobs.poll_error")

        if once:
            return

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

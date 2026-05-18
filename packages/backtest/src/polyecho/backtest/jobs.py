"""Backtest job-queue worker.

Polls the ``backtest_jobs`` table for pending jobs and dispatches them to
BacktestEngine.  Contract (FROZEN, shared with CLI agent):

  - Polls rows WHERE status='pending'  (FOR UPDATE SKIP LOCKED when possible)
  - Sets status→'running' + progress=0 before starting
  - Runs BacktestEngine.run() for every leader in leader_addresses
  - Writes result JSONB (per-leader triple-scenario) + status→'completed'
  - On any exception: status→'failed', result={'error': str(exc)}

  NOTE: terminal success status is 'completed' (NOT 'done'). This is the
  contract shared with the ORM model docstring, infra/migrations/0000_init.sql,
  packages/web/lib/db/schema.ts, and the CLI `backtest show`/data-table which
  all gate on status in ('completed', 'failed'). Writing 'done' here left
  every finished job stuck looking unfinished in the UI/CLI.
  - No secrets in logs (no private keys, no DB URLs)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from polyecho.backtest.engine import BacktestEngine, BacktestParams
from polyecho.backtest.scenarios import run_triple
from polyecho.shared.datasource import DataSource
from polyecho.shared.db import session_scope
from polyecho.shared.models import BacktestJob

logger = logging.getLogger(__name__)

# Polling interval for the continuous loop
_POLL_INTERVAL_SECONDS = 10

# I5: a job left in 'running' for longer than this is presumed orphaned by a
# crashed/killed worker (no worker holds it any more) and is recovered to
# 'pending' so a live worker re-processes it.  The threshold is generous
# relative to a normal multi-leader triple-scenario run (seconds → low
# minutes) but well under any human-noticeable stall.
_STALE_RUNNING_SECONDS = 30 * 60  # 30 minutes


def _build_params(job: BacktestJob, leader_address: str) -> BacktestParams:
    """Construct BacktestParams from a BacktestJob row.

    template_params JSONB (if present) can carry any BacktestParams field.
    Top-level job columns (start_date, end_date, initial_capital_usd,
    slippage_bps, execution_delay_seconds) always win over template_params.

    NOTE: the CLI (`polyecho backtest new`) writes EXPLICIT JSON ``null`` for
    every option the user did not pass (max_position_usd, min_trade_size_usd,
    daily_loss_limit_usd, max_concurrent_positions, fixed_size_usd, ...). With
    plain ``tp.get(key, default)`` the key *exists* with value ``None``, so the
    default is NOT used and ``float(None)`` / ``int(None)`` raises
    ``TypeError: float() argument must be ... not 'NoneType'`` — every
    default-option CLI job failed with a per-leader error envelope instead of a
    real backtest. ``_opt`` below coalesces an explicit ``None`` to the
    documented PRD §5.4 default so CLI-enqueued jobs run correctly.
    """
    tp: dict[str, Any] = job.template_params or {}

    def _opt(key: str, default: Any) -> Any:
        """tp[key] unless absent OR explicitly None → then `default`."""
        val = tp.get(key)
        return default if val is None else val

    return BacktestParams(
        leader_address=leader_address,
        start_date=job.start_date
        if isinstance(job.start_date, date)
        else date.fromisoformat(str(job.start_date)),
        end_date=job.end_date
        if isinstance(job.end_date, date)
        else date.fromisoformat(str(job.end_date)),
        initial_capital_usd=float(job.initial_capital_usd),
        copy_ratio=float(_opt("copy_ratio", 0.001)),
        fixed_size_usd=float(_opt("fixed_size_usd", 0.0)),
        max_position_usd=float(_opt("max_position_usd", 100.0)),
        min_trade_size_usd=float(_opt("min_trade_size_usd", 5.0)),
        daily_loss_limit_usd=float(_opt("daily_loss_limit_usd", 50.0)),
        max_concurrent_positions=int(_opt("max_concurrent_positions", 10)),
        slippage_bps=int(job.slippage_bps),
        # fee_bps has no dedicated job column (schema is Wave A / P1-11);
        # it rides in template_params, defaulting to PRD §5.4's 0 bps.
        fee_bps=int(_opt("fee_bps", 0)),
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
            "completed",
            progress=100.0,
            result={"leaders": all_results},
        )
    logger.info("jobs.done", extra={"job_id": str(job_id)})


async def _recover_stale_running(session: AsyncSession) -> int:
    """I5: reset orphaned 'running' jobs back to 'pending'.

    A worker crash / OOM-kill / container restart leaves a job stuck in
    'running' forever — no live worker owns it, so it is never finished.
    On every poll (and at startup) we sweep jobs that have been 'running'
    longer than ``_STALE_RUNNING_SECONDS`` and flip them back to 'pending'
    (progress reset to 0) so a healthy worker re-claims and re-runs them.

    NOTE on "resume from cursor": ``backtest_jobs`` persists no per-leader
    mid-run cursor column (schema is owned by Wave A / P1-11 and is
    out-of-scope here), and BacktestEngine.run() is deterministic and
    side-effect-free, so "resume" == a clean idempotent re-run of all
    leaders.  ``created_at`` is the only available age signal; it is a safe
    *lower bound* on how long the job has existed, so a job 'running' with
    ``created_at`` older than the threshold is unambiguously stale (a fresh
    job cannot be both newly-created AND older than the threshold).

    The UPDATE is a single atomic statement guarded by
    ``status == 'running'`` so concurrent workers cannot both recover the
    same row (whoever commits first wins; the other's WHERE no longer
    matches).

    Returns:
        Number of rows recovered (0 when none are stale).
    """
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=_STALE_RUNNING_SECONDS)
    result = await session.execute(
        update(BacktestJob)
        .where(
            BacktestJob.status == "running",
            BacktestJob.created_at < cutoff,
        )
        .values(status="pending", progress=0.0)
    )
    await session.commit()
    recovered = int(getattr(result, "rowcount", 0) or 0)
    if recovered:
        logger.warning(
            "jobs.stale_running_recovered",
            extra={"recovered": recovered, "stale_seconds": _STALE_RUNNING_SECONDS},
        )
    return recovered


async def _fetch_pending(session: AsyncSession) -> list[BacktestJob]:
    """Atomically claim up to 5 pending jobs (flip them to 'running').

    Hardened double-processing guard (I5): selecting pending rows and
    *then* processing them in a separate session is racy — two workers can
    both read the same 'pending' row.  We now claim inside ONE transaction:

    * Fast path: ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent
      workers never even see each other's locked rows, then flip the
      claimed rows to ``status='running'`` and commit before returning.
    * Fallback (test DBs / engines without SKIP LOCKED): re-select then
      issue a **conditional** ``UPDATE ... WHERE id = ANY(:ids) AND
      status='pending'`` — only rows this worker actually transitioned
      (rowcount-confirmed) are returned, so a second worker that read the
      same ids loses the race and gets nothing.  Either way a job is
      returned to the caller ONLY if this worker owns it.
    """
    try:
        result = await session.execute(
            select(BacktestJob)
            .where(BacktestJob.status == "pending")
            .with_for_update(skip_locked=True)
            .limit(5)  # process up to 5 at a time
        )
        jobs = list(result.scalars().all())
        if jobs:
            await session.execute(
                update(BacktestJob)
                .where(BacktestJob.id.in_([j.id for j in jobs]))
                .values(status="running", progress=0.0)
            )
            await session.commit()
        return jobs
    except Exception:
        # FOR UPDATE SKIP LOCKED may not be supported in all test DBs; fall
        # back to a conditional compare-and-set claim so two workers cannot
        # both win the same pending row.
        await session.rollback()
        result = await session.execute(
            select(BacktestJob).where(BacktestJob.status == "pending").limit(5)
        )
        candidates = list(result.scalars().all())
        claimed: list[BacktestJob] = []
        for job in candidates:
            upd = await session.execute(
                update(BacktestJob)
                .where(
                    BacktestJob.id == job.id,
                    BacktestJob.status == "pending",
                )
                .values(status="running", progress=0.0)
            )
            if int(getattr(upd, "rowcount", 0) or 0) == 1:
                claimed.append(job)
        await session.commit()
        return claimed


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
            # I5: recover orphaned 'running' jobs (crashed-worker leftovers)
            # at startup and on every poll BEFORE claiming new pending work,
            # so recovered jobs re-enter the same poll's pending claim.
            try:
                async with session_scope() as session:
                    await _recover_stale_running(session)
            except Exception:
                logger.exception("jobs.stale_recovery_error")

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

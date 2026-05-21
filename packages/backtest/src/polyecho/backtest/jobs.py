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

from polyecho.backtest._heartbeat import (
    PHASE_IDLE,
    PHASE_RUNNING,
    write_backtest_heartbeat,
)
from polyecho.backtest._perf import BACKTEST_TARGET_SECONDS, perf_timer
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

    # R12 #3: the job arrives here ALREADY claimed — ``_fetch_pending`` flipped
    # it to ``status='running', progress=0`` inside the claim transaction (that
    # atomic compare-and-set IS the ownership handshake).  Re-issuing the same
    # ``status='running', progress=0`` write here was a redundant second
    # round-trip that clobbered the claim a worker had just committed; under
    # the no-SKIP-LOCKED fallback it also masked a lost race (the row could
    # have been re-claimed in between).  The claim already established the
    # running state — go straight to per-leader processing.

    all_results: dict[str, Any] = {}
    n = len(leaders)

    # PRD §7.1 — a single 90-day backtest should complete in < 60 s.
    # Best-effort monotonic timing around the actual compute; the timer
    # cannot raise into the job outcome (see polyecho.backtest._perf).
    with perf_timer() as elapsed:
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

    # P11 Bug 2: if ANY leader produced an ``"error"`` envelope, the job is
    # NOT a success — calling it 'completed' silently is exactly how the
    # UI showed "已完成 / completed" for job 557eef18 despite every leader
    # carrying ``RuntimeError: LiveDataSource must be used as an async
    # context manager``.  A job with any leader-level error is reported as
    # ``status='failed'`` so the existing failed-state UI surfaces it.
    # Per-leader envelopes remain inside ``result["leaders"]`` unchanged
    # (schema is preserved — no extra wrapper layer added).
    has_error = any(
        isinstance(leader_result, dict) and "error" in leader_result
        for leader_result in all_results.values()
    )
    final_status = "failed" if has_error else "completed"
    async with session_scope() as session:
        await _set_status(
            session,
            job_id,
            final_status,
            progress=100.0,
            result={"leaders": all_results},
        )
    # Best-effort backtest-duration observability — wrapped so a
    # timing/log failure can NEVER affect the completed job outcome.
    # No event is emitted (shared ``EventType`` is frozen) — LOG only.
    _done_extra: dict[str, Any] = {"job_id": str(job_id)}
    try:
        _secs = elapsed[0]
        if _secs >= 0:
            _done_extra["backtest_seconds"] = round(_secs, 3)
            _done_extra["backtest_target_seconds"] = BACKTEST_TARGET_SECONDS
            _done_extra["n_leaders"] = n
    except Exception as exc:  # noqa: BLE001 - observability is best-effort
        logger.debug(
            "backtest_seconds_log_failed_continuing",
            extra={"error": str(exc)},
        )
    logger.info("jobs.done", extra=_done_extra)


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
    leaders.

    R12 IMP-1 — false-recovery of a *backpressured* running job.  There is
    NO ``started_at`` column (the schema is E-schema-frozen and adding one is
    explicitly out of scope), so ``created_at`` is the only timestamp.  But
    ``created_at`` is the *enqueue* time, NOT the time the worker picked the
    job up.  Under queue backpressure (many pending jobs, a job claimed long
    after it was created) a job can be *legitimately running right now* yet
    have ``created_at`` far older than the stale threshold — the old
    condition (``status='running' AND created_at < cutoff``) would then
    spuriously yank a healthy in-flight job back to 'pending' mid-run.

    Without a new column the only crash-vs-alive signal available is
    ``progress``: ``_process_job`` writes ``progress = (i+1)/n * 100`` after
    *every* completed leader, so a job that has made ANY progress
    (``progress > 0``) is provably owned by a live worker that is actively
    advancing it — it must NOT be recovered regardless of ``created_at``.
    The recovery condition is therefore tightened to::

        status == 'running' AND progress == 0 AND created_at < cutoff

    A genuinely-orphaned job (worker crashed/killed) is still recovered: it
    sits at ``progress == 0`` (the claim sets progress=0 and the crash
    happened before the first leader finished) with an old ``created_at``.

    RESIDUAL LIMITATION (documented, accepted — no-schema-change constraint):
    a worker that crashes *after* it has completed at least one leader
    (``progress > 0``) on a multi-leader job is NOT auto-recovered by this
    sweep — its row stays 'running' until an operator intervenes.  Fully
    closing this gap requires a real ``started_at`` / heartbeat column,
    which is E-schema-frozen and out of scope.  The chosen trade-off
    deliberately favours *never killing a healthy backpressured job*
    (a silent correctness/cost hazard — duplicate runs, doubled fees) over
    auto-recovering the rarer mid-multi-leader crash (an availability
    blip an operator can resolve).  ``_STALE_RUNNING_SECONDS`` (30 min) is
    left unchanged: widening it would only enlarge the window in which the
    progress==0 guard must hold and does not remove the backpressure race.

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
            # R12 IMP-1: progress==0 ⇒ no live worker has advanced this job;
            # any progress>0 means a worker is actively running it (do NOT
            # recover a backpressured-but-healthy in-flight job).
            BacktestJob.progress == 0,
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
            # R12 #2: the bulk UPDATE above mutated the rows in the DB but the
            # in-memory ORM instances still carry the pre-claim
            # ``status='pending'`` / ``progress`` values (the UPDATE went
            # through a Core statement, not the ORM identity map; commit also
            # expires nothing because expire_on_commit is off on this session
            # factory).  Expire ONLY the two stale attributes (not the whole
            # object) and re-`refresh` while the session is still open, so a
            # later ``job.status`` read sees the committed ``'running'`` while
            # the columns ``_process_job`` actually consumes (id,
            # leader_addresses, template_params, dates) stay loaded — a blanket
            # ``session.expire(j)`` would make those raise
            # ``DetachedInstanceError`` once the session closes (jobs are
            # processed AFTER the claim session is torn down).
            for j in jobs:
                session.expire(j, ["status", "progress"])
                await session.refresh(j, ["status", "progress"])
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
        # R12 #2: same staleness as the fast path — the conditional UPDATE
        # transitioned these rows to ``status='running'`` in the DB, but the
        # returned ORM objects were SELECTed *before* the claim and still
        # report ``status='pending'``.  Expire ONLY status/progress and
        # refresh them while the session is open so a downstream ``.status``
        # read reflects the committed claim without detaching the columns
        # ``_process_job`` later consumes.
        for job in claimed:
            session.expire(job, ["status", "progress"])
            await session.refresh(job, ["status", "progress"])
        return claimed


async def process_pending_jobs(datasource: DataSource, *, once: bool = False) -> None:
    """Main worker loop: poll and process pending backtest jobs.

    Args:
        datasource: Configured DataSource (fixture or live).
        once:       If True, **drain all currently-pending jobs** (poll
                    repeatedly until a poll claims zero pending rows) then
                    return.  R12 #5: ``_fetch_pending`` claims at most 5 rows
                    per call, so the old single-iteration ``once`` exited
                    after the first ≤5 jobs while the CLI/help promised
                    "process all pending then exit" — 6+ pending jobs needed
                    several invocations.  We now keep polling until a poll
                    finds nothing pending (and nothing newly recovered),
                    matching operator expectation, while preserving the
                    ``--once`` exit-code contract (returns normally → exit 0).
                    If False (default), loop forever polling every
                    _POLL_INTERVAL_SECONDS seconds.
    """
    engine = BacktestEngine(datasource=datasource)

    while True:
        # Default to "no work this iteration" so a poll that raises before
        # the claim still has a defined exit decision below (a failed poll in
        # --once mode exits rather than spinning — the outer except logs it).
        claimed_any = False
        # Per-cycle heartbeat counters: jobs processed / failed THIS poll
        # only (reset every iteration so the heartbeat reflects the latest
        # cycle, not a cumulative running total).
        cycle_processed = 0
        cycle_failed = 0
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

            # R12 #5: in --once (drain) mode, an iteration that claimed at
            # least one job means there may be more pending rows beyond this
            # ≤5 batch (or freshly recovered ones), so we must poll again
            # before exiting.  Only an iteration that claimed *zero* jobs
            # proves the pending queue is empty → safe to exit.
            claimed_any = bool(job_snapshots)

            for job in job_snapshots:
                try:
                    await _process_job(job, engine)
                    cycle_processed += 1
                except Exception as exc:
                    cycle_failed += 1
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

        # Per-cycle liveness: emit ONE backtest_heartbeat after every poll
        # (including the --once drain's iterations and its final empty poll).
        # phase = running when this cycle claimed work, else idle; counters
        # are this cycle's processed/failed tallies.  STRICTLY fail-soft:
        # write_backtest_heartbeat never raises (a heartbeat failure must
        # NEVER abort the worker or affect job processing), but it is also
        # wrapped here as defence-in-depth so the loop/exit/sleep below is
        # reached unconditionally.
        try:
            await write_backtest_heartbeat(
                PHASE_RUNNING if claimed_any else PHASE_IDLE,
                {"processed": cycle_processed, "failed": cycle_failed},
            )
        except Exception:  # noqa: BLE001 - heartbeat must never abort work
            logger.exception("jobs.heartbeat_error")

        if once:
            # R12 #5: drain semantics — exit only once a poll claimed zero
            # jobs (queue provably empty).  If this iteration claimed work,
            # loop again immediately (no inter-poll sleep) to pick up the
            # next ≤5 batch / any rows recovered from stale 'running'.  This
            # preserves the ``--once`` exit-code contract: the function
            # returns normally (→ exit 0) once, and only once, the queue is
            # fully drained.
            if not claimed_any:
                return
            continue

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

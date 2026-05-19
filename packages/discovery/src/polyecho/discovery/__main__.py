"""Entry-point: python -m polyecho.discovery

Starts the discovery-worker service (PRD §5.2, §5.3).

Run modes
---------
``--once``
    Execute one full daily + hourly pass then exit.  Useful for CI and
    manual one-shot runs (``polyecho discovery run --once``).

default (no flags)
    Scheduled loop:
    - Daily tasks at startup, then every 24 h.
    - Hourly tasks at startup, then every 1 h.
    - Weekly seed sync at startup, then every 7 days.
    Uses simple asyncio scheduling; no extra dependencies.

Structured logging
------------------
All log records are emitted as JSON via :func:`polyecho.shared.logging.configure_logging`.
No secrets are logged.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from polyecho.discovery._lifecycle import datasource_lifecycle
from polyecho.discovery.worker import DiscoveryWorker
from polyecho.shared.datasource import get_datasource
from polyecho.shared.logging import configure_logging

# F0 heartbeat contract (FROZEN, lives in packages/shared — code AGAINST
# it, never modify/recreate it).  The lazy-import + sync/coroutine-tolerant
# + CancelledError-reraising fail-soft *mechanism* is the ONE shared seam
# ``polyecho.shared.health_emit.emit_heartbeat_safe`` (imported lazily
# INSIDE :func:`_emit_discovery_heartbeat` so a heartbeat subsystem problem
# — incl. an absent shared package in this package's isolated test base —
# can NEVER abort discovery, fail-soft task 2).

logger = logging.getLogger(__name__)

_DAILY_INTERVAL = timedelta(hours=24)
_HOURLY_INTERVAL = timedelta(hours=1)
_WEEKLY_INTERVAL = timedelta(days=7)

# I1 fix — error backoff, DISTINCT from the normal cadence.
#
# Previously, ANY unhandled exception in a task (empty DB at startup,
# network not ready, transient DB hiccup) caused the loop to sleep the
# FULL ``interval`` (up to 7 days for the weekly task) before retrying —
# discovery would go dark for a day after a single startup blip.
#
# Now a failed iteration sleeps a SHORT ``retry_sleep`` instead: start at
# 60 s and double on each *consecutive* failure (capped at 1 h), so a
# persistent outage still backs off politely without hammering, while a
# transient blip recovers within a minute.  A SUCCESSFUL iteration resets
# the backoff and resumes the normal long cadence.
_RETRY_SLEEP_INITIAL = timedelta(seconds=60)
_RETRY_SLEEP_MAX = timedelta(hours=1)


async def _emit_discovery_heartbeat(
    worker: DiscoveryWorker, *, phase: str
) -> None:
    """Write the ``discovery_heartbeat`` via the FROZEN F0 shared contract.

    Populates the F0 heartbeat shape
    ``{ts, phase, datasource, live_trading, counters}`` where ``counters``
    are this-pass candidate metrics (scanned / scored / emitted) drained
    from the worker.

    Delegates the fail-soft write to the ONE shared seam
    :func:`polyecho.shared.health_emit.emit_heartbeat_safe` (lazy F0
    import + sync/coroutine tolerance + swallow-all-but-CancelledError),
    imported lazily INSIDE so this package's isolated test base — which
    predates the shared ``health``/``health_emit`` modules — keeps
    ``import polyecho.discovery`` working (a caught ImportError degrades
    to a no-op heartbeat).

    STRICTLY fail-soft (task 2): a missing ``polyecho.shared.health``
    module, a write failure, or any other exception is logged (the
    unchanged ``discovery_heartbeat_failed_continuing`` event, via the
    seam's ``on_error`` hook) and swallowed — a heartbeat problem must
    NEVER abort a discovery pass.  ``asyncio.CancelledError`` still
    propagates (graceful shutdown is never a "heartbeat failure").
    """
    try:
        counters = worker.drain_pass_counters()

        # Lazy import INSIDE the guard: the shared seam (and the frozen
        # shared.health it wraps) may be absent in this package's isolated
        # base — an ImportError must degrade to a no-op, never abort a pass.
        from polyecho.shared.health_emit import emit_heartbeat_safe

        await emit_heartbeat_safe(
            "discovery",
            phase=phase,
            datasource=worker.datasource_name,
            live_trading=False,  # discovery never trades (read-only service)
            counters=counters,
            on_error=lambda exc: logger.warning(
                "discovery_heartbeat_failed_continuing",
                extra={"phase": phase, "error": str(exc)},
            ),
        )
        logger.debug(
            "discovery_heartbeat_written",
            extra={"phase": phase, "counters": counters},
        )
    except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
        # Final backstop: the seam is fail-soft, but draining counters or
        # importing the seam itself must still never abort the pass — same
        # event name as before for log-assertion parity.
        logger.warning(
            "discovery_heartbeat_failed_continuing",
            extra={"phase": phase, "error": str(exc)},
        )


async def _run_once(worker: DiscoveryWorker) -> None:
    """Execute one full daily + hourly pass, then emit a heartbeat."""
    logger.info("run_once: starting daily pipeline")
    await worker.run_daily()
    logger.info("run_once: starting hourly pipeline")
    await worker.run_hourly()
    # One heartbeat for the whole one-shot pass (task 2).  Fail-soft.
    await _emit_discovery_heartbeat(worker, phase="once")
    logger.info("run_once: complete")


async def _scheduled_loop(worker: DiscoveryWorker) -> None:
    """Run daily, hourly, and weekly tasks on their respective cadences.

    Tasks are staggered slightly to avoid thundering-herd on startup.
    Each task loop is independent; an exception in one does not stop others.
    """
    async def _loop(
        coro_factory: Callable[[], Awaitable[None]],
        interval: timedelta,
        name: str,
    ) -> None:
        # Consecutive-failure backoff (I1).  Reset to the initial value
        # after every SUCCESSFUL iteration; doubled (capped) after each
        # failure.  This is the sleep used ONLY on the error path — it is
        # deliberately decoupled from the long normal ``interval``.
        retry_sleep = _RETRY_SLEEP_INITIAL
        while True:
            try:
                await coro_factory()
            except Exception:
                logger.exception(
                    "%s task raised an unhandled exception; retrying in %s",
                    name,
                    retry_sleep,
                )
                # Short, capped exponential backoff — NOT the full
                # interval (would dark-out discovery for up to a day/week
                # after a single startup blip).
                await asyncio.sleep(retry_sleep.total_seconds())
                retry_sleep = min(retry_sleep * 2, _RETRY_SLEEP_MAX)
                continue
            # Success → emit one heartbeat for this completed pass (task
            # 2), then reset backoff and resume the normal long cadence.
            # The heartbeat helper is itself fail-soft so it can never
            # turn a successful pass into the error/backoff path.
            await _emit_discovery_heartbeat(worker, phase=name)
            retry_sleep = _RETRY_SLEEP_INITIAL
            logger.info("%s: sleeping %s until next run", name, interval)
            await asyncio.sleep(interval.total_seconds())

    daily_task = asyncio.create_task(
        _loop(worker.run_daily, _DAILY_INTERVAL, "daily"), name="discovery-daily"
    )
    hourly_task = asyncio.create_task(
        _loop(worker.run_hourly, _HOURLY_INTERVAL, "hourly"), name="discovery-hourly"
    )
    weekly_task = asyncio.create_task(
        _loop(worker.run_weekly, _WEEKLY_INTERVAL, "weekly"), name="discovery-weekly"
    )

    try:
        await asyncio.gather(daily_task, hourly_task, weekly_task)
    except asyncio.CancelledError:
        logger.info("scheduled loop cancelled, shutting down")
        daily_task.cancel()
        hourly_task.cancel()
        weekly_task.cancel()
        raise


async def main(*, once: bool = False) -> None:
    """Configure logging, build datasource, dispatch to run mode.

    Args:
        once: When True, run one pass then exit.  Default: scheduled loop.
    """
    configure_logging()
    logger.info(
        "discovery-worker starting",
        extra={"mode": "once" if once else "scheduled"},
    )

    # LIFECYCLE FIX (task 1): the datasource MUST be entered as an async
    # context manager around BOTH run modes.  ``LiveDataSource.__aenter__``
    # builds its httpx client; without ``async with`` every live call
    # raised ``RuntimeError: LiveDataSource must be used as an async
    # context manager``.  ``datasource_lifecycle`` enters/exits live (the
    # fix) and is a transparent no-op for ``FixtureDataSource`` (which has
    # no ``__aenter__``/``__aexit__``), so the fixture path is preserved
    # byte-for-byte while the live path now actually runs.
    async with datasource_lifecycle(get_datasource()) as ds:
        worker = DiscoveryWorker(ds)

        if once:
            await _run_once(worker)
        else:
            await _scheduled_loop(worker)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m polyecho.discovery",
        description="PolyEcho discovery-worker service",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run one full daily+hourly pass then exit.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(once=args.once))

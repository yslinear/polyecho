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

from polyecho.discovery.worker import DiscoveryWorker
from polyecho.shared.datasource import get_datasource
from polyecho.shared.logging import configure_logging

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


async def _run_once(worker: DiscoveryWorker) -> None:
    """Execute one full daily + hourly pass."""
    logger.info("run_once: starting daily pipeline")
    await worker.run_daily()
    logger.info("run_once: starting hourly pipeline")
    await worker.run_hourly()
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
            # Success → reset backoff and resume the normal long cadence.
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

    ds = get_datasource()
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

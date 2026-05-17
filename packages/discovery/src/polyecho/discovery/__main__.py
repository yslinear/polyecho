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
from datetime import timedelta

from polyecho.shared.datasource import get_datasource
from polyecho.shared.logging import configure_logging

from polyecho.discovery.worker import DiscoveryWorker

logger = logging.getLogger(__name__)

_DAILY_INTERVAL = timedelta(hours=24)
_HOURLY_INTERVAL = timedelta(hours=1)
_WEEKLY_INTERVAL = timedelta(days=7)


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
    async def _loop(coro_factory, interval: timedelta, name: str) -> None:
        while True:
            try:
                await coro_factory()
            except Exception:
                logger.exception("%s task raised an unhandled exception", name)
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

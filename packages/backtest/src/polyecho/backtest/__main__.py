"""Entry-point: python -m polyecho.backtest

Starts the backtest job-queue worker (PRD §5.4).

Usage:
    python -m polyecho.backtest               # loop forever, poll every 10 s
    python -m polyecho.backtest --once        # process pending jobs then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from polyecho.shared.datasource import get_datasource
from polyecho.shared.logging import configure_logging

from polyecho.backtest.jobs import process_pending_jobs

logger = logging.getLogger(__name__)


async def main() -> None:
    """Configure logging, parse args, then run the backtest job-queue worker."""
    configure_logging()

    parser = argparse.ArgumentParser(
        prog="python -m polyecho.backtest",
        description="PolyEcho backtest worker — processes pending backtest_jobs rows.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Process all currently pending jobs then exit (useful for CLI / one-shots).",
    )
    args = parser.parse_args()

    logger.info("backtest-worker.starting", extra={"once": args.once})

    datasource = get_datasource()
    await process_pending_jobs(datasource, once=args.once)

    logger.info("backtest-worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())

"""Entry-point: python -m polyecho.backtest

Starts the backtest service loop / job-queue worker (PRD §5.4).
"""

from __future__ import annotations

import asyncio
import logging

from polyecho.shared.logging import configure_logging

from polyecho.backtest.engine import BacktestEngine

logger = logging.getLogger(__name__)


async def main() -> None:
    """Configure logging, then run the backtest job-queue worker."""
    configure_logging()
    logger.info("starting backtest-worker")
    # TODO: poll backtest_jobs table for queued jobs and dispatch to BacktestEngine
    raise NotImplementedError("Phase 2")


if __name__ == "__main__":
    asyncio.run(main())

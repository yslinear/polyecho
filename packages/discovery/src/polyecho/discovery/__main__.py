"""Entry-point: python -m polyecho.discovery

Starts the discovery-worker service (PRD §5.2, §5.3).
"""

from __future__ import annotations

import asyncio
import logging

from polyecho.shared.logging import configure_logging

from polyecho.discovery.worker import DiscoveryWorker

logger = logging.getLogger(__name__)


async def main() -> None:
    """Configure logging, then run the DiscoveryWorker service loop."""
    configure_logging()
    logger.info("starting discovery-worker")
    worker = DiscoveryWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

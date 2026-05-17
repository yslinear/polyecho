"""Entry-point: python -m polyecho.core

Starts the trader-core service (PRD §4.2, §5.5).
"""

from __future__ import annotations

import asyncio
import logging

from polyecho.core.trader import TraderCore
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    """Configure logging, then run the TraderCore service loop."""
    configure_logging()
    logger.info("starting trader-core")
    core = TraderCore()
    await core.run()


if __name__ == "__main__":
    asyncio.run(main())

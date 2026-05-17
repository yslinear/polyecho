"""Entry-point: python -m polyecho.push

Starts the web-push-sender service (PRD §5.8).
"""

from __future__ import annotations

import asyncio
import logging

from polyecho.shared.logging import configure_logging

from polyecho.push.sender import WebPushSender

logger = logging.getLogger(__name__)


async def main() -> None:
    """Configure logging, then run the WebPushSender polling loop."""
    configure_logging()
    logger.info("starting web-push-sender")
    sender = WebPushSender()
    await sender.run()


if __name__ == "__main__":
    asyncio.run(main())

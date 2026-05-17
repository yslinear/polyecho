"""Entry-point: ``python -m polyecho.push`` (PRD §5.8).

Modes:

* ``--once``  — run the startup backfill (deliver every currently-unpushed
  ``events`` row), then exit ``0``.  This is the CI-smoke path: in fixture
  mode with no subscriptions it drains and stamps cleanly without any
  outbound network.
* default     — the supervised service: backfill, then stream live
  ``polyecho_events`` NOTIFYs forever, with an APScheduler cron job at
  23:00 ``settings.tz`` emitting the daily PnL summary.  The loop
  reconnects on any drop; one bad event never kills it.

Structured JSON logging is configured first; the VAPID private key and
subscription secrets are never logged (``polyecho.shared.logging`` redacts
defence-in-depth).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from polyecho.push.sender import WebPushSender
from polyecho.shared.config import get_settings
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)

#: Nightly PnL digest fires at 23:00 in ``settings.tz`` (PRD §5.8).
_DAILY_SUMMARY_HOUR = 23
_DAILY_SUMMARY_MINUTE = 0


def _build_scheduler(sender: WebPushSender, tz: str) -> AsyncIOScheduler:
    """Create the APScheduler instance with the 23:00 daily-summary job.

    The job is *not* started here; the caller starts/shuts it down around
    the supervised stream so the scheduler shares the running event loop.
    """
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        sender.emit_daily_pnl_summary,
        CronTrigger(
            hour=_DAILY_SUMMARY_HOUR,
            minute=_DAILY_SUMMARY_MINUTE,
            timezone=tz,
        ),
        id="daily_pnl_summary",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    return scheduler


async def main(argv: list[str] | None = None) -> int:
    """Configure logging, then run the requested mode."""
    args = argv if argv is not None else sys.argv[1:]
    settings = get_settings()
    configure_logging(settings.log_level)
    once = "--once" in args

    logger.info(
        "starting web-push-sender",
        extra={
            "mode": "once" if once else "supervised",
            "datasource": settings.datasource,
        },
    )
    sender = WebPushSender()

    if once:
        processed = await sender.run_once()
        logger.info(
            "web-push-sender --once complete",
            extra={"processed": processed},
        )
        return 0

    scheduler = _build_scheduler(sender, settings.tz)
    scheduler.start()
    try:
        await sender.run()
    finally:
        scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

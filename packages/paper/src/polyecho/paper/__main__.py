"""Entry-point: ``python -m polyecho.paper``.

Starts the paper-trading-worker service (C1-P3).

Default OFF
-----------
The worker does NOTHING unless ``POLYECHO_PAPER_TRADING_ENABLED=true``.
The flag defaults to ``false`` (see :class:`polyecho.shared.config.Settings`
and ``.env.example``) so deploying the service is inert until an operator
explicitly opts in.  A disabled worker logs one line and exits cleanly.

Run modes
---------
``--once``
    Execute one paper cycle then exit (CI / manual one-shot).

default (no flags)
    Scheduled loop: a cycle at startup, then every ~15 min.  Simple
    asyncio scheduling, no APScheduler -- mirrors ``discovery/__main__``.

The worker is a *validation* layer and NEVER trades real money.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

from polyecho.paper.worker import PaperWorker
from polyecho.shared.config import get_settings
from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _datasource_lifecycle(ds: DataSource) -> AsyncIterator[DataSource]:
    """Enter *ds* as an async context manager when it implements that protocol.

    ``LiveDataSource`` builds its ``httpx.AsyncClient`` in ``__aenter__``;
    calling any data method before that raises ``RuntimeError``.
    ``FixtureDataSource`` has no I/O lifecycle, so this adapter enters the
    CM only when ``ds`` actually is one (mirrors backtest/__main__).
    """
    if isinstance(ds, AbstractAsyncContextManager):
        cm: AbstractAsyncContextManager[object] = ds
        async with cm:
            yield ds
    else:
        yield ds


#: Normal cycle cadence (~15 min, per the C1 roadmap).
_CYCLE_INTERVAL = timedelta(minutes=15)

#: Error backoff, distinct from the normal cadence: a failed cycle sleeps
#: a short, doubling, capped interval instead of the full 15 min so a
#: transient blip recovers quickly while a persistent outage backs off.
_RETRY_SLEEP_INITIAL = timedelta(seconds=60)
_RETRY_SLEEP_MAX = timedelta(minutes=15)


async def _run_once(worker: PaperWorker) -> None:
    """Run exactly one paper cycle."""
    logger.info("paper_run_once_start")
    result = await worker.run_cycle()
    logger.info(
        "paper_run_once_complete",
        extra={
            "skipped_locked_cycle": result.skipped_locked_cycle,
            "sessions_simulated": result.sessions_simulated,
        },
    )


async def _scheduled_loop(worker: PaperWorker) -> None:
    """Run a paper cycle at startup, then every ~15 min, with backoff."""
    retry_sleep = _RETRY_SLEEP_INITIAL
    while True:
        try:
            await worker.run_cycle()
        except asyncio.CancelledError:
            logger.info("paper_scheduled_loop_cancelled")
            raise
        except Exception:
            logger.exception("paper_cycle_failed_retrying", extra={"retry": str(retry_sleep)})
            await asyncio.sleep(retry_sleep.total_seconds())
            retry_sleep = min(retry_sleep * 2, _RETRY_SLEEP_MAX)
            continue
        retry_sleep = _RETRY_SLEEP_INITIAL
        logger.info("paper_cycle_sleeping", extra={"interval": str(_CYCLE_INTERVAL)})
        await asyncio.sleep(_CYCLE_INTERVAL.total_seconds())


async def main(*, once: bool = False) -> None:
    """Configure logging, honour the default-off flag, dispatch run mode.

    Args:
        once: When True, run one cycle then exit.  Default: scheduled loop.
    """
    configure_logging()
    settings = get_settings()

    # DEFAULT-OFF GATE: the paper worker is inert unless the operator
    # explicitly opts in.  A disabled worker logs once and exits 0 so the
    # service can be deployed (C1-P6) without doing anything.
    if not settings.paper_trading_enabled:
        logger.info(
            "paper_worker_disabled",
            extra={
                "reason": (
                    "POLYECHO_PAPER_TRADING_ENABLED is false (default) -- the "
                    "paper-trading worker does nothing. Set it to true to "
                    "enable the forward paper-trading ladder."
                )
            },
        )
        return

    logger.info("paper-worker starting", extra={"mode": "once" if once else "scheduled"})

    # The datasource must be entered as an async context manager so the
    # LiveDataSource httpx client is built (no-op for FixtureDataSource).
    async with _datasource_lifecycle(get_datasource()) as ds:
        worker = PaperWorker(ds)
        if once:
            await _run_once(worker)
        else:
            await _scheduled_loop(worker)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m polyecho.paper",
        description="PolyEcho paper-trading-worker service (C1)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Run one paper cycle then exit.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(once=args.once))

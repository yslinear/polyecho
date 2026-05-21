"""Entry-point: python -m polyecho.backtest

Starts the backtest job-queue worker (PRD §5.4).

Usage:
    python -m polyecho.backtest               # loop forever, poll every 10 s
    python -m polyecho.backtest --once        # drain ALL pending jobs, then exit

``--once`` keeps polling until the pending queue is fully empty (R12 #5 — it
no longer stops after the first ≤5-job batch), then returns normally (exit 0;
the ``--once`` exit-code contract is preserved).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from polyecho.backtest.jobs import process_pending_jobs
from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _datasource_lifecycle(ds: DataSource) -> AsyncIterator[DataSource]:
    """Enter *ds* as an async context manager when it implements that protocol.

    ``LiveDataSource`` builds its ``httpx.AsyncClient`` in ``__aenter__``;
    calling any data method before that raises ``RuntimeError: LiveDataSource
    must be used as an async context manager``.  The backtest worker
    previously did ``ds = get_datasource()`` and handed ``ds`` straight to
    ``process_pending_jobs`` — that worked for fixture but every live run
    raised the above per-leader and stored an ``"error"`` envelope.

    ``FixtureDataSource`` has no I/O lifecycle (no ``__aenter__`` /
    ``__aexit__``), so a bare ``async with FixtureDataSource()`` would raise
    ``TypeError``.  This adapter enters only when both dunders exist
    (the data-model contract used by
    ``contextlib.AbstractAsyncContextManager.__subclasshook__``), otherwise
    yields *ds* unchanged — preserving fixture behaviour byte-for-byte.
    """
    if isinstance(ds, AbstractAsyncContextManager):
        cm: AbstractAsyncContextManager[object] = ds
        async with cm:
            yield ds
    else:
        yield ds


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
        help=(
            "Drain ALL currently-pending jobs (poll until the queue is empty) "
            "then exit (useful for CLI / one-shots)."
        ),
    )
    args = parser.parse_args()

    logger.info("backtest-worker.starting", extra={"once": args.once})

    # P11 fix: wrap the datasource in its async-CM lifecycle so a live
    # ``LiveDataSource`` has its httpx client built (``__aenter__``) before
    # ``process_pending_jobs`` -> ``_process_job`` -> ``run_triple`` -> engine
    # ever touches ``ds.get_*``.  Without this every live-mode backtest stored
    # a per-leader ``RuntimeError: LiveDataSource must be used as an async
    # context manager`` envelope and the job still mark-completed (job
    # 557eef18-e70b-47fc-bb6e-18519120e72a was the wake-up call).
    async with _datasource_lifecycle(get_datasource()) as datasource:
        await process_pending_jobs(datasource, once=args.once)

    logger.info("backtest-worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())

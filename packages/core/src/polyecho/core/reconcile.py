"""
polyecho.core.reconcile
========================
Per-leader ``last_seen_ts`` reconcile loop (PolyHermes brief §7 #5).

PolyHermes' weakness: a WS gap / downtime silently loses leader trades.
PolyEcho persists a per-leader ``follow_relations.last_seen_ts`` cursor;
on every scheduler tick **and after a restart** it backfills missed
trades from the datasource (``get_user_activity`` filtered to ``> cursor``)
and re-enqueues them through the same dedup'd queue, then advances the
cursor.  Restart-safe and idempotent (the queue/executor dedup on
``(leader, tx_hash)`` so a backfilled trade already processed is a no-op).

Pure / injectable: the cursor read/write and the enqueue callback are
passed in so this is unit-testable with no DB and no network.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from polyecho.core.detection import DetectedTrade, _activity_to_detected
from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource

logger = logging.getLogger(__name__)

#: ``async () -> {leader_address: last_seen_ts|None}`` — the per-leader cursor.
CursorReader = Callable[[], Awaitable[dict[str, datetime | None]]]
#: ``async (leader, new_ts) -> None`` — advance the persisted cursor.
CursorWriter = Callable[[str, datetime], Awaitable[None]]
#: ``async (DetectedTrade) -> None`` — re-enqueue a backfilled trade.
Enqueuer = Callable[[DetectedTrade], Awaitable[None]]


class Reconciler:
    """Backfills WS-gap / post-restart missed trades (brief §7 #5)."""

    def __init__(
        self,
        *,
        cursor_reader: CursorReader,
        cursor_writer: CursorWriter,
        enqueue: Enqueuer,
        datasource: DataSource | None = None,
    ) -> None:
        self._cursor_reader = cursor_reader
        self._cursor_writer = cursor_writer
        self._enqueue = enqueue
        self._ds = datasource if datasource is not None else get_datasource()

    async def reconcile_once(self) -> int:
        """One reconcile pass; return the number of trades backfilled.

        For each followed leader, fetch activity strictly after the
        persisted ``last_seen_ts``, re-enqueue each as a detected trade
        (the queue/executor dedup makes already-processed ones no-ops),
        and advance the cursor to the newest timestamp seen.
        """
        cursors = await self._cursor_reader()
        backfilled = 0
        for leader, cursor in cursors.items():
            # P1-4: isolate each leader.  A datasource error (or any
            # exception) for ONE leader must neither abort the whole pass
            # nor advance any cursor past unprocessed trades — we log it,
            # leave that leader's cursor exactly where it was (so the
            # missed window is retried next tick — restart-safe), and
            # continue with the other leaders.
            try:
                backfilled += await self._reconcile_leader(leader, cursor)
            except Exception as exc:  # noqa: BLE001 - per-leader isolation
                logger.warning(
                    "reconcile_leader_failed_continuing",
                    extra={"leader": leader, "error": str(exc)},
                )
                continue
        return backfilled

    async def _reconcile_leader(
        self, leader: str, cursor: datetime | None
    ) -> int:
        """Backfill one leader; advance its cursor ONLY on full success.

        The cursor is written strictly after every in-window record was
        successfully enqueued, so a failure mid-leader leaves the cursor
        unmoved and the window is safely retried next tick (idempotent —
        the queue/executor dedup on ``(leader, tx_hash)``).
        """
        records = await self._ds.get_user_activity(
            leader, start=cursor, activity_type="TRADE"
        )
        newest = cursor
        backfilled = 0
        for rec in records:
            ts: datetime = getattr(rec, "timestamp", datetime.now(tz=UTC))
            # Strictly-after the cursor (the cursor row itself was seen).
            if cursor is not None and ts <= cursor:
                continue
            detected = _activity_to_detected(rec, leader=leader, source="reconcile")
            if detected is None:
                continue
            await self._enqueue(detected)
            backfilled += 1
            if newest is None or ts > newest:
                newest = ts
        if newest is not None and newest != cursor:
            await self._cursor_writer(leader, newest)
            logger.info(
                "reconcile_cursor_advanced",
                extra={"leader": leader, "last_seen_ts": newest.isoformat()},
            )
        return backfilled

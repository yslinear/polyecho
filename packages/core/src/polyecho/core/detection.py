"""
polyecho.core.detection
========================
Dual-redundant leader-trade detection (PolyHermes brief §3).

Two independent producers feed **one** :class:`asyncio.Queue`:

* **Path A — WS User-Channel listener** (primary, low-latency).  In live
  mode this is the authenticated Polymarket User-Channel WebSocket
  (``CLOB_WSS_USER_URL``); in fixture / test mode it replays the bundled
  ``DataSource`` activity so the whole pipeline is exercisable with no
  network (brief §3 — "use ``get_datasource()`` for replay/tests").
* **Path B — Polygon CTF on-chain log cross-check** (fallback).  In live
  mode this subscribes to Polygon logs and reconstructs the trade from
  USDC / ERC-1155 token-flow deltas (brief §3); in fixture mode it is a
  delayed replay of the same datasource so the N-confirmation guard and
  cross-source dedup are exercised deterministically.

**Idempotency — the cross-source dedup key is ``tx_hash``** (brief §3):

1. an in-proc LRU keyed by ``(leader_address, tx_hash)`` (cheap, first),
2. an in-proc per-tx lock so two producers racing the same tx serialise,
3. the persistent ``leader_trades(leader_address, tx_hash)`` UNIQUE
   constraint (the durable anchor — handled by the consumer/executor).

PolyEcho safety improvement #1 (brief §7): the on-chain cross-check path
carries an **N-confirmation guard** — a path-B event is only emitted once
it has been seen ``confirmations`` times (a stand-in for block-depth /
reorg safety that is fully deterministic under the fixture replay).

No network or DB I/O on import.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource

logger = logging.getLogger(__name__)

#: A detected leader trade pushed onto the queue.  ``source`` is ``"ws"``
#: (path A) or ``"chain"`` (path B); ``tx_hash`` is the cross-source dedup
#: anchor (brief §3).
DetectedTrade = dict[str, Any]


def synthetic_tx_hash(*, leader: str, condition_id: str, ts: int, side: str, size: float) -> str:
    """Deterministic stand-in tx hash for fixture replay (brief §3).

    Live detection carries the real on-chain ``transactionHash``; fixtures
    have none, so we derive a stable key from the trade's natural identity
    so path A and path B agree on the **same** dedup anchor.
    """
    import hashlib

    raw = f"{leader.lower()}|{condition_id}|{ts}|{side}|{size:.6f}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:40]


@dataclass
class _LruSet:
    """Fixed-size insertion-ordered set for the in-proc dedup LRU (brief §3)."""

    capacity: int = 4096
    _data: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def add(self, key: str) -> bool:
        """Add *key*; return ``True`` if it was new, ``False`` if a dup."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._data


class TradeDeduplicator:
    """Cross-source idempotency (brief §3 — layers 1 & 2, ``tx_hash`` key).

    Layer 3 (the ``leader_trades`` UNIQUE constraint) is enforced by the
    persistence path; this object owns the two in-process layers so two
    producers that race the *same* tx never both reach the executor.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._seen = _LruSet(capacity=capacity)
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def key(leader_address: str, tx_hash: str) -> str:
        return f"{leader_address.lower()}|{tx_hash}"

    def lock_for(self, leader_address: str, tx_hash: str) -> asyncio.Lock:
        k = self.key(leader_address, tx_hash)
        lock = self._locks.get(k)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[k] = lock
        return lock

    def claim(self, leader_address: str, tx_hash: str) -> bool:
        """Claim a ``(leader, tx)`` once; ``True`` ⇒ first, ``False`` ⇒ dup."""
        return self._seen.add(self.key(leader_address, tx_hash))

    def seen(self, leader_address: str, tx_hash: str) -> bool:
        return self.key(leader_address, tx_hash) in self._seen


def _activity_to_detected(
    rec: Mapping[str, Any] | Any, *, leader: str, source: str
) -> DetectedTrade | None:
    """Normalise a datasource ``ActivityRecord`` into a detected-trade dict.

    Only ``TRADE`` activity with a side becomes a copy candidate; redeem /
    reward rows are ignored here (the redeem worker owns those).
    """
    activity_type = getattr(rec, "activity_type", None)
    side = getattr(rec, "side", None)
    condition_id = getattr(rec, "condition_id", None)
    if activity_type != "TRADE" or side is None or not condition_id:
        return None
    ts_dt: datetime = getattr(rec, "timestamp", datetime.now(tz=UTC))
    ts = int(ts_dt.timestamp())
    size = float(getattr(rec, "size", 0.0) or 0.0)
    price = getattr(rec, "price", None)
    tx_hash = synthetic_tx_hash(
        leader=leader, condition_id=condition_id, ts=ts, side=side, size=size
    )
    return {
        "leader_address": leader.lower(),
        "tx_hash": tx_hash,
        "trade_id": tx_hash,  # fixtures have no separate trade id
        "condition_id": condition_id,
        "market_id": condition_id,
        "side": side,
        "size": size,
        "price": float(price) if price is not None else None,
        "timestamp": ts,
        "source": source,
        "confirmations": 1,
        "raw": dict(getattr(rec, "raw", {}) or {}),
    }


class UserChannelListener:
    """Path A — primary low-latency producer (brief §3).

    Live mode connects to the authenticated WS User-Channel
    (``CLOB_WSS_USER_URL``).  Fixture / test mode replays the bundled
    ``DataSource`` activity so the full copy pipeline runs with no network
    (brief §3 — the same interface for replay/tests and live).
    """

    source = "ws"

    def __init__(
        self,
        leader_addresses: list[str],
        queue: asyncio.Queue[DetectedTrade],
        *,
        datasource: DataSource | None = None,
        live: bool = False,
    ) -> None:
        self._leaders = [a.lower() for a in leader_addresses]
        self._queue = queue
        self._ds = datasource if datasource is not None else get_datasource()
        self._live = live

    async def run_once(self) -> int:
        """Replay every leader's activity once; return #events enqueued.

        This is the deterministic single-pass used by ``--once`` and the
        fixture tests; the supervised live loop wraps the real WS stream.
        """
        n = 0
        for leader in self._leaders:
            records = await self._ds.get_user_activity(leader, activity_type="TRADE")
            for rec in records:
                detected = _activity_to_detected(rec, leader=leader, source=self.source)
                if detected is None:
                    continue
                await self._queue.put(detected)
                n += 1
        return n

    async def run(self) -> None:
        """Supervised producer loop (live WS or fixture replay).

        In fixture mode this performs one replay pass then idles (the
        supervised loop is restart-safe; reconcile backfills any gap).  The
        live WS subscription is the Phase-4 W2 deliverable behind the same
        interface and only ever exercised with ``POLYECHO_LIVE_TRADING``.
        """
        if not self._live:
            await self.run_once()
            # Idle; the reconcile loop is the durable backfill (brief §7).
            # An Event that is never set parks forever without a busy
            # sleep loop until the supervising TaskGroup cancels us.
            await asyncio.Event().wait()  # pragma: no cover - supervised idle
            return
        # TODO(verify): live authenticated WS User-Channel subscription
        # (CLOB_WSS_USER_URL) — single multiplexed connection, NOT
        # one-per-address (brief §3/§8).  Behind the live lock; never
        # reached by the default-off test path.
        raise NotImplementedError(  # pragma: no cover - live-only path
            "Phase-4 W2: live WS User-Channel subscription "
            "(CLOB_WSS_USER_URL) — fixture replay is the tested path"
        )


class ChainCrossCheckListener:
    """Path B — Polygon CTF on-chain log cross-check (brief §3, §7 #1).

    Live mode subscribes to Polygon logs and reconstructs the trade from
    USDC / ERC-1155 token-flow deltas (brief §3).  Fixture mode replays the
    same datasource as a *delayed* second source so cross-source dedup and
    the **N-confirmation guard** (brief §7 #1) are exercised
    deterministically.  A path-B event is only forwarded once it has been
    observed ``confirmations`` times (reorg-safety stand-in).
    """

    source = "chain"

    def __init__(
        self,
        leader_addresses: list[str],
        queue: asyncio.Queue[DetectedTrade],
        *,
        datasource: DataSource | None = None,
        confirmations: int = 1,
        live: bool = False,
    ) -> None:
        self._leaders = [a.lower() for a in leader_addresses]
        self._queue = queue
        self._ds = datasource if datasource is not None else get_datasource()
        self._confirmations = max(1, confirmations)
        self._live = live
        self._seen_count: dict[str, int] = {}

    async def run_once(self) -> int:
        """Replay once, applying the N-confirmation guard (brief §7 #1)."""
        n = 0
        for leader in self._leaders:
            records = await self._ds.get_user_activity(leader, activity_type="TRADE")
            for rec in records:
                detected = _activity_to_detected(rec, leader=leader, source=self.source)
                if detected is None:
                    continue
                key = f"{detected['leader_address']}|{detected['tx_hash']}"
                self._seen_count[key] = self._seen_count.get(key, 0) + 1
                count = self._seen_count[key]
                if count < self._confirmations:
                    logger.debug(
                        "chain_confirmation_pending",
                        extra={
                            "leader": leader,
                            "tx_hash": detected["tx_hash"],
                            "seen": count,
                            "need": self._confirmations,
                        },
                    )
                    continue
                detected["confirmations"] = count
                await self._queue.put(detected)
                n += 1
        return n

    async def run(self) -> None:
        """Supervised path-B loop (live RPC log scan or fixture replay)."""
        if not self._live:
            await self.run_once()
            await asyncio.Event().wait()  # pragma: no cover - supervised idle
            return
        # TODO(verify): live Polygon log subscription + USDC/ERC-1155
        # token-flow delta reconstruction (brief §3).  Live-only.
        raise NotImplementedError(  # pragma: no cover - live-only path
            "Phase-4 W2: live Polygon CTF on-chain log cross-check "
            "(token-flow delta reconstruction) — fixture replay is tested"
        )

"""
polyecho.core.detection
========================
Dual-redundant leader-trade detection (PolyHermes brief §3).

Two independent producers feed **one** :class:`asyncio.Queue`:

* **Path A — activity-firehose listener** (primary, low-latency).  In live
  mode this consumes the PUBLIC Polymarket activity firehose
  (``ACTIVITY_FIREHOSE_WSS_URL``) — leader trades are public so NO L2 auth
  is needed (C2 roadmap "Design decision (settled)").  In fixture / test
  mode it replays the bundled ``DataSource`` activity so the whole
  pipeline is exercisable with no network (brief §3 — "use
  ``get_datasource()`` for replay/tests").
* **Path B — Polygon CTF on-chain log cross-check** (fallback).  In live
  mode a SINGLE multiplexed ``eth_subscribe("logs")`` connection watches
  CTF / USDC logs and reconstructs the trade from token-flow deltas (NOT
  one WS per address — that is PolyHermes' explicit antipattern); in
  fixture mode it is a delayed replay of the same datasource so the
  N-confirmation guard and cross-source dedup are exercised
  deterministically.

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
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource

logger = logging.getLogger(__name__)

#: Minimum length a raw firehose frame must reach before it is worth a
#: substring scan / JSON parse (C2-P2 cheap pre-filter).  Keepalive echoes
#: and trivial control frames are far shorter than any real trade payload.
_MIN_FRAME_LENGTH: int = 50

#: The activity-firehose subscription frame (C2-P2).  ``trades`` carries
#: leader fills; ``orders_matched`` carries the matched-order side.  Sent
#: verbatim after every (re)connect by :class:`ReconnectingWSClient`.
_FIREHOSE_SUBSCRIBE_FRAME: str = json.dumps(
    {
        "action": "subscribe",
        "subscriptions": [
            {"topic": "activity", "type": "trades"},
            {"topic": "activity", "type": "orders_matched"},
        ],
    },
    separators=(",", ":"),
)

#: ERC-1155 ``TransferSingle(operator,from,to,id,value)`` topic0 (CTF
#: position-token moves).  keccak256 of the event signature.
_TRANSFER_SINGLE_TOPIC: str = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
#: ERC-1155 ``TransferBatch(operator,from,to,ids,values)`` topic0.
_TRANSFER_BATCH_TOPIC: str = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
#: ERC-20 ``Transfer(from,to,value)`` topic0 (USDC collateral moves).
_ERC20_TRANSFER_TOPIC: str = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

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
    #: P1-13: the key evicted by the most recent :meth:`add` (``None`` if
    #: none), so :class:`TradeDeduplicator` can prune the matching per-tx
    #: lock in lock-step and keep ``_locks`` bounded.
    _evicted: str | None = None

    def add(self, key: str) -> bool:
        """Add *key*; return ``True`` if it was new, ``False`` if a dup."""
        self._evicted = None
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self.capacity:
            # P1-13: surface the evicted key so the owning
            # TradeDeduplicator can evict its per-tx lock in lock-step
            # (bounded — same capacity, no unbounded ``_locks`` growth).
            self._evicted = self._data.popitem(last=False)[0]
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
        # C4a (R6): a single instance-level lock that makes the
        # lock_for → claim sequence ATOMIC.  Previously a key could be
        # evicted from ``_seen`` (and its per-tx lock dropped) by an
        # unrelated tx *between* one coroutine's ``lock_for`` and its
        # ``claim`` — letting a second coroutine fetch a *different* Lock
        # object "for the same K" and both think they hold "the lock for
        # K" ⇒ duplicate processing.  All structural mutation of
        # ``_locks``/``_seen`` (lock_for + claim + eviction) now happens
        # under this guard, so claim+lock for one tx is indivisible.
        self._guard = asyncio.Lock()

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

    async def acquire_for(self, leader_address: str, tx_hash: str) -> tuple[asyncio.Lock, bool]:
        """Atomically resolve the per-tx lock AND claim the key (C4a/R6).

        Returns ``(lock, is_new)`` where ``lock`` is **already acquired**
        and ``is_new`` is ``True`` iff this caller is the first to claim
        ``(leader, tx)`` (``False`` ⇒ a duplicate the caller must drop).
        The whole resolve-lock → acquire-lock → claim window runs under
        the instance ``_guard`` so no concurrent eviction can swap the
        per-tx lock identity mid-sequence (the old TOCTOU).  The caller
        MUST ``release()`` the returned lock when done (see
        :meth:`TraderCore.process_detected`).
        """
        async with self._guard:
            lock = self.lock_for(leader_address, tx_hash)
            await lock.acquire()
            is_new = self._claim(leader_address, tx_hash)
        return lock, is_new

    def _claim(self, leader_address: str, tx_hash: str) -> bool:
        """Claim ``(leader, tx)`` once; ``True`` ⇒ first, ``False`` ⇒ dup.

        P1-13: the per-tx :class:`asyncio.Lock` dict used to grow
        unboundedly (``_seen`` is LRU-capped but ``_locks`` was never
        pruned — a memory leak in the long-running loop).  When the LRU
        evicts a key we evict its lock in lock-step so ``_locks`` stays
        bounded by the same capacity as ``_seen``.  Eviction only ever
        runs while holding ``_guard`` (via :meth:`acquire_for`), so a key
        currently being raced can never have its lock dropped out from
        under a concurrent claimer (the C4a invariant).  This is internal:
        callers go through :meth:`acquire_for` for the atomic claim+lock.
        """
        is_new = self._seen.add(self.key(leader_address, tx_hash))
        evicted = self._seen._evicted
        if evicted is not None:
            self._locks.pop(evicted, None)
        return is_new

    def claim(self, leader_address: str, tx_hash: str) -> bool:
        """Back-compat sync claim (kept for existing callers/tests).

        Prefer :meth:`acquire_for` for the atomic claim+lock; this remains
        for code paths that already hold the per-tx lock another way.
        """
        return self._claim(leader_address, tx_hash)

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


def _coerce_float(value: Any) -> float | None:
    """Coerce a polymorphic firehose ``str | int | float`` field to float.

    The activity firehose is loosely typed: ``size`` / ``price`` /
    ``timestamp`` arrive as either JSON numbers OR numeric strings
    depending on the event subtype.  Returns ``None`` for missing /
    un-parseable values so the caller decides whether the field is
    mandatory.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — never a number here
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _firehose_leader_address(payload: Mapping[str, Any]) -> str | None:
    """Extract the actor wallet from a firehose payload, lowercased.

    The firehose puts the trading wallet under ``proxyWallet`` for trade
    events and under a nested ``trader.address`` for orders-matched
    events; either may be present.  Returns ``None`` when neither is.
    """
    proxy = payload.get("proxyWallet")
    if isinstance(proxy, str) and proxy:
        return proxy.lower()
    trader = payload.get("trader")
    if isinstance(trader, Mapping):
        addr = trader.get("address")
        if isinstance(addr, str) and addr:
            return addr.lower()
    return None


def _firehose_to_detected(payload: Mapping[str, Any], *, leader: str) -> DetectedTrade | None:
    """Normalise one PUBLIC activity-firehose payload into a detected trade.

    Mirrors :func:`_activity_to_detected` (same dict shape, ``source="ws"``)
    but reads the firehose JSON wire-format instead of an ``ActivityRecord``.
    Only a payload with a BUY/SELL side and a condition id becomes a copy
    candidate; anything else returns ``None`` and is dropped by the caller.

    ``size`` / ``price`` / ``timestamp`` are coerced via :func:`_coerce_float`
    because the firehose mixes JSON numbers and numeric strings.  When the
    payload carries a real ``transactionHash`` it is used verbatim as the
    cross-source dedup anchor (so the chain path agrees); otherwise a
    deterministic :func:`synthetic_tx_hash` stands in.
    """
    side_raw = payload.get("side")
    side = side_raw.upper() if isinstance(side_raw, str) else None
    if side not in ("BUY", "SELL"):
        return None
    condition_id = payload.get("conditionId") or payload.get("condition_id")
    if not condition_id or not isinstance(condition_id, str):
        return None

    ts_val = _coerce_float(payload.get("timestamp"))
    ts = int(ts_val) if ts_val is not None else int(datetime.now(tz=UTC).timestamp())
    size = _coerce_float(payload.get("size")) or 0.0
    price = _coerce_float(payload.get("price"))

    tx_raw = payload.get("transactionHash") or payload.get("transaction_hash")
    tx_hash = (
        tx_raw
        if isinstance(tx_raw, str) and tx_raw
        else synthetic_tx_hash(
            leader=leader, condition_id=condition_id, ts=ts, side=side, size=size
        )
    )
    market_id = payload.get("market") or payload.get("market_id") or condition_id
    return {
        "leader_address": leader.lower(),
        "tx_hash": tx_hash,
        "trade_id": tx_hash,
        "condition_id": condition_id,
        "market_id": market_id if isinstance(market_id, str) else condition_id,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": ts,
        "source": "ws",
        "confirmations": 1,
        "raw": dict(payload),
    }


class UserChannelListener:
    """Path A — primary low-latency producer (C2-P2).

    Live mode consumes the PUBLIC activity firehose
    (``ACTIVITY_FIREHOSE_WSS_URL``) over the reusable
    :class:`~polyecho.core.ws_client.ReconnectingWSClient`.  Leader trades
    are public broadcasts, so there is NO L2 auth — this listener is the
    repurposed C2 design (the original ``CLOB_WSS_USER_URL`` + auth
    assumption is superseded; the authenticated own-fill channel is the
    deferred, optional C2-P5).  Fixture / test mode replays the bundled
    ``DataSource`` activity so the full copy pipeline runs with no network.

    The live path stays default-OFF: it is reached ONLY when
    ``live=True`` (``datasource == "live"``).  The fixture/test path is
    unchanged.

    :param connect: injection seam forwarded to the WS client (tests pass
        an in-memory transport so the live path is exercised network-free).
    """

    source = "ws"

    def __init__(
        self,
        leader_addresses: list[str],
        queue: asyncio.Queue[DetectedTrade],
        *,
        datasource: DataSource | None = None,
        live: bool = False,
        connect: Any | None = None,
    ) -> None:
        self._leaders = [a.lower() for a in leader_addresses]
        #: A frozenset of the monitored leaders for the O(1) pre-filter.
        self._leader_set: frozenset[str] = frozenset(self._leaders)
        self._queue = queue
        self._ds = datasource if datasource is not None else get_datasource()
        self._live = live
        self._connect = connect

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

    def _matched_leader(self, raw: str) -> str | None:
        """Cheap substring pre-filter: which monitored leader, if any?

        Runs BEFORE any JSON parse (C2-P2).  Frames shorter than
        :data:`_MIN_FRAME_LENGTH` (keepalive echoes, control frames) and
        frames that do not mention any monitored leader address are
        rejected without paying the JSON-decode cost.  A substring hit is
        only a candidate — :meth:`_on_firehose_message` still parses and
        verifies the actual ``proxyWallet`` / ``trader.address`` field, so
        a coincidental substring never produces a false detection.
        """
        if len(raw) < _MIN_FRAME_LENGTH:
            return None
        lowered = raw.lower()
        for leader in self._leader_set:
            if leader in lowered:
                return leader
        return None

    async def _on_firehose_message(self, message: str | bytes) -> None:
        """Handle one inbound firehose frame: pre-filter, parse, enqueue.

        Never raises on a malformed / non-leader / non-trade frame — the
        firehose is a noisy public broadcast and a single bad frame must
        not tear down the supervised WS loop.  Only :class:`asyncio.Queue`
        back-pressure (an ``await``) and cancellation propagate.
        """
        raw = message.decode("utf-8", "ignore") if isinstance(message, bytes) else message
        if self._matched_leader(raw) is None:
            return  # pre-filter miss: not for any monitored leader
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("firehose_unparseable_frame")
            return
        # The firehose may wrap the trade dict under a "payload" key or
        # send it at the top level; tolerate both.
        if isinstance(decoded, Mapping):
            payloads: list[Any] = [decoded.get("payload", decoded)]
        elif isinstance(decoded, list):
            payloads = list(decoded)
        else:
            return
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            leader = _firehose_leader_address(payload)
            # Verified-field check: the substring may have hit a coincidental
            # field, so confirm the ACTUAL actor wallet is a monitored leader.
            if leader is None or leader not in self._leader_set:
                continue
            detected = _firehose_to_detected(payload, leader=leader)
            if detected is None:
                continue
            await self._queue.put(detected)

    async def run(self) -> None:
        """Supervised producer loop (live activity firehose or fixture replay).

        In fixture mode this performs one replay pass then idles (the
        supervised loop is restart-safe; reconcile backfills any gap).  In
        live mode it drives a :class:`ReconnectingWSClient` against the
        public activity firehose — connect / backoff / ping / watchdog are
        all owned by that client; this listener supplies only the
        subscription frame and the message handler.

        TaskGroup-safe: ``ReconnectingWSClient.run`` re-raises ONLY
        :class:`asyncio.CancelledError`, so this coroutine is safe to
        launch as a long-lived sibling (C2-P4 wiring).
        """
        if not self._live:
            await self.run_once()
            # Idle; the reconcile loop is the durable backfill (brief §7).
            # An Event that is never set parks forever without a busy
            # sleep loop until the supervising TaskGroup cancels us.
            await asyncio.Event().wait()  # pragma: no cover - supervised idle
            return
        # Live: the public activity firehose — no auth, leader trades are
        # public (C2 roadmap "Design decision (settled)").
        from polyecho.core.ws_client import ReconnectingWSClient
        from polyecho.shared.constants import ACTIVITY_FIREHOSE_WSS_URL

        client = ReconnectingWSClient(
            ACTIVITY_FIREHOSE_WSS_URL,
            on_message=self._on_firehose_message,
            subscribe_frames=[_FIREHOSE_SUBSCRIBE_FRAME],
            connect=self._connect,
        )
        logger.info(
            "user_channel_listener_live_start",
            extra={"url": ACTIVITY_FIREHOSE_WSS_URL, "leaders": len(self._leaders)},
        )
        await client.run()


def _topic_address(topic: str) -> str | None:
    """Extract a lowercased ``0x``-address from a 32-byte indexed log topic.

    An ``address`` indexed in a log topic is left-zero-padded to 32 bytes;
    the address is the trailing 20 bytes.  Returns ``None`` if *topic* is
    not a well-formed 32-byte hex string.
    """
    if not isinstance(topic, str) or not topic.startswith("0x"):
        return None
    body = topic[2:]
    if len(body) != 64:
        return None
    return "0x" + body[-40:].lower()


class ChainCrossCheckListener:
    """Path B — Polygon CTF on-chain log cross-check (C2-P3, brief §3/§7 #1).

    Live mode opens a SINGLE multiplexed Polygon ``eth_subscribe("logs")``
    connection (NOT one WS per address — that is PolyHermes' explicit
    antipattern) over the reusable
    :class:`~polyecho.core.ws_client.ReconnectingWSClient`.  Inbound CTF
    ERC-1155 and USDC ERC-20 ``Transfer`` logs are filtered by the
    monitored leader set (an indexed ``from`` / ``to`` topic mentioning a
    leader), a trade is reconstructed from the token-flow direction, and
    the **N-confirmation guard** is applied BEFORE enqueue: a path-B event
    is only forwarded once its ``(leader, tx)`` has been observed
    ``confirmations`` times (a reorg-safety stand-in).

    Fixture mode replays the same datasource as a *delayed* second source
    so cross-source dedup and the N-confirmation guard are exercised
    deterministically.

    .. note:: **Log-decoding depth (honest scope flag).**  This
       reconstructs the trade *direction* (BUY/SELL) and the *leader* and
       *tx_hash* from raw ERC-1155 / ERC-20 ``Transfer`` topics — the
       fields that are unambiguously derivable from a single log without
       extra RPC.  It does NOT decode ``size`` / ``price`` from the log
       ``data`` field, nor resolve the ERC-1155 token id back to its
       ``condition_id`` (that needs a CTF position-id ↔ condition map and
       a per-tx receipt fetch).  Path B is a *cross-check / confirmation*
       source, not the price-bearing path: the firehose (path A) carries
       authoritative size/price, and ``TradeDeduplicator`` keys on
       ``(leader, tx_hash)`` source-agnostically so a path-B event with a
       partial body still correctly dedupes against the path-A event.
       Full ``data``-field + token-id decoding is deliberately deferred
       rather than shipped subtly wrong on a money path.
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
        connect: Any | None = None,
    ) -> None:
        self._leaders = [a.lower() for a in leader_addresses]
        #: Padded-topic forms of each leader address for the log filter and
        #: the O(1) topic-membership check.
        self._leader_set: frozenset[str] = frozenset(self._leaders)
        self._queue = queue
        self._ds = datasource if datasource is not None else get_datasource()
        self._confirmations = max(1, confirmations)
        self._live = live
        self._connect = connect
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
                if await self._gate_and_enqueue(detected):
                    n += 1
        return n

    async def _gate_and_enqueue(self, detected: DetectedTrade) -> bool:
        """Apply the N-confirmation guard; enqueue iff confirmed.

        Returns ``True`` iff *detected* was forwarded to the queue.  Shared
        verbatim by the fixture replay and the live log path so the
        reorg-safety gate is identical on both (brief §7 #1).
        """
        key = f"{detected['leader_address']}|{detected['tx_hash']}"
        self._seen_count[key] = self._seen_count.get(key, 0) + 1
        count = self._seen_count[key]
        if count < self._confirmations:
            logger.debug(
                "chain_confirmation_pending",
                extra={
                    "leader": detected["leader_address"],
                    "tx_hash": detected["tx_hash"],
                    "seen": count,
                    "need": self._confirmations,
                },
            )
            return False
        detected["confirmations"] = count
        await self._queue.put(detected)
        return True

    def _log_to_detected(self, log: Mapping[str, Any]) -> DetectedTrade | None:
        """Reconstruct a partial detected-trade from one Polygon log.

        Filters to CTF ERC-1155 / USDC ERC-20 ``Transfer`` events whose
        indexed ``from`` / ``to`` topic is a monitored leader, then derives
        the trade direction from the token-flow:

        * a position-token (ERC-1155) leaving the leader, or USDC arriving
          to the leader  ⇒ ``SELL``;
        * a position-token arriving to the leader, or USDC leaving the
          leader  ⇒ ``BUY``.

        Returns ``None`` for any non-matching / malformed log.  See the
        class docstring for the deliberate ``size`` / ``price`` /
        ``condition_id`` decoding-depth limit.
        """
        topics = log.get("topics")
        if not isinstance(topics, list) or not topics:
            return None
        topic0 = topics[0]
        is_erc1155 = topic0 in (_TRANSFER_SINGLE_TOPIC, _TRANSFER_BATCH_TOPIC)
        is_erc20 = topic0 == _ERC20_TRANSFER_TOPIC
        if not (is_erc1155 or is_erc20):
            return None
        # ERC-1155 Transfer*: topics = [sig, operator, from, to].
        # ERC-20  Transfer:    topics = [sig, from, to].
        if is_erc1155 and len(topics) >= 4:
            from_addr = _topic_address(topics[2])
            to_addr = _topic_address(topics[3])
        elif is_erc20 and len(topics) >= 3:
            from_addr = _topic_address(topics[1])
            to_addr = _topic_address(topics[2])
        else:
            return None

        leader_is_sender = from_addr in self._leader_set
        leader_is_recipient = to_addr in self._leader_set
        if not (leader_is_sender or leader_is_recipient):
            return None
        leader = (from_addr if leader_is_sender else to_addr) or ""

        # Token-flow direction: position-token OUT or USDC IN ⇒ SELL.
        if is_erc1155:
            side = "SELL" if leader_is_sender else "BUY"
        else:  # USDC: collateral IN to the leader ⇒ a SELL settled.
            side = "SELL" if leader_is_recipient else "BUY"

        tx_hash = log.get("transactionHash")
        if not isinstance(tx_hash, str) or not tx_hash:
            return None
        block_raw = log.get("blockNumber")
        # ts is not on the log; the consumer/executor stamps the real time.
        return {
            "leader_address": leader,
            "tx_hash": tx_hash,
            "trade_id": tx_hash,
            # condition_id is NOT derivable from a bare log (see docstring);
            # tx_hash is the dedup anchor so a partial body still dedupes.
            "condition_id": None,
            "market_id": None,
            "side": side,
            "size": 0.0,
            "price": None,
            "timestamp": int(datetime.now(tz=UTC).timestamp()),
            "source": self.source,
            "confirmations": 1,
            "raw": {
                "blockNumber": block_raw,
                "topics": list(topics),
                "address": log.get("address"),
                "data": log.get("data"),
            },
        }

    async def _on_log_message(self, message: str | bytes) -> None:
        """Handle one ``eth_subscription`` log notification frame.

        Never raises on a malformed / non-matching frame — a single bad
        log must not tear down the supervised connection.  Confirmed
        events pass through the SAME :meth:`_gate_and_enqueue` guard as the
        fixture path.
        """
        raw = message.decode("utf-8", "ignore") if isinstance(message, bytes) else message
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("chain_log_unparseable_frame")
            return
        if not isinstance(decoded, Mapping):
            return
        # eth_subscribe pushes {"method":"eth_subscription","params":
        # {"subscription":id,"result":<log>}}; tolerate a bare log too.
        params = decoded.get("params")
        if isinstance(params, Mapping):
            log = params.get("result")
        else:
            log = decoded.get("result", decoded)
        logs = log if isinstance(log, list) else [log]
        for entry in logs:
            if not isinstance(entry, Mapping):
                continue
            detected = self._log_to_detected(entry)
            if detected is None:
                continue
            await self._gate_and_enqueue(detected)

    async def _subscribe_logs(self, client: Any) -> None:
        """Send ONE multiplexed ``eth_subscribe("logs")`` after every open.

        A single subscription with an ``address`` (CTF + USDC) + ``topics``
        OR-filter (the indexed ``from`` / ``to`` slots constrained to the
        leader set) — NOT one connection per leader.  Fired by the WS
        client's ``on_open`` hook so it re-subscribes on every reconnect.
        """
        from polyecho.shared.constants import CTF_ADDRESS, PUSD_ADDRESS

        # Pad each leader address to a 32-byte topic for the indexed-arg
        # filter; the same set is matched in both the from and to slots.
        leader_topics = ["0x" + "0" * 24 + a[2:] for a in self._leaders]
        log_filter: dict[str, Any] = {
            "address": [CTF_ADDRESS, PUSD_ADDRESS],
            "topics": [
                [_TRANSFER_SINGLE_TOPIC, _TRANSFER_BATCH_TOPIC, _ERC20_TRANSFER_TOPIC],
            ],
        }
        if leader_topics:
            # ERC-20 from/to live in topics[1]/[2]; an ERC-1155 from/to in
            # [2]/[3].  A single filter cannot cover both slot layouts, so
            # we subscribe broad on event-sig + contract and do the precise
            # leader match in _log_to_detected.  Recorded for observability.
            log_filter["_leader_topics"] = leader_topics
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["logs", {k: v for k, v in log_filter.items() if k != "_leader_topics"}],
            },
            separators=(",", ":"),
        )
        await client.send(request)

    async def run(self) -> None:
        """Supervised path-B loop (live multiplexed log scan or fixture replay).

        In live mode this drives a :class:`ReconnectingWSClient` against a
        Polygon JSON-RPC WS endpoint with ONE multiplexed
        ``eth_subscribe("logs")`` subscription.  Connect / backoff / ping /
        watchdog are owned by the WS client; this listener supplies the
        subscription and the log handler.  Confirmed events pass the
        N-confirmation guard before enqueue.

        TaskGroup-safe: ``ReconnectingWSClient.run`` re-raises ONLY
        :class:`asyncio.CancelledError`.
        """
        if not self._live:
            await self.run_once()
            await asyncio.Event().wait()  # pragma: no cover - supervised idle
            return
        from polyecho.core.ws_client import ReconnectingWSClient
        from polyecho.shared.config import get_settings

        # The first configured Polygon RPC, swapped http(s) -> ws(s): a WS
        # endpoint is required for eth_subscribe (HTTP RPC cannot push).
        rpc_urls = get_settings().polygon_rpc_urls
        rpc_url = rpc_urls[0] if rpc_urls else ""
        ws_url = rpc_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        client = ReconnectingWSClient(
            ws_url,
            on_message=self._on_log_message,
            on_open=self._subscribe_logs,
            connect=self._connect,
        )
        logger.info(
            "chain_cross_check_listener_live_start",
            extra={"url": ws_url, "leaders": len(self._leaders)},
        )
        await client.run()

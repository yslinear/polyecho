"""Tests for the C2-P2 / C2-P3 live detection listeners.

Network-free: both live ``run()`` paths are driven through an injected
in-memory fake WS transport (the same ``connect`` seam
:class:`~polyecho.core.ws_client.ReconnectingWSClient` exposes).  Covered:

* C2-P2 — ``UserChannelListener`` live mode: the activity-firehose
  substring pre-filter (a non-leader / short frame is dropped before any
  JSON parse), and a matched leader trade is normalised into a correct
  ``DetectedTrade`` (``source="ws"``, polymorphic string|number coercion).
* C2-P3 — ``ChainCrossCheckListener`` live mode: a multiplexed
  ``eth_subscribe("logs")`` is sent on open, an ERC-1155 / USDC log is
  filtered by the leader set + direction-reconstructed, and the
  N-confirmation guard is applied before enqueue.
* Both live loops re-raise ONLY ``CancelledError`` (TaskGroup-safe).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from polyecho.core.detection import (
    ChainCrossCheckListener,
    DetectedTrade,
    UserChannelListener,
    _coerce_float,
    _firehose_to_detected,
    _topic_address,
)

# asyncio_mode = "auto" (pyproject) auto-marks the async tests; no
# module-level pytest.mark.asyncio (it would warn on the sync helpers).

LEADER = "0xabc0000000000000000000000000000000000001"
OTHER = "0xdef0000000000000000000000000000000000002"

# ERC-1155 / ERC-20 topic0 signatures mirrored from detection.py.
_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
_ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _pad_topic(addr: str) -> str:
    """Left-zero-pad an address into a 32-byte indexed-arg log topic."""
    return "0x" + "0" * 24 + addr[2:]


class FakeWS:
    """Minimal in-memory fake of a ``websockets`` connection.

    Yields the queued ``inbound`` frames once, records every sent frame in
    :attr:`sent`, then ends the stream cleanly (close code 1000) so the
    supervised ``ReconnectingWSClient`` loop exits on its own.
    """

    def __init__(self, inbound: list[str] | None = None) -> None:
        self._inbound = list(inbound or [])
        self.sent: list[str | bytes] = []
        self.close_code: int | None = None

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        if self._inbound:
            return self._inbound.pop(0)
        if self.close_code is None:
            self.close_code = 1000
        raise StopAsyncIteration

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        if self.close_code is None:
            self.close_code = code


def _connector(session: FakeWS) -> Any:
    """Build an injectable ``connect`` handing out *session* once."""
    calls: list[dict[str, Any]] = []

    async def _connect(url: str, **kwargs: Any) -> FakeWS:
        calls.append({"url": url, "kwargs": kwargs})
        if len(calls) == 1:
            return session
        # Any reconnect: a session that ends cleanly so the loop stops.
        return FakeWS(inbound=[])

    _connect.calls = calls  # type: ignore[attr-defined]
    return _connect


# -- pure helpers ------------------------------------------------------


def test_coerce_float_handles_polymorphic_inputs() -> None:
    assert _coerce_float("12.5") == 12.5
    assert _coerce_float(7) == 7.0
    assert _coerce_float(3.0) == 3.0
    assert _coerce_float(None) is None
    assert _coerce_float("") is None
    assert _coerce_float("not-a-number") is None
    assert _coerce_float(True) is None  # bool is not a numeric field


def test_topic_address_extracts_trailing_20_bytes() -> None:
    assert _topic_address(_pad_topic(LEADER)) == LEADER
    assert _topic_address("0xdeadbeef") is None  # wrong length
    assert _topic_address("not-hex") is None


def test_firehose_to_detected_normalizes_string_and_number_fields() -> None:
    # size/price/timestamp arrive as STRINGS here; coercion must apply.
    payload = {
        "proxyWallet": LEADER,
        "side": "buy",
        "conditionId": "0xcond",
        "size": "100.0",
        "price": "0.42",
        "timestamp": "1716400000",
        "transactionHash": "0xtxreal",
    }
    detected = _firehose_to_detected(payload, leader=LEADER)
    assert detected is not None
    assert detected["leader_address"] == LEADER
    assert detected["side"] == "BUY"
    assert detected["size"] == 100.0
    assert detected["price"] == 0.42
    assert detected["timestamp"] == 1716400000
    assert detected["tx_hash"] == "0xtxreal"  # real hash used verbatim
    assert detected["source"] == "ws"
    assert detected["condition_id"] == "0xcond"


def test_firehose_to_detected_rejects_non_trade() -> None:
    # No BUY/SELL side ⇒ not a copy candidate.
    assert _firehose_to_detected({"proxyWallet": LEADER, "side": "X"}, leader=LEADER) is None
    # No condition id ⇒ dropped.
    assert _firehose_to_detected({"proxyWallet": LEADER, "side": "BUY"}, leader=LEADER) is None


# -- C2-P2: UserChannelListener live activity firehose -----------------


async def test_firehose_listener_emits_detected_for_leader_trade() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    frame = json.dumps(
        {
            "topic": "activity",
            "type": "trades",
            "payload": {
                "proxyWallet": LEADER,
                "side": "SELL",
                "conditionId": "0xcondition",
                "size": 50,
                "price": 0.6,
                "timestamp": 1716400000,
                "transactionHash": "0xrealtx",
            },
        }
    )
    session = FakeWS(inbound=[frame])
    listener = UserChannelListener([LEADER], q, live=True, connect=_connector(session))

    await asyncio.wait_for(listener.run(), timeout=2.0)

    assert q.qsize() == 1
    detected = q.get_nowait()
    assert detected["leader_address"] == LEADER
    assert detected["side"] == "SELL"
    assert detected["source"] == "ws"
    assert detected["tx_hash"] == "0xrealtx"
    # The subscribe frame was sent on open.
    assert any("subscribe" in str(m) for m in session.sent)
    sub = json.loads(next(m for m in session.sent if "subscribe" in str(m)))
    assert sub["action"] == "subscribe"
    assert {"topic": "activity", "type": "trades"} in sub["subscriptions"]


async def test_firehose_prefilter_drops_non_leader_and_short_frames() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    # A real trade frame, but for a DIFFERENT wallet: dropped by the
    # substring pre-filter / the verified-field check.
    other_frame = json.dumps(
        {
            "payload": {
                "proxyWallet": OTHER,
                "side": "BUY",
                "conditionId": "0xc",
                "size": 10,
                "price": 0.5,
                "timestamp": 1716400000,
            }
        }
    )
    short_frame = "ping"  # < 50 chars: dropped before any JSON parse
    session = FakeWS(inbound=[other_frame, short_frame])
    listener = UserChannelListener([LEADER], q, live=True, connect=_connector(session))

    await asyncio.wait_for(listener.run(), timeout=2.0)

    assert q.empty()  # neither frame produced a detection


async def test_firehose_listener_survives_malformed_frame() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    # A long-enough non-JSON frame mentioning the leader: must not crash
    # the supervised loop, just be dropped.
    junk = "garbage-" + LEADER + "-" + "x" * 60
    good = json.dumps(
        {
            "payload": {
                "proxyWallet": LEADER,
                "side": "BUY",
                "conditionId": "0xc",
                "size": 1,
                "price": 0.5,
                "timestamp": 1716400000,
            }
        }
    )
    session = FakeWS(inbound=[junk, good])
    listener = UserChannelListener([LEADER], q, live=True, connect=_connector(session))

    await asyncio.wait_for(listener.run(), timeout=2.0)

    assert q.qsize() == 1  # the junk frame was dropped, the good one kept


async def test_firehose_listener_is_cancel_safe() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()

    # A connect that never yields and never closes ⇒ run() parks.
    class _ParkedWS(FakeWS):
        async def __anext__(self) -> str:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    async def _connect(url: str, **kwargs: Any) -> Any:
        return _ParkedWS(inbound=[])

    listener = UserChannelListener([LEADER], q, live=True, connect=_connect)
    task = asyncio.ensure_future(listener.run())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- C2-P3: ChainCrossCheckListener live multiplexed logs --------------


def _log_notification(*, from_addr: str, to_addr: str, topic0: str, tx: str) -> str:
    """Build one eth_subscribe log-notification frame.

    ERC-1155 ``Transfer*`` has topics ``[sig, operator, from, to]``; the
    ERC-20 ``Transfer`` has ``[sig, from, to]`` — this helper builds the
    layout that matches *topic0* so the decoder reads the right slots.
    """
    if topic0 == _ERC20_TRANSFER:
        topics = [topic0, _pad_topic(from_addr), _pad_topic(to_addr)]
    else:  # ERC-1155 Transfer*: an extra leading operator slot.
        topics = [
            topic0,
            _pad_topic("0x" + "0" * 39 + "9"),  # operator
            _pad_topic(from_addr),
            _pad_topic(to_addr),
        ]
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {
                "subscription": "0xsub",
                "result": {
                    "topics": topics,
                    "transactionHash": tx,
                    "blockNumber": "0x1",
                    "address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
                    "data": "0x",
                },
            },
        }
    )


async def test_chain_listener_subscribes_logs_once_on_open() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    session = FakeWS(inbound=[])
    listener = ChainCrossCheckListener([LEADER], q, live=True, connect=_connector(session))
    await asyncio.wait_for(listener.run(), timeout=2.0)

    # Exactly one eth_subscribe("logs") was sent (multiplexed, not per-addr).
    subs = [m for m in session.sent if "eth_subscribe" in str(m)]
    assert len(subs) == 1
    req = json.loads(subs[0])
    assert req["method"] == "eth_subscribe"
    assert req["params"][0] == "logs"


async def test_chain_listener_applies_n_confirmation_guard() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    # The SAME tx log twice: with confirmations=2 the first sighting is
    # held back, the second confirms and enqueues exactly once.
    log = _log_notification(
        from_addr=LEADER,
        to_addr=OTHER,
        topic0=_TRANSFER_SINGLE,
        tx="0xchaintx",
    )
    session = FakeWS(inbound=[log, log])
    listener = ChainCrossCheckListener(
        [LEADER], q, live=True, confirmations=2, connect=_connector(session)
    )
    await asyncio.wait_for(listener.run(), timeout=2.0)

    assert q.qsize() == 1  # gated: only the confirmed (2nd) sighting emitted
    detected = q.get_nowait()
    assert detected["leader_address"] == LEADER
    assert detected["tx_hash"] == "0xchaintx"
    assert detected["source"] == "chain"
    assert detected["confirmations"] == 2
    # ERC-1155 token leaving the leader ⇒ SELL.
    assert detected["side"] == "SELL"


async def test_chain_listener_drops_non_leader_logs() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    # A transfer between two non-monitored wallets: filtered out.
    log = _log_notification(
        from_addr=OTHER,
        to_addr="0x" + "0" * 39 + "8",
        topic0=_ERC20_TRANSFER,
        tx="0xtx",
    )
    session = FakeWS(inbound=[log])
    listener = ChainCrossCheckListener(
        [LEADER], q, live=True, confirmations=1, connect=_connector(session)
    )
    await asyncio.wait_for(listener.run(), timeout=2.0)
    assert q.empty()


async def test_chain_listener_usdc_inflow_is_sell() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    # USDC arriving TO the leader ⇒ a SELL settled.
    log = _log_notification(
        from_addr=OTHER,
        to_addr=LEADER,
        topic0=_ERC20_TRANSFER,
        tx="0xusdctx",
    )
    session = FakeWS(inbound=[log])
    listener = ChainCrossCheckListener(
        [LEADER], q, live=True, confirmations=1, connect=_connector(session)
    )
    await asyncio.wait_for(listener.run(), timeout=2.0)
    assert q.qsize() == 1
    assert q.get_nowait()["side"] == "SELL"


async def test_chain_listener_is_cancel_safe() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()

    class _ParkedWS(FakeWS):
        async def __anext__(self) -> str:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    async def _connect(url: str, **kwargs: Any) -> Any:
        return _ParkedWS(inbound=[])

    listener = ChainCrossCheckListener([LEADER], q, live=True, connect=_connect)
    task = asyncio.ensure_future(listener.run())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

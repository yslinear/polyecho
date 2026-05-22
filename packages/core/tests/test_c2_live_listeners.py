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


# -- C2-P4: TaskGroup wiring + the queue-drain consumer ----------------
#
# These exercise the C2-P4 wiring on ``TraderCore`` itself: the live
# listeners + the drain consumer attach to the supervised TaskGroup ONLY
# when ``datasource == "live"``; fixture mode never starts them.


def _live_core(*, follows: Any | None = None) -> Any:
    """A ``TraderCore`` whose engine reports ``datasource == "live"``.

    ``_settings`` is read once at construction and only ``datasource`` /
    ``auto_redeem`` are consulted on the supervised path, so swapping in a
    tiny stub flips :pyattr:`TraderCore._is_live` without standing up the
    full live config (which would demand real RPC URLs etc.).
    """
    import types as _types

    from polyecho.core.trader import TraderCore

    core = TraderCore(follows=follows or [])
    # ``_settings`` is a frozen pydantic model; a SimpleNamespace with the
    # two fields the supervised path consults is sufficient and avoids
    # standing up the full live config.
    core._settings = _types.SimpleNamespace(  # type: ignore[assignment]
        datasource="live", auto_redeem=False
    )
    return core


def _fixture_core(*, follows: Any | None = None) -> Any:
    """A default fixture-mode ``TraderCore`` (``datasource == "fixture"``)."""
    from polyecho.core.trader import TraderCore

    return TraderCore(follows=follows or [])


def test_is_live_flag_tracks_datasource() -> None:
    assert _fixture_core()._is_live is False
    assert _live_core()._is_live is True


def test_build_live_listeners_constructs_with_live_flag() -> None:
    # Live: both listeners are built with live=True.
    ws, chain = _live_core()._build_live_listeners()
    assert ws._live is True
    assert chain._live is True
    # Fixture: byte-identical to today — both built with live=False.
    ws2, chain2 = _fixture_core()._build_live_listeners()
    assert ws2._live is False
    assert chain2._live is False


async def test_drain_consumer_routes_detected_trade_to_process_detected() -> None:
    """A ``DetectedTrade`` put on ``_queue`` reaches ``process_detected``."""
    core = _live_core()
    seen: list[DetectedTrade] = []

    async def _capture(trade: DetectedTrade) -> None:
        seen.append(trade)

    core.process_detected = _capture

    task = asyncio.ensure_future(core._drain_consumer())
    trade: DetectedTrade = {"leader_address": LEADER, "tx_hash": "0xqueued"}
    await core._queue.put(trade)
    await asyncio.wait_for(core._queue.join(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [trade]


async def test_drain_consumer_counts_errors_and_keeps_draining() -> None:
    """A bad trade is counted, logged, and never aborts the drain loop."""
    core = _live_core()
    calls = 0

    async def _flaky(_trade: DetectedTrade) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")

    core.process_detected = _flaky

    task = asyncio.ensure_future(core._drain_consumer())
    await core._queue.put({"tx_hash": "0xbad"})
    await core._queue.put({"tx_hash": "0xgood"})
    await asyncio.wait_for(core._queue.join(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 2  # the loop survived the first trade's error
    assert core.stats.errors == 1


async def test_drain_consumer_is_cancel_safe() -> None:
    """The drain consumer re-raises ONLY CancelledError (TaskGroup-safe)."""
    core = _live_core()
    task = asyncio.ensure_future(core._drain_consumer())
    await asyncio.sleep(0.02)  # park on an empty queue
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_until_started(
    started: list[str], expected: set[str], *, timeout: float = 3.0
) -> None:
    """Poll until the started-task set matches ``expected``.

    ``core.run()`` does a ``run_once`` DB pass before it enters its
    TaskGroup; that pass is not instant on a cold CI runner, so a fixed
    sleep races (the TaskGroup tasks have not started yet). Poll instead;
    on timeout the caller's assert still reports the real diff.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if set(started) == expected:
            return
        await asyncio.sleep(0.01)


async def test_run_live_starts_listeners_and_drain_in_taskgroup() -> None:
    """LIVE: ``run`` attaches ws + chain + the drain consumer as tasks."""
    core = _live_core()
    started: list[str] = []

    async def _never_exit(label: str) -> None:
        started.append(label)
        await asyncio.Event().wait()

    # Stub the three live coroutines so we can observe they were started
    # without driving real WS streams; the drain consumer is replaced too.
    class _StubWS:
        async def run(self) -> None:
            await _never_exit("ws")

    class _StubChain:
        async def run(self) -> None:
            await _never_exit("chain")

    core._build_live_listeners = lambda: (_StubWS(), _StubChain())
    core._drain_consumer = lambda: _never_exit("drain")
    # Keep the always-on siblings inert so the TaskGroup just parks.
    core._integration_probe_loop = lambda: _never_exit("probe")
    core._park = lambda: _never_exit("park")

    task = asyncio.ensure_future(core.run())
    await _wait_until_started(started, {"ws", "chain", "drain", "probe", "park"})
    assert set(started) == {"ws", "chain", "drain", "probe", "park"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_fixture_does_not_start_live_listeners() -> None:
    """FIXTURE: ``run`` starts NO live listeners / drain (byte-identical)."""
    core = _fixture_core()
    started: list[str] = []

    async def _never_exit(label: str) -> None:
        started.append(label)
        await asyncio.Event().wait()

    def _fail_build() -> Any:  # must never be called in fixture mode
        raise AssertionError("live listeners must not be built in fixture mode")

    core._build_live_listeners = _fail_build
    core._drain_consumer = lambda: _never_exit("drain")
    core._integration_probe_loop = lambda: _never_exit("probe")
    core._park = lambda: _never_exit("park")

    task = asyncio.ensure_future(core.run())
    await _wait_until_started(started, {"probe", "park"})
    # Only the always-on siblings ran; no ws / chain / drain.
    assert set(started) == {"probe", "park"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_live_drain_consumer_processes_a_real_detected_trade() -> None:
    """LIVE end-to-end: a trade enqueued during ``run`` is processed.

    The drain consumer attached by ``run`` pulls a ``DetectedTrade`` off
    ``_queue`` and runs it through the real ``process_detected`` pipeline.
    """
    core = _live_core()
    seen: list[str] = []

    async def _capture(trade: DetectedTrade) -> None:
        seen.append(str(trade.get("tx_hash")))

    core.process_detected = _capture

    # No real WS streams: the listeners just park; only the drain runs.
    class _Idle:
        async def run(self) -> None:
            await asyncio.Event().wait()

    core._build_live_listeners = lambda: (_Idle(), _Idle())

    async def _inert() -> None:
        await asyncio.Event().wait()

    core._integration_probe_loop = _inert
    core._park = _inert

    task = asyncio.ensure_future(core.run())
    await asyncio.sleep(0.02)  # let run_once + the TaskGroup spin up
    await core._queue.put({"tx_hash": "0xlive_e2e"})
    await asyncio.wait_for(core._queue.join(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "0xlive_e2e" in seen


# -- C2-P4: run_supervised (the DEPLOYED entrypoint) wiring ------------
#
# ``__main__`` deploys ``wiring.run_supervised`` (not ``TraderCore.run``),
# so the live listeners + drain consumer must attach there too.  These
# patch the heavy collaborators and capture the coroutine list handed to
# ``asyncio.gather`` to assert exactly which loops were started.


async def _gather_capture(monkeypatch: pytest.MonkeyPatch, core: Any) -> list[Any]:
    """Run ``run_supervised(core)`` with all heavy I/O stubbed; capture
    the coroutines passed to ``asyncio.gather``."""
    from polyecho.core import wiring

    captured: list[Any] = []

    async def _noop_startup() -> None:
        return None

    def _fake_build_redeem_worker(_follows: Any) -> Any:
        return object()

    async def _fake_gather(*coros: Any, **_kw: Any) -> list[Any]:
        captured.extend(coros)
        # Close every coroutine so pytest does not warn "never awaited".
        for c in coros:
            if asyncio.iscoroutine(c):
                c.close()
        return []

    monkeypatch.setattr(wiring, "startup_reconcile", _noop_startup)
    monkeypatch.setattr(wiring, "build_redeem_worker", _fake_build_redeem_worker)
    monkeypatch.setattr(asyncio, "gather", _fake_gather)
    await wiring.run_supervised(core)
    return captured


async def test_run_supervised_live_adds_listeners_and_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIVE: run_supervised hands ws + chain + drain to the gather."""
    core = _live_core()
    built: list[Any] = []

    class _Idle:
        async def run(self) -> None:  # pragma: no cover - never awaited
            await asyncio.Event().wait()

    def _build() -> Any:
        ws, chain = _Idle(), _Idle()
        built.extend([ws, chain])
        return ws, chain

    core._build_live_listeners = _build
    captured = await _gather_capture(monkeypatch, core)

    # 4 always-on loops + ws + chain + drain == 7 coroutines.
    assert len(captured) == 7
    assert len(built) == 2  # the live listeners WERE constructed


async def test_run_supervised_fixture_adds_no_live_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIXTURE: run_supervised hands ONLY the 4 always-on loops."""
    core = _fixture_core()

    def _fail_build() -> Any:  # must never be called in fixture mode
        raise AssertionError("live listeners must not be built in fixture mode")

    core._build_live_listeners = _fail_build
    captured = await _gather_capture(monkeypatch, core)

    # redeem + daily-reconcile + integration-probe + park == 4, no more.
    assert len(captured) == 4

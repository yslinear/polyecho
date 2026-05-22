"""Tests for the reusable reconnecting WS client (polyecho.core.ws_client).

No network: every test drives :class:`ReconnectingWSClient` through an
injected in-memory fake connection.  Covered: exponential backoff +
reset-on-open, re-subscribe on every (re)connect, ``"ping"`` cadence,
pong drop, clean-close (code 1000) does NOT reconnect, abnormal close
DOES, and the 30s no-message watchdog force-reconnect.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from polyecho.core import ws_client
from polyecho.core.ws_client import ReconnectingWSClient


class FakeWS:
    """An in-memory fake of a ``websockets`` client connection.

    The async iterator yields the queued ``inbound`` frames.  Once those
    are exhausted, behaviour depends on ``auto_close_after_drain``:

    * ``True`` (default) - the stream ends immediately with
      ``close_code`` (the realistic "peer closed the stream" case; lets
      a test ``await client.run()`` directly without a kicker task).
    * ``False`` - the iterator blocks until :meth:`end_stream` or
      :meth:`close` is called (a long-lived parked session a test or the
      watchdog ends explicitly).

    Frames sent by the client are recorded in :attr:`sent`.
    """

    def __init__(
        self,
        inbound: list[str] | None = None,
        close_code: int = 1000,
        *,
        auto_close_after_drain: bool = True,
        frame_delay: float = 0.0,
    ) -> None:
        self._inbound = list(inbound or [])
        self.sent: list[str | bytes] = []
        self.close_code: int | None = None
        self._final_close_code = close_code
        self._auto_close = auto_close_after_drain
        self._frame_delay = frame_delay
        self._closed = asyncio.Event()
        self.closed_by_call = False
        #: Every code passed to :meth:`close` (teardown 1000 + any
        #: watchdog 1011), in call order -- lets a test tell a watchdog
        #: force-close apart from the normal end-of-session teardown.
        self.close_codes: list[int] = []

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        if self._inbound:
            # An optional per-frame delay so a test can keep a stream
            # "alive" at a controlled cadence (watchdog liveness).
            if self._frame_delay:
                await asyncio.sleep(self._frame_delay)
            return self._inbound.pop(0)
        # Drain done.  Auto-close ends the stream immediately; otherwise
        # block until the session is closed by a test or the watchdog.
        if not self._auto_close:
            await self._closed.wait()
        if self.close_code is None:
            self.close_code = self._final_close_code
        raise StopAsyncIteration

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed_by_call = True
        self.close_codes.append(code)
        if self.close_code is None:
            self.close_code = code
        self._closed.set()

    def end_stream(self) -> None:
        """Let the peer close the stream with its preset final code."""
        self._closed.set()


def _connector(sessions: list[FakeWS]) -> Any:
    """Build an injectable ``connect`` that hands out *sessions* in order."""
    calls: list[dict[str, Any]] = []

    async def _connect(url: str, **kwargs: Any) -> FakeWS:
        idx = len(calls)
        calls.append({"url": url, "kwargs": kwargs})
        if idx < len(sessions):
            return sessions[idx]
        # No more scripted sessions: a session that never yields and never
        # closes (keeps run() parked so the test can cancel it).
        return FakeWS(inbound=[], auto_close_after_drain=False)

    _connect.calls = calls  # type: ignore[attr-defined]
    return _connect


async def _drain(coro_task: asyncio.Task[None]) -> None:
    """Cancel and await a backgrounded ``run()`` task."""
    coro_task.cancel()
    try:
        await coro_task
    except asyncio.CancelledError:
        pass


# -- message delivery + pong drop -------------------------------------


async def test_on_message_receives_frames_and_drops_pong() -> None:
    received: list[str | bytes] = []

    async def on_message(msg: str | bytes) -> None:
        received.append(msg)

    session = FakeWS(inbound=["hello", "pong", "PONG", "ping", "world"])
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        connect=_connector([session]),
    )
    await client.run()  # one clean session, then loop exits
    assert received == ["hello", "world"]


# -- re-subscribe on every (re)connect --------------------------------


async def test_subscribe_frames_and_on_open_fire_on_every_connect() -> None:
    open_count = 0

    async def on_open(c: ReconnectingWSClient) -> None:
        nonlocal open_count
        open_count += 1
        await c.send(f"dynamic-sub-{open_count}")

    async def on_message(_msg: str | bytes) -> None:
        return None

    # Session 1 closes abnormally (4000) -> reconnect; session 2 clean
    # -> the loop exits.  Both drain instantly (no inbound) and close.
    s1 = FakeWS(inbound=[], close_code=4000)
    s2 = FakeWS(inbound=[], close_code=1000)
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        subscribe_frames=["static-sub"],
        on_open=on_open,
        backoff_initial=0.0,
        connect=_connector([s1, s2]),
    )

    await asyncio.wait_for(client.run(), timeout=2.0)

    # on_open fired on BOTH connects; static frame sent BOTH times.
    assert open_count == 2
    assert s1.sent == ["dynamic-sub-1", "static-sub"]
    assert s2.sent == ["dynamic-sub-2", "static-sub"]


# -- clean close (1000) does NOT reconnect ----------------------------


async def test_clean_close_does_not_reconnect() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    s1 = FakeWS(inbound=["x"], close_code=1000)
    connector = _connector([s1])
    client = ReconnectingWSClient("wss://example/ws", on_message=on_message, connect=connector)
    await asyncio.wait_for(client.run(), timeout=2.0)
    # Exactly one connect: a clean close ends the supervised loop.
    assert len(connector.calls) == 1


# -- abnormal close DOES reconnect ------------------------------------


async def test_abnormal_close_triggers_reconnect() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    # s1 drains instantly and closes abnormally (1006) -> reconnect;
    # s2 drains and closes cleanly (1000) -> the loop exits.
    s1 = FakeWS(inbound=[], close_code=1006)  # abnormal
    s2 = FakeWS(inbound=[], close_code=1000)  # clean
    connector = _connector([s1, s2])
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        backoff_initial=0.0,
        connect=connector,
    )

    await asyncio.wait_for(client.run(), timeout=2.0)
    assert len(connector.calls) == 2


# -- exponential backoff sequence + reset-on-open ---------------------


async def test_backoff_sequence_is_exponential_capped() -> None:
    """3 -> 6 -> 12 -> 24 -> 48 -> 60 -> 60 with no successful open.

    The ``backoff_sleep`` seam records each delay and returns instantly,
    so the exact sequence is observed without any real waiting.
    """
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)
        # connect always fails -> the loop never exits on its own; once
        # enough samples are gathered, cancel it to end the test.
        if len(sleeps) >= 8:
            raise asyncio.CancelledError

    async def on_message(_msg: str | bytes) -> None:
        return None

    # connect() always raises => every iteration is a failed attempt.
    async def _always_fail(url: str, **kwargs: Any) -> Any:
        raise OSError("connect refused")

    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        connect=_always_fail,
        backoff_sleep=record_sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(client.run(), timeout=2.0)

    assert sleeps[:7] == [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0]


async def test_backoff_resets_on_successful_open() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    # s1 opens then closes abnormally -> one backoff sleep; s2 opens
    # again: the open must have reset backoff to the initial value, so
    # the only recorded sleep is the initial 3.0 (never the doubled 6.0).
    s1 = FakeWS(inbound=[], close_code=1006)
    s2 = FakeWS(inbound=[], close_code=1000)
    connector = _connector([s1, s2])

    seen_backoff: list[float] = []

    async def record_sleep(delay: float) -> None:
        seen_backoff.append(delay)

    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        backoff_initial=3.0,
        connect=connector,
        backoff_sleep=record_sleep,
    )

    await asyncio.wait_for(client.run(), timeout=2.0)

    # The single reconnect sleep was the initial 3.0: a successful open
    # reset the delay so it never grew past the initial value.
    assert seen_backoff == [3.0]


# -- ping cadence -----------------------------------------------------


async def test_ping_keepalive_cadence() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    # A parked session (never auto-closes) so pings accumulate.
    session = FakeWS(inbound=[], auto_close_after_drain=False)
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        ping_interval=0.01,
        connect=_connector([session]),
    )
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(0.08)  # ~8 ping intervals
    session.end_stream()
    await _drain(task)

    pings = [m for m in session.sent if m == "ping"]
    assert len(pings) >= 3
    assert all(p == "ping" for p in pings)


# -- 30s no-message watchdog ------------------------------------------


async def test_watchdog_force_reconnects_on_silence() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    # s1 is parked + silent -> the watchdog force-closes it; s2 drains
    # and closes cleanly so the loop exits.
    s1 = FakeWS(inbound=[], auto_close_after_drain=False)
    s2 = FakeWS(inbound=[], close_code=1000)
    connector = _connector([s1, s2])
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        ping_interval=10.0,  # irrelevant here
        watchdog_interval=0.01,
        watchdog_silence=0.02,
        backoff_initial=0.0,
        connect=connector,
    )

    # The watchdog force-closes the silent s1 -> reconnect to s2 which
    # drains + closes cleanly -> the supervised loop exits on its own.
    await asyncio.wait_for(client.run(), timeout=3.0)
    assert len(connector.calls) >= 2
    # The watchdog closed s1 with the abnormal code 1011 (a normal
    # teardown would only ever use 1000).
    assert 1011 in s1.close_codes


async def test_watchdog_quiet_when_messages_flow() -> None:
    """A steady message stream keeps last_message_at fresh: no force-close."""

    received: list[str | bytes] = []

    async def on_message(msg: str | bytes) -> None:
        received.append(msg)

    # Frames trickle every 5ms -- always inside the 50ms silence window,
    # so the watchdog (checking every 10ms) must never fire.  Parked
    # after drain so the test ends the session itself.
    session = FakeWS(
        inbound=[f"m{i}" for i in range(30)],
        auto_close_after_drain=False,
        frame_delay=0.005,
    )
    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        watchdog_interval=0.01,
        watchdog_silence=0.05,
        connect=_connector([session]),
    )
    task = asyncio.ensure_future(client.run())
    # Let the stream flow for longer than the silence window.
    await asyncio.sleep(0.12)
    # Still serviced by the message flow; not force-closed mid-stream.
    session.end_stream()
    await _drain(task)
    # The watchdog (code 1011) never fired; only the normal teardown
    # close (1000) is ever seen.
    assert 1011 not in session.close_codes
    assert len(received) >= 10  # the stream really was being consumed


# -- stop() ------------------------------------------------------------


async def test_stop_closes_clean_and_halts_loop() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    # A parked session so stop() is what ends it (not an auto-close).
    session = FakeWS(inbound=[], auto_close_after_drain=False)
    connector = _connector([session])
    client = ReconnectingWSClient("wss://example/ws", on_message=on_message, connect=connector)
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(0.02)
    await client.stop()
    await asyncio.wait_for(task, timeout=2.0)
    # stop() closed with code 1000 and the loop did not reconnect.
    assert session.close_code == ws_client.CLOSE_CODE_NORMAL
    assert len(connector.calls) == 1


# -- run() is TaskGroup-safe (re-raises only CancelledError) ----------


async def test_run_is_cancel_safe() -> None:
    async def on_message(_msg: str | bytes) -> None:
        return None

    client = ReconnectingWSClient(
        "wss://example/ws",
        on_message=on_message,
        connect=_connector([FakeWS(inbound=[], auto_close_after_drain=False)]),
    )
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

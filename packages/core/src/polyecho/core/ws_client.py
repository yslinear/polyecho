"""Reusable reconnecting async WebSocket client (C2-P1).

A standalone, supervised WS client that the C2 live-listener phases
(``UserChannelListener`` / ``ChainCrossCheckListener``) will build on.
It is NOT wired into :mod:`polyecho.core.detection` or ``TraderCore`` yet
that is C2-P2 / P3 / P4.

PolyHermes' ``PolymarketWebSocketClient`` is the reference design.  This
module owns the unglamorous connection plumbing so a caller only has to
supply the subscription frame(s) and a message handler:

* **Exponential reconnect/backoff** — first retry waits
  :data:`BACKOFF_INITIAL_SECONDS` (3s), doubling on each consecutive
  failure, capped at :data:`BACKOFF_MAX_SECONDS` (60s):
  ``3 -> 6 -> 12 -> 24 -> 48 -> 60 -> 60 ...``.  The delay resets to 3s
  the moment a connection opens successfully.
* **Re-subscribe** — :class:`ReconnectingWSClient` fires the caller's
  ``on_open`` callback right after *every* open (the first connect AND
  every reconnect) so the caller re-sends its subscription frame(s).
  Frames passed as ``subscribe_frames`` are additionally sent verbatim
  after ``on_open`` for the common static-subscription case.
* **Ping keepalive** — the literal lowercase string ``"ping"`` is sent
  every :data:`PING_INTERVAL_SECONDS` (10s).  Any ``"pong"`` / ``"PONG"``
  reply is swallowed and never delivered to ``on_message``.
* **Clean vs abnormal close** — reconnect happens ONLY on an abnormal
  close (close code != 1000).  An intentional :meth:`stop` closes with
  code 1000 and the supervised loop exits without reconnecting.
* **No-message liveness watchdog** — every
  :data:`WATCHDOG_INTERVAL_SECONDS` (30s) the client compares
  ``now - last_message_at``; at >= :data:`WATCHDOG_SILENCE_SECONDS`
  (30s) of silence it force-closes the socket, which the supervisor
  treats as abnormal and reconnects.  PolyHermes wrote this watchdog but
  left it commented out; PolyEcho ENABLES it.

The connection is routed through :func:`polyecho.shared.http.get_proxy_url`
(the gluetun seam) exactly as :mod:`polyecho.core.ws_probe` does:
``*.polymarket.com``'s direct path is TLS-intercepted, only the proxy/VPN
egress is clean.  ``None`` (no proxy configured) means a direct
connection.

No network or DB I/O on import.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from polyecho.shared.http import get_proxy_url

logger = logging.getLogger(__name__)

#: First reconnect delay; the base of the exponential backoff.
BACKOFF_INITIAL_SECONDS: float = 3.0
#: Hard ceiling on the reconnect delay (``3 -> 6 -> 12 -> 24 -> 48 -> 60``).
BACKOFF_MAX_SECONDS: float = 60.0
#: Multiplier applied to the backoff after each consecutive failure.
BACKOFF_FACTOR: float = 2.0

#: Cadence of the ``"ping"`` keepalive frame.
PING_INTERVAL_SECONDS: float = 10.0

#: Cadence of the no-message liveness watchdog check.
WATCHDOG_INTERVAL_SECONDS: float = 30.0
#: Silence threshold: at/above this gap with no message the socket is
#: force-closed and the supervisor reconnects.
WATCHDOG_SILENCE_SECONDS: float = 30.0

#: RFC 6455 normal-closure code.  A close with this code is "clean" and
#: NOT followed by a reconnect.
CLOSE_CODE_NORMAL: int = 1000

#: Outbound keepalive payload (literal lowercase, per the reference).
_PING_FRAME: str = "ping"
#: Replies that are keepalive echoes and must not reach ``on_message``.
_PONG_REPLIES: frozenset[str] = frozenset({"ping", "pong"})

#: An async message handler: receives each non-keepalive inbound frame.
OnMessage = Callable[[str | bytes], Awaitable[None]]
#: An async open hook: fired after every (re)connect for re-subscription.
OnOpen = Callable[["ReconnectingWSClient"], Awaitable[None]]


def _is_pong(message: str | bytes) -> bool:
    """Return ``True`` if *message* is a keepalive echo to be dropped."""
    if isinstance(message, bytes):
        try:
            text = message.decode("utf-8")
        except UnicodeDecodeError:
            return False
    else:
        text = message
    return text.strip().lower() in _PONG_REPLIES


class ReconnectingWSClient:
    """A supervised, self-healing async WebSocket client.

    The client owns connect / backoff / ping / watchdog / clean-close.
    The caller owns the subscription-frame content and message handling.

    :param url: the WebSocket URL to connect to.
    :param on_message: async callback invoked with every inbound frame
        that is not a keepalive echo.
    :param subscribe_frames: optional static frames sent verbatim after
        every (re)connect once ``on_open`` has run.
    :param on_open: optional async callback fired after every successful
        (re)connect, before ``subscribe_frames`` are sent, so a caller
        with a dynamic subscription can re-send it.
    :param ping_interval: keepalive cadence (seconds).
    :param watchdog_interval: liveness-check cadence (seconds).
    :param watchdog_silence: no-message silence threshold (seconds).
    :param backoff_initial / backoff_max / backoff_factor: the
        exponential reconnect-delay parameters.
    :param connect: injection seam for ``websockets.connect`` (tests pass
        an in-memory transport here).
    :param backoff_sleep: injection seam for the reconnect-delay sleep
        (defaults to :func:`asyncio.sleep`); tests pass an instant,
        observable stand-in.  ONLY the backoff wait uses this seam; the
        ping / watchdog loops always use the real :func:`asyncio.sleep`.
    """

    def __init__(
        self,
        url: str,
        *,
        on_message: OnMessage,
        subscribe_frames: Sequence[str] | None = None,
        on_open: OnOpen | None = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        watchdog_interval: float = WATCHDOG_INTERVAL_SECONDS,
        watchdog_silence: float = WATCHDOG_SILENCE_SECONDS,
        backoff_initial: float = BACKOFF_INITIAL_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
        backoff_factor: float = BACKOFF_FACTOR,
        connect: Callable[..., Any] | None = None,
        backoff_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._url = url
        self._on_message = on_message
        self._subscribe_frames: tuple[str, ...] = tuple(subscribe_frames or ())
        self._on_open = on_open
        self._ping_interval = ping_interval
        self._watchdog_interval = watchdog_interval
        self._watchdog_silence = watchdog_silence
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._backoff_factor = backoff_factor
        self._connect = connect
        self._backoff_sleep = backoff_sleep if backoff_sleep is not None else asyncio.sleep

        #: Current reconnect delay; resets to ``backoff_initial`` on open.
        self._backoff = backoff_initial
        #: Monotonic timestamp of the last inbound message (watchdog input).
        self._last_message_at = 0.0
        #: Set by :meth:`stop` so the supervised loop exits cleanly.
        self._stopping = False
        #: The live connection object (set while a session is open).
        self._ws: Any | None = None

    # -- public API ---------------------------------------------------

    async def run(self) -> None:
        """Supervised never-exit connect/reconnect loop (TaskGroup-safe).

        Each iteration opens one WS session and services it until it
        closes.  An abnormal close (or a watchdog force-close) is followed
        by an exponential-backoff sleep and a reconnect; a clean close
        (code 1000, via :meth:`stop`) ends the loop.  Only
        :class:`asyncio.CancelledError` is re-raised so the client is safe
        to launch as a long-lived sibling inside an ``asyncio.TaskGroup``.
        """
        while not self._stopping:
            clean = False
            try:
                clean = await self._run_one_session()
            except asyncio.CancelledError:
                # Cooperative cancellation always propagates.
                raise
            except Exception as exc:  # noqa: BLE001 - supervised: log + retry
                logger.warning(
                    "ws_session_error",
                    extra={"url": self._url, "error": repr(exc)},
                )
            if self._stopping or clean:
                # Intentional stop, or a clean (code 1000) close: done.
                break
            await self._sleep_backoff()

    async def stop(self) -> None:
        """Request a clean shutdown: close with code 1000, no reconnect.

        Safe to call from another task while :meth:`run` is in flight; the
        supervised loop observes the flag and exits without reconnecting.
        """
        self._stopping = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code=CLOSE_CODE_NORMAL)

    async def send(self, message: str | bytes) -> None:
        """Send *message* on the live socket (no-op if not connected)."""
        ws = self._ws
        if ws is None:
            logger.debug("ws_send_skipped_no_connection", extra={"url": self._url})
            return
        await ws.send(message)

    @property
    def backoff_seconds(self) -> float:
        """The delay the *next* reconnect would wait (test/observability)."""
        return self._backoff

    @property
    def last_message_at(self) -> float:
        """Monotonic timestamp of the last inbound message (0.0 if none)."""
        return self._last_message_at

    # -- internals ----------------------------------------------------

    async def _open(self) -> Any:
        """Open one WS connection through the gluetun proxy seam."""
        # Same proxy pattern as ws_probe: *.polymarket.com is
        # TLS-intercepted on the direct path; None => direct.
        proxy = get_proxy_url()
        connect = self._connect
        if connect is None:
            # Imported lazily so the module imports with no network stack.
            import websockets

            connect = websockets.connect
        return await connect(self._url, proxy=proxy)

    async def _run_one_session(self) -> bool:
        """Open, service and tear down exactly one WS session.

        Returns ``True`` iff the session ended with a *clean* close
        (code 1000) and the supervisor must NOT reconnect; ``False`` for
        an abnormal close (reconnect) or a watchdog force-close.
        """
        ws = await self._open()
        self._ws = ws
        # A successful open: reset the backoff and prime the watchdog.
        self._backoff = self._backoff_initial
        self._last_message_at = time.monotonic()
        logger.info("ws_connected", extra={"url": self._url})

        clean = False
        try:
            # Re-subscribe: fire on_open (dynamic) then static frames.
            if self._on_open is not None:
                await self._on_open(self)
            for frame in self._subscribe_frames:
                await ws.send(frame)
            clean = await self._service_session(ws)
        finally:
            self._ws = None
            with contextlib.suppress(Exception):
                await ws.close(code=CLOSE_CODE_NORMAL)
        return clean

    async def _service_session(self, ws: Any) -> bool:
        """Run reader + ping + watchdog until the session ends.

        Returns ``True`` for a clean close (code 1000), ``False`` for an
        abnormal close or a watchdog-triggered force-close.
        """
        ping_task = asyncio.ensure_future(self._ping_loop(ws))
        watchdog_task = asyncio.ensure_future(self._watchdog_loop(ws))
        try:
            return await self._read_loop(ws)
        finally:
            for task in (ping_task, watchdog_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _read_loop(self, ws: Any) -> bool:
        """Consume inbound frames; classify the terminating close.

        Returns ``True`` iff the socket closed cleanly (code 1000).
        """
        try:
            async for message in ws:
                self._last_message_at = time.monotonic()
                if _is_pong(message):
                    continue
                await self._on_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify as abnormal close
            logger.warning("ws_read_error", extra={"url": self._url, "error": repr(exc)})
            return False
        # The async iterator ended: the peer closed.  websockets exposes
        # the close code on the connection; 1000 (or absent) => clean.
        code = getattr(ws, "close_code", None)
        clean = code is None or code == CLOSE_CODE_NORMAL
        logger.info(
            "ws_closed",
            extra={"url": self._url, "close_code": code, "clean": clean},
        )
        return clean

    async def _ping_loop(self, ws: Any) -> None:
        """Send the literal ``"ping"`` keepalive every ``ping_interval``."""
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                await ws.send(_PING_FRAME)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead socket ends the loop
            logger.debug("ws_ping_loop_ended", extra={"url": self._url, "error": repr(exc)})

    async def _watchdog_loop(self, ws: Any) -> None:
        """Force-close the socket after ``watchdog_silence`` of no messages.

        PolyHermes left this loop commented out; PolyEcho enables it.  A
        force-close makes the async reader iterator stop, which the
        supervisor classifies as abnormal and reconnects.
        """
        try:
            while True:
                await asyncio.sleep(self._watchdog_interval)
                silence = time.monotonic() - self._last_message_at
                if silence >= self._watchdog_silence:
                    logger.warning(
                        "ws_watchdog_force_reconnect",
                        extra={"url": self._url, "silence_seconds": silence},
                    )
                    with contextlib.suppress(Exception):
                        # Abnormal code => the supervisor reconnects.
                        await ws.close(code=1011)
                    return
        except asyncio.CancelledError:
            raise

    async def _sleep_backoff(self) -> None:
        """Sleep the current backoff, then double it (capped at the max)."""
        delay = self._backoff
        logger.info(
            "ws_reconnect_backoff",
            extra={"url": self._url, "delay_seconds": delay},
        )
        await self._backoff_sleep(delay)
        self._backoff = min(self._backoff * self._backoff_factor, self._backoff_max)

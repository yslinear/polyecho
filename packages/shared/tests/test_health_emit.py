"""Tests for the ONE fail-soft heartbeat seam ``polyecho.shared.health_emit``.

This seam is the single consolidated implementation of the lazy-import +
sync/coroutine-tolerant + swallow-all-but-``CancelledError`` heartbeat
wrapper that ``core`` / ``discovery`` / ``backtest`` / ``push`` previously
each re-implemented.  These tests pin the contract every one of those call
sites depends on:

* an arbitrary exception from the frozen ``write_heartbeat`` is swallowed
  (the heartbeat is pure observability; it must never abort its caller)
  and the call site's ``on_error`` hook is invoked with that exception so
  it can emit its own log event;
* :class:`asyncio.CancelledError` is ALWAYS re-raised (graceful shutdown /
  task-cancel is never a "heartbeat failure" and must never be eaten),
  and ``on_error`` is NOT called for it;
* a *coroutine* ``write_heartbeat`` is awaited (no "coroutine was never
  awaited" leak) and a *sync* one is tolerated too;
* an ABSENT frozen ``polyecho.shared.health`` (isolated build → the lazy
  ``from polyecho.shared.health import write_heartbeat`` raises
  ``ImportError``) degrades to a swallowed no-op;
* the canonical keyword-only contract (``kind`` positional; ``phase`` /
  ``datasource`` / ``live_trading`` / ``counters`` / ``extra`` kw-only —
  F0 stamps ``ts`` itself) is forwarded verbatim;
* a bug *inside* the caller's ``on_error`` logger does not turn a
  swallowed heartbeat failure into a raise.

Fixture-only, no network / no Postgres: a fake ``polyecho.shared.health``
is injected into ``sys.modules`` (mirroring how the real call-site tests
in core/backtest/push prove the lazy-import seam).  ``asyncio_mode=auto``
(repo pyproject) so ``async def test_*`` run without an explicit marker.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from polyecho.shared.health_emit import emit_heartbeat_safe


@contextmanager
def _fake_health(write_impl: Any) -> Iterator[None]:
    """Inject a fake ``polyecho.shared.health`` exposing ``write_impl``.

    The seam does a *lazy* in-function
    ``from polyecho.shared.health import write_heartbeat``; patching
    ``sys.modules`` makes that bind to ``write_impl``.  Restored on exit so
    no test leaks the fake (and so a real F0 outside an isolated build is
    never shadowed beyond the test body).
    """
    mod = types.ModuleType("polyecho.shared.health")
    mod.write_heartbeat = write_impl  # type: ignore[attr-defined]
    prev = sys.modules.get("polyecho.shared.health")
    sys.modules["polyecho.shared.health"] = mod
    try:
        yield
    finally:
        if prev is not None:
            sys.modules["polyecho.shared.health"] = prev
        else:
            sys.modules.pop("polyecho.shared.health", None)


# ---------------------------------------------------------------------------
# 1. Forwards the FROZEN keyword-only contract verbatim
# ---------------------------------------------------------------------------


async def test_forwards_canonical_kwonly_contract() -> None:
    """``kind`` is positional; phase/datasource/live_trading/counters/extra
    are forwarded keyword-only (F0 stamps ``ts`` itself — no ``ts`` arg)."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _wh(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    with _fake_health(_wh):
        await emit_heartbeat_safe(
            "backtest",
            phase="running",
            datasource="fixture",
            live_trading=False,
            counters={"processed": 3, "failed": 1},
            extra={"note": "x"},
        )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("backtest",)
    assert kwargs == {
        "phase": "running",
        "datasource": "fixture",
        "live_trading": False,
        "counters": {"processed": 3, "failed": 1},
        "extra": {"note": "x"},
    }
    assert "ts" not in kwargs  # F0 stamps ts itself; caller passes none


# ---------------------------------------------------------------------------
# 2. Arbitrary exceptions are swallowed; on_error gets the exception
# ---------------------------------------------------------------------------


async def test_swallows_arbitrary_exception_and_invokes_on_error() -> None:
    """A raising ``write_heartbeat`` must NOT propagate; the call site's
    ``on_error`` hook is invoked with the swallowed exception so it can log
    its own (per-site) event name."""
    boom = RuntimeError("settings upsert exploded")
    seen: list[BaseException] = []

    def _raise(*_a: Any, **_k: Any) -> None:
        raise boom

    # Must complete without raising.
    with _fake_health(_raise):
        await emit_heartbeat_safe(
            "push", phase="idle", on_error=seen.append
        )

    assert seen == [boom]


async def test_swallows_with_no_on_error_hook() -> None:
    """``on_error`` is optional: a failure with no hook is still swallowed
    (the seam never requires a logger to stay fail-soft)."""
    with _fake_health(lambda *_a, **_k: (_ for _ in ()).throw(ValueError())):
        await emit_heartbeat_safe("trader", phase="cycle_start")


# ---------------------------------------------------------------------------
# 3. CancelledError ALWAYS propagates and on_error is NOT called for it
# ---------------------------------------------------------------------------


async def test_reraises_cancelled_error_and_skips_on_error() -> None:
    """``asyncio.CancelledError`` must propagate (graceful shutdown is not a
    heartbeat failure) and ``on_error`` must NOT be invoked for it."""
    on_error_calls: list[BaseException] = []

    def _cancel(*_a: Any, **_k: Any) -> None:
        raise asyncio.CancelledError

    with _fake_health(_cancel):
        with pytest.raises(asyncio.CancelledError):
            await emit_heartbeat_safe(
                "push", phase="idle", on_error=on_error_calls.append
            )

    assert on_error_calls == []  # cancellation is NOT a swallowed failure


async def test_reraises_cancelled_from_awaited_coroutine() -> None:
    """A coroutine ``write_heartbeat`` that cancels mid-await must still
    propagate ``CancelledError`` (the await path, not just the call path)."""

    async def _cancel_async(*_a: Any, **_k: Any) -> None:
        raise asyncio.CancelledError

    with _fake_health(_cancel_async):
        with pytest.raises(asyncio.CancelledError):
            await emit_heartbeat_safe("discovery", phase="daily")


# ---------------------------------------------------------------------------
# 4. Sync AND coroutine write_heartbeat are both tolerated
# ---------------------------------------------------------------------------


async def test_awaits_coroutine_write_impl() -> None:
    """An async ``write_heartbeat`` is awaited (no un-awaited-coroutine
    warning) — proven by the side effect actually running."""
    ran: list[str] = []

    async def _async_wh(kind: str, **_k: Any) -> None:
        ran.append(kind)

    with _fake_health(_async_wh):
        await emit_heartbeat_safe("push", phase="sending")

    assert ran == ["push"]


async def test_tolerates_sync_write_impl() -> None:
    """A plain (non-coroutine) ``write_heartbeat`` is also accepted — the
    seam must not assume one shape."""
    ran: list[str] = []

    def _sync_wh(kind: str, **_k: Any) -> None:
        ran.append(kind)

    with _fake_health(_sync_wh):
        await emit_heartbeat_safe("backtest", phase="idle")

    assert ran == ["backtest"]


# ---------------------------------------------------------------------------
# 5. Absent F0 (isolated build) → swallowed ImportError, no-op
# ---------------------------------------------------------------------------


async def test_absent_frozen_health_is_swallowed() -> None:
    """If ``polyecho.shared.health`` is genuinely ABSENT, the lazy
    ``from polyecho.shared.health import write_heartbeat`` raises
    ``ImportError`` — which must degrade to a swallowed no-op (the whole
    reason the import is lazy and the seam exists)."""
    seen: list[BaseException] = []
    prev = sys.modules.pop("polyecho.shared.health", None)
    # Also block re-import from disk so this asserts the *absent* path even
    # when the real F0 is installed (integrated verify).
    sys.modules["polyecho.shared.health"] = None  # type: ignore[assignment]
    try:
        await emit_heartbeat_safe(
            "discovery", phase="once", on_error=seen.append
        )
    finally:
        if prev is not None:
            sys.modules["polyecho.shared.health"] = prev
        else:
            sys.modules.pop("polyecho.shared.health", None)

    assert len(seen) == 1
    assert isinstance(seen[0], ImportError)


# ---------------------------------------------------------------------------
# 6. A bug inside the caller's on_error logger does not become a raise
# ---------------------------------------------------------------------------


async def test_on_error_failure_is_itself_swallowed() -> None:
    """A broken caller logger must not turn a swallowed heartbeat failure
    into a propagating exception — that would defeat the fail-soft seam."""

    def _raise(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("write failed")

    def _broken_on_error(_exc: BaseException) -> None:
        raise ValueError("logger blew up")

    # Neither the write error nor the logger error may propagate.
    with _fake_health(_raise):
        await emit_heartbeat_safe(
            "trader", phase="cycle_complete", on_error=_broken_on_error
        )

"""
polyecho.shared.health_emit
===========================
ONE fail-soft seam for emitting a worker heartbeat — the single place the
"lazy-import the FROZEN :func:`polyecho.shared.health.write_heartbeat` →
tolerate a sync *or* coroutine impl → swallow EVERYTHING except
:class:`asyncio.CancelledError` → let the caller log its own event" wrapper
lives.

Why this module exists
----------------------
Four services (``core`` / ``discovery`` / ``backtest`` / ``push``) each
independently re-implemented the *identical* best-effort heartbeat wrapper.
That is four copies of the same delicate fail-soft contract (lazy import so
an isolated build without F0 still works; ``CancelledError`` MUST propagate
so graceful shutdown is never silently eaten; the F0 ``write_heartbeat`` may
be sync or a coroutine).  This collapses the *mechanism* into one tested
seam while letting every call site keep its EXACT observable behaviour —
each one still emits its own structured log event on failure via the
:paramref:`on_error` hook, so no test that asserts a specific event name
(``web_push_heartbeat_write_failed`` /
``backtest_heartbeat_write_failed_continuing`` /
``trader_heartbeat_canonical_write_failed_continuing`` /
``discovery_heartbeat_failed_continuing``) changes.

This is a **non-frozen sibling** of :mod:`polyecho.shared.health`.  It
WRAPS the frozen ``write_heartbeat``; it does NOT change it.  The frozen
signature it targets is::

    write_heartbeat(kind, *, phase, datasource=None, live_trading=None,
                    counters=None, extra=None)            # F0 stamps ``ts``

— keyword-only after ``kind``.  :func:`emit_heartbeat_safe` forwards
exactly that shape.

Fail-soft contract (the whole point of the seam)
------------------------------------------------
* The ``from polyecho.shared.health import write_heartbeat`` is done
  **inside** the function, never at module top — F0 lives in
  ``packages/shared`` but a *consumer's* isolated test base may legitimately
  not have it; an :class:`ImportError` there must degrade to a swallowed
  no-op, never abort the worker.
* ``write_heartbeat`` may be a plain function or a coroutine function
  depending on the F0 build / a test double; the result is awaited iff it
  is a coroutine (``asyncio.iscoroutine``) so either shape is tolerated
  without coupling to one.
* **Every** exception is swallowed *except* :class:`asyncio.CancelledError`,
  which is ALWAYS re-raised — a cooperative cancel / graceful shutdown is
  not a "heartbeat failure" and swallowing it would wedge shutdown.  This
  matches the strictest existing call site (push) and is a no-op tightening
  for the others (their ``except Exception`` never caught ``CancelledError``
  anyway, since it derives from ``BaseException``).
* On a swallowed failure the optional :paramref:`on_error` callback is
  invoked with the exception so the *call site* emits its own log event
  with its own field names — keeping the seam logging-framework-agnostic
  (``core``/``backtest`` use stdlib ``logging``; ``push`` uses ``structlog``)
  and every existing log-assertion green.  ``on_error`` is itself called
  defensively: a bug in the caller's logger must not turn a swallowed
  heartbeat failure into a raise.

No module-import side effects; no network/DB on import.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

__all__ = ["emit_heartbeat_safe"]


async def emit_heartbeat_safe(
    kind: str,
    *,
    phase: str,
    datasource: str | None = None,
    live_trading: bool | None = None,
    counters: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Emit one worker heartbeat, best-effort — the single shared seam.

    Lazily imports the FROZEN
    :func:`polyecho.shared.health.write_heartbeat`, forwards the canonical
    keyword-only contract (``phase`` / ``datasource`` / ``live_trading`` /
    ``counters`` / ``extra``; F0 stamps ``ts`` itself), and awaits the
    result iff it is a coroutine.

    STRICTLY fail-soft: ANY failure — the (possibly-absent) F0 import, a DB
    blip inside ``write_heartbeat``, a contract mismatch — is swallowed so
    the heartbeat can never abort or stall its caller.  The lone exception
    is :class:`asyncio.CancelledError`, which is ALWAYS re-raised so a
    graceful shutdown / task-cancel is never silently eaten.

    Args:
        kind: Logical worker name (``"trader"`` / ``"discovery"`` /
            ``"backtest"`` / ``"push"``) or a full ``*_heartbeat`` key —
            passed straight through to F0's ``write_heartbeat``.
        phase: Free-form liveness phase string (e.g. ``"idle"`` /
            ``"running"`` / ``"cycle_start"`` / ``"sending"``).
        datasource: ``"fixture"`` / ``"live"`` (or ``None`` to omit).
        live_trading: Master live-money lock state (or ``None`` to omit).
        counters: This-cycle tally dict (defensively forwarded as-is; F0
            copies it into the nested ``counters`` block).
        extra: Optional extra top-level heartbeat keys (F0 never lets these
            clobber the load-bearing fields).
        on_error: Optional callback invoked with the swallowed exception so
            the call site can emit its own structured log event (keeping
            each site's existing event name/fields unchanged).  Never
            invoked for :class:`asyncio.CancelledError` (that propagates).
            A failure *inside* this callback is itself swallowed.

    Returns:
        ``None`` — fire-and-forget liveness; callers ignore the outcome.
    """
    try:
        # Lazy import INSIDE the guard: F0 may be absent in a consumer's
        # isolated build; an ImportError here must degrade to a swallowed
        # no-op, never abort the worker (this is the seam's reason to exist).
        from polyecho.shared.health import write_heartbeat

        # Forward optionals ONLY when supplied.  F0's ``write_heartbeat``
        # itself treats ``datasource``/``live_trading``/``counters``/
        # ``extra`` as optional and omits unset ones; passing an explicit
        # ``…=None`` would also change the EXACT call signature each
        # collapsed call site previously used (none passed ``extra``), and
        # the call-site test doubles mirror that narrower signature — so
        # only the kwargs the caller actually gave are propagated, keeping
        # observable semantics byte-identical to the 4 originals.
        kwargs: dict[str, Any] = {"phase": phase}
        if datasource is not None:
            kwargs["datasource"] = datasource
        if live_trading is not None:
            kwargs["live_trading"] = live_trading
        if counters is not None:
            kwargs["counters"] = counters
        if extra is not None:
            kwargs["extra"] = extra

        result = write_heartbeat(kind, **kwargs)
        # F0's ``write_heartbeat`` may be sync or a coroutine (or a test
        # double of either shape) — await only if it actually returned an
        # awaitable so neither shape is coupled to.
        if asyncio.iscoroutine(result):
            await result
    except asyncio.CancelledError:
        # Cooperative cancellation is NOT a heartbeat failure — never
        # swallow it or graceful shutdown wedges.  Always propagate.
        raise
    except BaseException as exc:  # noqa: BLE001 - heartbeat is fail-soft
        # Anything else (absent F0, DB blip inside write_heartbeat, a
        # contract mismatch, …) is swallowed so the caller is untouched.
        # The call site logs its own event via on_error so existing
        # log-name assertions stay green.
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:  # noqa: BLE001 - a logging bug must not raise
                # A broken caller logger must not turn a swallowed
                # heartbeat failure into a raise — that would defeat the
                # entire fail-soft contract.
                pass

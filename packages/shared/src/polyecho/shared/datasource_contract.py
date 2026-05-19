"""
polyecho.shared.datasource_contract
===================================
Importable **datasource-lifecycle contract assertions** — the user-mandated
regression infrastructure that prevents the live-datasource recurrence (a
``LiveDataSource`` / ``get_datasource()`` result used WITHOUT being entered as
an ``async with`` context manager → an un-opened httpx client / ``None``).

This module is **shipped (not test-only)** so *every* package's test suite can
``import`` these helpers and lock the same invariant without copy-pasting it:

    from polyecho.shared.datasource_contract import (
        assert_requires_async_context,
        assert_datasource_factory_contract,
    )

    def test_my_worker_uses_datasource_via_async_with() -> None:
        assert_datasource_factory_contract()

It performs **no** network or DB I/O and has no import side effects.  It does
NOT modify ``LiveDataSource`` — it only *probes* the already-existing guard
(``LiveDataSource._ensure_client`` raises a clear ``RuntimeError`` mentioning
"async context manager" when used un-entered) and asserts the
``get_datasource()`` factory contract documented in
``polyecho.shared.datasource``.

The invariant being locked
--------------------------
``LiveDataSource`` opens its proxy-resolved httpx client in ``__aenter__`` and
closes it in ``__aexit__``.  Any code path that does ``ds = LiveDataSource()``
(or ``ds = get_datasource()`` when ``POLYECHO_DATASOURCE=live``) and then calls
a data method WITHOUT ``async with`` must fail fast with that clear error —
never silently hit ``self._client is None``.  These assertions make any such
future regression a red test in whichever package introduced it.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

__all__ = [
    "CONTEXT_MANAGER_ERROR_PATTERN",
    "assert_requires_async_context",
    "assert_datasource_factory_contract",
]

#: The substring/regex the un-entered-use ``RuntimeError`` MUST contain.  This
#: is itself part of the locked contract: if someone rewors the guard message
#: so it no longer tells the operator HOW to fix it, that is a regression.
CONTEXT_MANAGER_ERROR_PATTERN = r"async context manager"


def _drive(coro: Any) -> Any:
    """Run *coro* on the current thread's event loop (test-bridge parity).

    Mirrors ``packages/shared/tests/conftest.py``'s ``run`` so this helper
    is usable from a plain sync test in any package without that package
    needing its own async harness.
    """
    return asyncio.get_event_loop().run_until_complete(coro)


def assert_requires_async_context(
    factory: Any,
    *,
    method: str = "get_leaderboard",
    call: Any = None,
) -> None:
    """Assert a datasource built by *factory* rejects un-``async with`` use.

    *factory* is a zero-arg callable returning the datasource instance under
    test (e.g. ``lambda: LiveDataSource(settings=...)`` or a partial of
    ``get_datasource``).  The instance's ``method`` is invoked WITHOUT
    entering it as a context manager; this MUST raise ``RuntimeError``
    matching :data:`CONTEXT_MANAGER_ERROR_PATTERN`.

    Args:
        factory: ``() -> datasource`` under test.
        method:  Name of the async data method to probe (default
            ``get_leaderboard``).
        call:    Optional ``(ds) -> awaitable`` override if the chosen
            method needs specific arguments; defaults to calling
            ``getattr(ds, method)()``.

    Raises:
        AssertionError: if the un-entered call did NOT raise, or raised
            something other than a ``RuntimeError`` whose message matches
            the required pattern.
    """
    ds = factory()
    awaitable_factory = call or (lambda d: getattr(d, method)())
    try:
        _drive(awaitable_factory(ds))
    except RuntimeError as exc:
        if not re.search(CONTEXT_MANAGER_ERROR_PATTERN, str(exc)):
            raise AssertionError(
                "datasource raised RuntimeError when used without "
                f"`async with`, but the message {str(exc)!r} does not "
                f"match the required pattern "
                f"{CONTEXT_MANAGER_ERROR_PATTERN!r} — the guard must tell "
                "the caller to use it as an async context manager."
            ) from exc
        return
    except Exception as exc:  # noqa: BLE001 - any other error is a contract break
        raise AssertionError(
            f"datasource.{method}() used WITHOUT `async with` raised "
            f"{type(exc).__name__} ({exc!r}); the lifecycle contract "
            "requires a clear RuntimeError naming the async context "
            "manager, not this."
        ) from exc
    raise AssertionError(
        f"datasource.{method}() returned WITHOUT being entered via "
        "`async with` — the lifecycle guard is MISSING. A LiveDataSource "
        "used un-entered must raise a RuntimeError naming the async "
        "context manager (this is the live-datasource regression guard)."
    )


def assert_datasource_factory_contract() -> None:
    """Lock the ``get_datasource()`` / ``LiveDataSource`` lifecycle contract.

    Asserts, with NO network/DB:

    1. ``LiveDataSource`` implements the async-context-manager protocol
       (``__aenter__`` / ``__aexit__`` are coroutine functions).
    2. A freshly constructed ``LiveDataSource`` (NOT entered) raises the
       clear ``RuntimeError`` (pattern :data:`CONTEXT_MANAGER_ERROR_PATTERN`)
       on first data-method use — i.e. the guard exists and is wired.
    3. ``get_datasource`` is importable and its module documents the
       ``async with`` requirement for the live datasource (the contract the
       6 dependent agents rely on).

    Any package can call this from a sync test as the single line that
    keeps the live-datasource recurrence caught.
    """
    from polyecho.shared import datasource as ds_mod
    from polyecho.shared.datasource import LiveDataSource, get_datasource

    # (1) protocol shape
    assert inspect.iscoroutinefunction(
        LiveDataSource.__aenter__
    ), "LiveDataSource.__aenter__ must be an async (coroutine) function"
    assert inspect.iscoroutinefunction(
        LiveDataSource.__aexit__
    ), "LiveDataSource.__aexit__ must be an async (coroutine) function"

    # (2) the guard fires for an un-entered instance.  A minimal stand-in
    # settings object avoids importing real env / config.
    class _S:
        datasource = "live"
        polymarket_data_api_url = "https://data-api.polymarket.com"

    assert_requires_async_context(lambda: LiveDataSource(settings=_S()))

    # (3) factory + documented contract
    assert callable(get_datasource), "get_datasource must be callable"
    doc = (inspect.getdoc(ds_mod) or "") + (
        inspect.getdoc(get_datasource) or ""
    )
    assert "async with" in doc, (
        "polyecho.shared.datasource must document the `async with` "
        "requirement for the live datasource (the cross-package lifecycle "
        "contract the status feature's 6 agents build against)."
    )

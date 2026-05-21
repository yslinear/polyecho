"""P11 Bug 1 regression-lock: backtest worker must enter the datasource as
an async context manager.

The backtest worker entry-point (``polyecho.backtest.__main__``) previously
did::

    datasource = get_datasource()
    await process_pending_jobs(datasource, ...)

For ``POLYECHO_DATASOURCE=fixture`` this worked (``FixtureDataSource`` has no
I/O lifecycle).  For ``POLYECHO_DATASOURCE=live`` it raised, **per leader**,
``RuntimeError: LiveDataSource must be used as an async context manager
(`async with LiveDataSource() as ds: ...`)``.  Every live-mode backtest
therefore stored a per-leader error envelope (and — before P11 Bug 2 — was
still marked ``status='completed'``).  Job
``557eef18-e70b-47fc-bb6e-18519120e72a`` was the wake-up call.

This module locks the fix:

1. ``_datasource_lifecycle`` enters/exits a datasource that implements the
   async-CM protocol (matches ``LiveDataSource``) and is a no-op for one
   that does not (matches ``FixtureDataSource``).
2. ``main()`` actually wraps ``process_pending_jobs`` inside that
   lifecycle context — i.e. by the time ``process_pending_jobs`` runs, the
   datasource has been entered.

No network, no DB: a tiny fake datasource records ``__aenter__`` /
``__aexit__`` calls.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from polyecho.shared.datasource.protocol import DataSource


@pytest.mark.asyncio
async def test_lifecycle_enters_async_context_manager_when_supported() -> None:
    """A datasource that IS an async context manager is entered/exited.

    Mirrors ``LiveDataSource``'s contract (httpx client built in
    ``__aenter__``).  Without this, every live-mode call would raise the
    "must be used as an async context manager" RuntimeError.
    """
    from polyecho.backtest.__main__ import _datasource_lifecycle

    calls: list[str] = []

    class _CMDataSource:
        async def __aenter__(self) -> _CMDataSource:
            calls.append("aenter")
            return self

        async def __aexit__(self, *exc: object) -> None:
            calls.append("aexit")

    # The structural ``DataSource`` Protocol is unrelated to the fake's
    # data-method API surface (the lifecycle adapter only needs the async-CM
    # dunders); cast keeps the call type-correct without forcing the fake to
    # re-implement every read method.
    ds = cast("DataSource", _CMDataSource())
    async with _datasource_lifecycle(ds) as entered:
        # The same value object is yielded (live's __aenter__ returns self).
        assert entered is ds
        assert calls == ["aenter"]
    assert calls == ["aenter", "aexit"]


@pytest.mark.asyncio
async def test_lifecycle_is_passthrough_for_non_context_manager() -> None:
    """A datasource without ``__aenter__`` / ``__aexit__`` is yielded as-is.

    Mirrors ``FixtureDataSource`` — preserves the pre-fix behaviour byte for
    byte so the fixture path is not regressed by the live-path fix.
    """
    from polyecho.backtest.__main__ import _datasource_lifecycle

    class _PlainDataSource:
        # Deliberately no __aenter__ / __aexit__ (fixture-equivalent).
        pass

    ds = cast("DataSource", _PlainDataSource())
    async with _datasource_lifecycle(ds) as entered:
        assert entered is ds


@pytest.mark.asyncio
async def test_main_enters_datasource_before_process_pending_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` must enter the datasource as an async CM *before* calling
    ``process_pending_jobs``.

    The bug was that ``main()`` handed an un-entered ``LiveDataSource``
    straight to ``process_pending_jobs``.  This test captures the call
    ordering via a stand-in datasource and a stub ``process_pending_jobs``.
    """
    import polyecho.backtest.__main__ as main_mod

    events: list[str] = []

    class _RecordingDS:
        async def __aenter__(self) -> _RecordingDS:
            events.append("aenter")
            return self

        async def __aexit__(self, *exc: object) -> None:
            events.append("aexit")

    ds = _RecordingDS()

    def _fake_get_datasource() -> _RecordingDS:
        return ds

    async def _fake_process(datasource: Any, *, once: bool = False) -> None:
        # By the time we land here the datasource MUST already be entered.
        events.append(f"process_pending_jobs(once={once})")
        assert datasource is ds

    monkeypatch.setattr(main_mod, "get_datasource", _fake_get_datasource)
    monkeypatch.setattr(main_mod, "process_pending_jobs", _fake_process)
    monkeypatch.setattr(main_mod, "configure_logging", lambda: None)
    monkeypatch.setattr("sys.argv", ["polyecho.backtest", "--once"])

    await main_mod.main()

    # Strict ordering: enter → process → exit.  A regression that drops the
    # async-with (returning to the pre-P11 bug) would produce
    # ['process_pending_jobs(...)'] with no 'aenter' before it.
    assert events == ["aenter", "process_pending_jobs(once=True)", "aexit"]

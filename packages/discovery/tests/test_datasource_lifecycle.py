"""Regression tests — discovery datasource async-context-manager lifecycle.

Context
-------
``packages/discovery/src/polyecho/discovery/__main__.py`` used to do::

    ds = get_datasource()
    worker = DiscoveryWorker(ds)
    await _run_once(worker)        # or _scheduled_loop(worker)

``LiveDataSource`` builds its ``httpx.AsyncClient`` in ``__aenter__`` and
raises ``RuntimeError: LiveDataSource must be used as an async context
manager`` from ``_ensure_client()`` if any data method is called before
that.  Because the entry-point never wrapped the datasource in ``async
with``, the live path crashed on the first call while the fixture path
(no I/O lifecycle) happened to work — so the bug shipped undetected.

These tests PREVENT recurrence:

* ``TestF0DatasourceContract`` — imports the FROZEN F0 contract module
  ``polyecho.shared.datasource_contract`` and runs its importable
  assertions (a ``LiveDataSource``/``get_datasource()`` used WITHOUT
  ``async with`` must raise the clear ``RuntimeError``).  This module is
  added in ``packages/shared`` OUTSIDE this worktree, so it is RED in the
  isolated base and GREEN in the integrated verify — skipped (not failed)
  when absent so the isolated suite stays green while still proving the
  contract is wired once F0 is present.

* ``TestMainEntersDatasourceAsAsyncCM`` — discovery-specific, uses a
  ``LiveDataSource``-faithful fake whose data methods raise unless
  ``__aenter__`` ran.  ``test_main_*`` FAIL against the pre-fix
  ``__main__`` (datasource never entered → RuntimeError) and PASS after
  the ``datasource_lifecycle`` fix.  ``test_buggy_pattern_is_genuinely
  _caught`` reproduces the OLD code path verbatim and asserts it raises
  — proving the test is non-vacuous (it really detects "datasource used
  without ``async with``", not something incidental).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
#  A LiveDataSource-faithful fake.
#
#  Mirrors the real lifecycle EXACTLY (see
#  packages/shared/src/polyecho/shared/datasource/live.py):
#    * __aenter__  builds the "client" (sets a sentinel) and returns self
#    * __aexit__   tears it down
#    * _ensure_client() raises RuntimeError with the real message if the
#      client was never built (i.e. used without `async with`)
#    * EVERY data method funnels through _ensure_client() first — so a
#      call made without entering the async CM raises, just like live.
# --------------------------------------------------------------------------- #
_LIVE_ERR = (
    "LiveDataSource must be used as an async context manager "
    "(`async with LiveDataSource() as ds: ...`)"
)


class _LiveLikeDataSource:
    """Fake datasource with LiveDataSource's strict async-CM lifecycle."""

    def __init__(self) -> None:
        self._client: object | None = None
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _LiveLikeDataSource:
        self._client = object()  # "build the httpx client"
        self.entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._client = None
        self.exited = True

    def _ensure_client(self) -> object:
        if self._client is None:
            raise RuntimeError(_LIVE_ERR)
        return self._client

    # ---- DataSource protocol surface — every method needs the client ----
    async def get_leaderboard(self, **_: Any) -> list[Any]:
        self._ensure_client()
        return []

    async def get_user_activity(self, *_: Any, **__: Any) -> list[Any]:
        self._ensure_client()
        return []

    async def get_positions(self, *_: Any, **__: Any) -> list[Any]:
        self._ensure_client()
        return []

    async def get_markets_by_volume(self, **_: Any) -> list[Any]:
        self._ensure_client()
        return []

    async def get_market_top_holders(self, *_: Any, **__: Any) -> list[Any]:
        self._ensure_client()
        return []


def _mock_session_scope() -> MagicMock:
    """Mock ``session_scope`` so the pipeline never needs a real DB.

    Matches the existing test-suite convention (see
    ``test_discovery.py::test_run_once_entry_point``).
    """
    mock_scope = MagicMock()
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        )
    )
    mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.add = MagicMock()
    return mock_scope


# --------------------------------------------------------------------------- #
#  (a) F0 contract — imported assertions (RED in isolated base, GREEN
#      integrated; skipped when F0 absent so isolated suite stays green).
# --------------------------------------------------------------------------- #
class TestF0DatasourceContract:
    """Bind discovery to the FROZEN F0 ``datasource_contract`` assertions.

    F0 lives in ``packages/shared`` (OUTSIDE this worktree).  When present
    (integrated verify) these run for real; when absent (isolated base)
    they ``skip`` rather than ``fail`` — the genuine recurrence-guard is
    :class:`TestMainEntersDatasourceAsAsyncCM`, which is fully
    self-contained and needs no F0.
    """

    def test_factory_requires_async_context(self) -> None:
        contract = pytest.importorskip(
            "polyecho.shared.datasource_contract",
            reason="F0 lives in packages/shared (outside this worktree); "
            "asserted by the post-consolidation integrated verify.",
        )
        from polyecho.shared.datasource import get_datasource

        # F0 API (per the frozen contract): asserts a LiveDataSource /
        # get_datasource() used WITHOUT `async with` raises the clear
        # RuntimeError.  Call whichever symbol F0 exposes.
        if hasattr(contract, "assert_requires_async_context"):
            contract.assert_requires_async_context(get_datasource)
        if hasattr(contract, "assert_datasource_factory_contract"):
            contract.assert_datasource_factory_contract()


# --------------------------------------------------------------------------- #
#  (b)+(c) Discovery-specific, self-contained recurrence guard.
# --------------------------------------------------------------------------- #
class TestMainEntersDatasourceAsAsyncCM:
    """``__main__.main()`` MUST enter the datasource as an async CM.

    With the ``_LiveLikeDataSource`` fake, any code path that touches a
    data method without first entering the async context manager raises
    ``RuntimeError`` — exactly the production bug.  These tests therefore
    FAIL against the pre-fix ``__main__`` and PASS after the fix.
    """

    def test_main_once_enters_live_like_datasource(self) -> None:
        """``main(once=True)`` must run the live-like ds through the full
        ``_run_once`` pipeline WITHOUT the lifecycle ``RuntimeError``.
        """
        from polyecho.discovery import __main__ as m

        fake = _LiveLikeDataSource()

        async def _run() -> None:
            with (
                patch.object(m, "get_datasource", return_value=fake),
                patch(
                    "polyecho.discovery.worker.session_scope",
                    _mock_session_scope(),
                ),
            ):
                # Pre-fix: get_datasource() is used directly, _run_once
                # calls get_leaderboard() → _ensure_client() → RuntimeError.
                # Post-fix: datasource_lifecycle enters it → no raise.
                await m.main(once=True)

        asyncio.get_event_loop().run_until_complete(_run())

        assert fake.entered, (
            "datasource was NOT entered as an async context manager — "
            "main() must wrap get_datasource() in `async with` "
            "(LiveDataSource builds its httpx client in __aenter__)."
        )
        assert fake.exited, (
            "datasource async context manager was entered but never "
            "exited — the httpx client would leak."
        )

    def test_main_once_propagates_no_ensure_client_runtimeerror(self) -> None:
        """The specific production failure must not resurface.

        Asserts the exact ``LiveDataSource must be used as an async
        context manager`` ``RuntimeError`` never escapes ``main()``.
        """
        from polyecho.discovery import __main__ as m

        fake = _LiveLikeDataSource()

        async def _run() -> None:
            with (
                patch.object(m, "get_datasource", return_value=fake),
                patch(
                    "polyecho.discovery.worker.session_scope",
                    _mock_session_scope(),
                ),
            ):
                await m.main(once=True)

        try:
            asyncio.get_event_loop().run_until_complete(_run())
        except RuntimeError as exc:  # pragma: no cover - fails pre-fix only
            if "async context manager" in str(exc):
                pytest.fail(
                    "REGRESSION: datasource used without `async with` — "
                    f"{exc}"
                )
            raise

    def test_buggy_pattern_is_genuinely_caught(self) -> None:
        """Non-vacuity proof.

        Reproduce the OLD ``__main__`` body VERBATIM (``ds =
        get_datasource(); ... await _run_once(worker)`` with NO ``async
        with``) and assert it raises the lifecycle ``RuntimeError``.  This
        proves the guard above genuinely detects "datasource used without
        ``async with``" — if the assertion here ever stops raising, the
        recurrence guard would be vacuous.
        """
        from polyecho.discovery import __main__ as m
        from polyecho.discovery.worker import DiscoveryWorker

        fake = _LiveLikeDataSource()

        async def _run_buggy_old_main() -> None:
            # ↓↓↓ EXACT pre-fix code path (no datasource_lifecycle) ↓↓↓
            ds = fake
            worker = DiscoveryWorker(ds)
            await m._run_once(worker)
            # ↑↑↑ this is what shipped broken ↑↑↑

        with patch(
            "polyecho.discovery.worker.session_scope",
            _mock_session_scope(),
        ):
            with pytest.raises(RuntimeError, match="async context manager"):
                asyncio.get_event_loop().run_until_complete(
                    _run_buggy_old_main()
                )

        # Sanity: the buggy path never entered the async CM.
        assert not fake.entered

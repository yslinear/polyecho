"""
tests/test_datasource_contract.py
=================================
Regression infrastructure (user-mandated) that PREVENTS the
live-datasource recurrence: a ``LiveDataSource`` / ``get_datasource()``
result used WITHOUT being entered as an ``async with`` context manager.

Two layers, exactly as briefed:

(a) A direct test that calling a ``LiveDataSource()`` method WITHOUT
    ``async with`` raises the clear ``RuntimeError`` — locks the contract
    here in ``shared``.
(b) Exercises the REUSABLE, shipped helpers in
    ``polyecho.shared.datasource_contract`` that every other package's
    test suite imports to assert the same invariant in one line — so any
    future code path that uses the datasource un-entered is caught by a
    test in whichever package introduced it.

No network, no DB.  ``LiveDataSource`` behaviour is NOT modified — these
only probe its already-existing ``_ensure_client`` guard + the documented
``get_datasource`` contract.
"""

from __future__ import annotations

import pytest

from polyecho.shared.datasource import LiveDataSource
from polyecho.shared.datasource_contract import (
    CONTEXT_MANAGER_ERROR_PATTERN,
    assert_datasource_factory_contract,
    assert_requires_async_context,
)

from .conftest import run


class _LiveSettings:
    datasource = "live"
    polymarket_data_api_url = "https://data-api.polymarket.com"


# ---------------------------------------------------------------------------
# (a) Direct contract lock — the recurrence is a RED test
# ---------------------------------------------------------------------------
class TestLiveDataSourceLifecycleInvariant:
    def test_method_without_async_with_raises_clear_runtimeerror(self) -> None:
        """``LiveDataSource()`` used un-entered → RuntimeError naming the
        async context manager.  This is THE guard against the recurrence."""
        ds = LiveDataSource(settings=_LiveSettings())
        with pytest.raises(RuntimeError, match=CONTEXT_MANAGER_ERROR_PATTERN):
            run(ds.get_leaderboard(limit=1))

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("get_leaderboard", lambda d: d.get_leaderboard(limit=1)),
            ("get_positions", lambda d: d.get_positions("0xabc")),
            (
                "get_user_activity",
                lambda d: d.get_user_activity("0xabc", activity_type=None),
            ),
            (
                "get_markets_by_volume",
                lambda d: d.get_markets_by_volume(limit=1),
            ),
            (
                "get_market_top_holders",
                lambda d: d.get_market_top_holders("0xcond", limit=1),
            ),
        ],
    )
    def test_every_data_method_guarded(self, method: str, call: object) -> None:
        """EVERY public data method (not just one) must enforce the
        lifecycle — a new method added without the guard is caught here."""
        ds = LiveDataSource(settings=_LiveSettings())
        with pytest.raises(RuntimeError, match=CONTEXT_MANAGER_ERROR_PATTERN):
            run(call(ds))  # type: ignore[operator]


# ---------------------------------------------------------------------------
# (b) The reusable shipped helper other packages import
# ---------------------------------------------------------------------------
class TestReusableContractHelper:
    def test_assert_requires_async_context_passes_for_livedatasource(
        self,
    ) -> None:
        # The exact call sites discovery/backtest/push/cli tests will use.
        assert_requires_async_context(
            lambda: LiveDataSource(settings=_LiveSettings())
        )

    def test_factory_contract_assertion_passes(self) -> None:
        """One-liner other packages call; green == contract intact."""
        assert_datasource_factory_contract()

    def test_helper_flags_a_datasource_missing_the_guard(self) -> None:
        """If a fake datasource does NOT enforce ``async with``, the helper
        MUST fail — proves the regression net actually catches the bug it
        exists for (not a vacuous always-pass)."""

        class _Unguarded:
            async def get_leaderboard(self, *, limit: int = 1) -> list[int]:
                return list(range(limit))  # used un-entered, no guard

        with pytest.raises(AssertionError, match="lifecycle guard is MISSING"):
            assert_requires_async_context(lambda: _Unguarded())

    def test_helper_flags_wrong_error_message(self) -> None:
        """A guard that raises but with an unhelpful message is also a
        regression (the message is part of the contract)."""

        class _BadMessage:
            async def get_leaderboard(self, *, limit: int = 1) -> list[int]:
                raise RuntimeError("nope")  # raises, but unhelpful

        with pytest.raises(AssertionError, match="does not\n?.*match"):
            assert_requires_async_context(lambda: _BadMessage())

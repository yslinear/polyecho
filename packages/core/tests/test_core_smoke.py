"""
Import / smoke tests for ``polyecho.core`` (trader-core).

``trader-core`` is a Phase-0 skeleton: its logic raises ``NotImplementedError``
until Phase 4 (mypy-strict is intentionally scoped off for it at the repo
root). These tests are still valuable — they catch *import-time* regressions
(syntax, bad imports, broken dataclasses / enums) and pin the documented
"stub raises NotImplementedError" contract so an accidental silent change is
caught immediately.

NO network / DB: ``get_settings`` is patched so ``TraderCore()`` constructs
without touching the environment.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock, patch

import pytest


def test_package_imports() -> None:
    """The package and its re-exports import cleanly."""
    pkg = importlib.import_module("polyecho.core")
    assert hasattr(pkg, "TraderCore")
    assert hasattr(pkg, "Decision")
    assert set(pkg.__all__) == {"TraderCore", "Decision"}


def test_entrypoint_module_imports() -> None:
    """``python -m polyecho.core`` entry module imports and exposes main()."""
    mod = importlib.import_module("polyecho.core.__main__")
    assert asyncio.iscoroutinefunction(mod.main)


def test_decision_outcome_enum_values() -> None:
    """DecisionOutcome is a StrEnum with the documented members."""
    from polyecho.core.trader import DecisionOutcome

    assert DecisionOutcome.EXECUTE.value == "execute"
    assert DecisionOutcome.SKIP_DAILY_LOSS_LIMIT.value == "skip_daily_loss_limit"
    # StrEnum: members compare/serialise as plain strings
    assert isinstance(DecisionOutcome.EXECUTE, str)
    assert {m.value for m in DecisionOutcome} >= {
        "execute",
        "skip_leader_not_active",
        "skip_daily_loss_limit",
        "skip_max_concurrent",
        "skip_category_not_whitelisted",
        "skip_strategy_drift",
        "skip_insufficient_liquidity",
        "skip_below_min_size",
        "skip_ctf_event_mismatch",
    }


def test_decision_dataclass_defaults_and_should_execute() -> None:
    """Decision is trivially constructible; should_execute reflects outcome."""
    from polyecho.core.trader import Decision, DecisionOutcome

    skip = Decision(outcome=DecisionOutcome.SKIP_MAX_CONCURRENT)
    assert skip.should_execute is False
    assert skip.copy_size_usd == 0.0
    assert skip.metadata == {}

    go = Decision(
        outcome=DecisionOutcome.EXECUTE,
        copy_size_usd=12.5,
        leader_address="0xabc",
        market_id="m1",
        side="BUY",
    )
    assert go.should_execute is True
    assert go.copy_size_usd == 12.5


def test_trader_core_instantiates_with_patched_settings() -> None:
    """TraderCore() constructs without network/DB when settings are patched."""
    with patch("polyecho.core.trader.get_settings", return_value=MagicMock()):
        from polyecho.core.trader import TraderCore

        core = TraderCore()
    assert core is not None


@pytest.mark.asyncio
async def test_trader_core_run_is_stubbed() -> None:
    """Documented contract: run() raises NotImplementedError (Phase 4)."""
    with patch("polyecho.core.trader.get_settings", return_value=MagicMock()):
        from polyecho.core.trader import TraderCore

        core = TraderCore()
    with pytest.raises(NotImplementedError, match="Phase 4"):
        await core.run()


@pytest.mark.asyncio
async def test_trader_core_evaluate_copy_is_stubbed() -> None:
    """Documented contract: evaluate_copy() raises NotImplementedError."""
    with patch("polyecho.core.trader.get_settings", return_value=MagicMock()):
        from polyecho.core.trader import TraderCore

        core = TraderCore()
    with pytest.raises(NotImplementedError, match="Phase 4"):
        await core.evaluate_copy(leader_trade={})


@pytest.mark.asyncio
async def test_risk_helper_stubs_raise() -> None:
    """Every risk-layer helper stub raises NotImplementedError (Phase 4)."""
    from polyecho.core import trader as t

    with pytest.raises(NotImplementedError):
        await t._check_daily_loss("acct", 50.0)
    with pytest.raises(NotImplementedError):
        await t._check_max_concurrent("acct", 3)
    with pytest.raises(NotImplementedError):
        await t._check_category_whitelist("Politics", ["Politics"])
    with pytest.raises(NotImplementedError):
        await t._check_strategy_drift("0xabc")
    with pytest.raises(NotImplementedError):
        await t._check_ctf_event("m1", "0xcond")


@pytest.mark.asyncio
async def test_user_channel_listener_run_is_stubbed() -> None:
    """_UserChannelListener constructs but run() is a Phase-4 stub."""
    from polyecho.core.trader import _UserChannelListener

    listener = _UserChannelListener(["0xabc"], asyncio.Queue())
    with pytest.raises(NotImplementedError, match="Phase 4"):
        await listener.run()


@pytest.mark.asyncio
async def test_trader_core_private_order_stubs_raise() -> None:
    """_place_fok_order / _place_ioc_order / _emit_trade_event are stubs."""
    with patch("polyecho.core.trader.get_settings", return_value=MagicMock()):
        from polyecho.core.trader import TraderCore

        core = TraderCore()
    with pytest.raises(NotImplementedError):
        await core._place_fok_order("m1", "BUY", 1.0, 0.5)
    with pytest.raises(NotImplementedError):
        await core._place_ioc_order("m1", "SELL", 1.0, 0.5)
    with pytest.raises(NotImplementedError):
        await core._emit_trade_event({"id": 1}, "new_trade")

"""
Import / smoke tests for ``polyecho.push`` (web-push-sender).

``web-push-sender`` is a Phase-0 skeleton: its logic raises
``NotImplementedError`` until Phase 5 (mypy-strict is intentionally scoped off
for it at the repo root). These tests guard against import-time regressions
and pin the documented event-type constants + "stub raises
NotImplementedError" contract.

NO network / DB: ``get_settings`` is patched so ``WebPushSender()`` constructs
without touching the environment.
"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import MagicMock, patch

import pytest


def test_package_imports() -> None:
    pkg = importlib.import_module("polyecho.push")
    assert hasattr(pkg, "WebPushSender")
    assert pkg.__all__ == ["WebPushSender"]


def test_entrypoint_module_imports() -> None:
    """``python -m polyecho.push`` entry module imports and exposes main()."""
    mod = importlib.import_module("polyecho.push.__main__")
    assert asyncio.iscoroutinefunction(mod.main)


def test_event_type_constants_and_set() -> None:
    """The seven PRD §5.8 event types are defined and collected in the set."""
    from polyecho.push import sender as s

    expected = {
        "new_trade",
        "position_closed",
        "daily_pnl_summary",
        "risk_triggered",
        "system_error",
        "new_leader_candidate",
        "pending_action",
    }
    assert s.EVENT_NEW_TRADE == "new_trade"
    assert s.EVENT_POSITION_CLOSED == "position_closed"
    assert s.EVENT_DAILY_PNL_SUMMARY == "daily_pnl_summary"
    assert s.EVENT_RISK_TRIGGERED == "risk_triggered"
    assert s.EVENT_SYSTEM_ERROR == "system_error"
    assert s.EVENT_NEW_LEADER_CANDIDATE == "new_leader_candidate"
    assert s.EVENT_PENDING_ACTION == "pending_action"
    assert s.ALL_EVENT_TYPES == frozenset(expected)
    assert isinstance(s.ALL_EVENT_TYPES, frozenset)


def test_sender_instantiates_with_patched_settings() -> None:
    with patch("polyecho.push.sender.get_settings", return_value=MagicMock()):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()
    assert sender is not None


@pytest.mark.asyncio
async def test_sender_run_is_stubbed() -> None:
    """Documented contract: run() raises NotImplementedError (Phase 5)."""
    with patch("polyecho.push.sender.get_settings", return_value=MagicMock()):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await sender.run()


@pytest.mark.asyncio
async def test_sender_push_event_is_stubbed() -> None:
    with patch("polyecho.push.sender.get_settings", return_value=MagicMock()):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()
    with pytest.raises(NotImplementedError, match="Phase 5"):
        await sender.push_event({"event_type": "new_trade"})


@pytest.mark.asyncio
async def test_sender_private_helpers_are_stubbed() -> None:
    """All private DB/delivery helpers raise NotImplementedError (Phase 5)."""
    with patch("polyecho.push.sender.get_settings", return_value=MagicMock()):
        from polyecho.push.sender import WebPushSender

        sender = WebPushSender()
    with pytest.raises(NotImplementedError):
        await sender._fetch_pending_events()
    with pytest.raises(NotImplementedError):
        await sender._fetch_subscriptions()
    with pytest.raises(NotImplementedError):
        sender._send_to_subscription({"endpoint": "x"}, {"title": "t"})
    with pytest.raises(NotImplementedError):
        await sender._mark_pushed(1)
    with pytest.raises(NotImplementedError):
        await sender._remove_expired_subscription("sub-id")

"""Tests for the non-frozen WS reachability probes (polyecho.core.ws_probe).

Contract mirrored from the frozen ``shared.health`` probes: bounded,
fail-soft, NEVER raises, returns a ``ProbeResult``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from polyecho.core import ws_probe
from polyecho.shared.constants import CLOB_WSS_MARKET_URL, CLOB_WSS_USER_URL
from polyecho.shared.health import ProbeResult


class _OkConn:
    """A fake ``websockets.connect(...)`` async context manager (success)."""

    async def __aenter__(self) -> _OkConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _fake_connect_ok(url: str, **kwargs: Any) -> _OkConn:
    return _OkConn()


async def test_probe_ws_user_ok_on_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _capture(url: str, **kwargs: Any) -> _OkConn:
        captured["url"] = url
        return _OkConn()

    monkeypatch.setattr("websockets.connect", _capture)
    res = await ws_probe.probe_polymarket_ws_user()
    assert isinstance(res, ProbeResult)
    assert res.ok is True
    assert captured["url"] == CLOB_WSS_USER_URL
    assert res.latency_ms is not None and res.latency_ms >= 0
    assert res.checked_at is not None


async def test_probe_ws_market_targets_market_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def _capture(url: str, **kwargs: Any) -> _OkConn:
        captured["url"] = url
        return _OkConn()

    monkeypatch.setattr("websockets.connect", _capture)
    res = await ws_probe.probe_polymarket_ws_market()
    assert res.ok is True
    assert captured["url"] == CLOB_WSS_MARKET_URL


async def test_probe_ws_failsoft_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising connect ⇒ ok=False + detail, the probe NEVER raises."""

    def _boom(url: str, **kwargs: Any) -> Any:
        raise OSError("dns/tls down")

    monkeypatch.setattr("websockets.connect", _boom)
    res = await ws_probe.probe_polymarket_ws_user()
    assert res.ok is False
    assert "WS unreachable" in res.detail
    assert CLOB_WSS_USER_URL in res.detail
    assert res.checked_at is not None


async def test_probe_ws_failsoft_when_websockets_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken/absent ``websockets`` import degrades to ok=False."""
    import builtins

    real_import = builtins.__import__

    def _no_ws(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "websockets":
            raise ModuleNotFoundError("No module named 'websockets'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_ws)
    res = await ws_probe.probe_polymarket_ws_user()
    assert res.ok is False
    assert "WS unreachable" in res.detail


async def test_probe_ws_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hanging handshake is cut by the per-probe timeout (ok=False)."""

    class _HangConn:
        async def __aenter__(self) -> Any:
            await asyncio.sleep(30)  # never completes within the bound
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    def _hang(url: str, **kwargs: Any) -> _HangConn:
        return _HangConn()

    monkeypatch.setattr("websockets.connect", _hang)
    monkeypatch.setattr(ws_probe, "WS_PROBE_TIMEOUT_SECONDS", 0.05)
    res = await asyncio.wait_for(ws_probe.probe_polymarket_ws_user(), timeout=5)
    assert res.ok is False
    assert "WS unreachable" in res.detail

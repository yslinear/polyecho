"""
tests/test_polymarket_client_retired_reads.py
=============================================
P0-1 — the four read-only stubs on
:class:`polyecho.shared.polymarket.client.PolymarketClient` are
intentionally retired.

Background
----------
Production reads route through:

* :class:`polyecho.shared.datasource.live.LiveDataSource` for Data-API
  endpoints (``get_leaderboard`` / ``get_user_activity`` /
  ``get_positions``).  It already has retry, a circuit breaker,
  bounded pagination, proxy routing and typed DTOs.
* ``polyecho.core.trader.Trader``'s ``orderbook_for`` callable
  injection for the CLOB orderbook.

The four stubs on ``PolymarketClient`` (``get_leaderboard``,
``get_user_activity``, ``get_positions``, ``get_orderbook``) used to
raise ``NotImplementedError("Phase 1 …")`` which reads as "TODO,
implement later" — exactly the wrong signal for retired code.  They
now raise ``RuntimeError`` with an actionable message naming the
canonical alternative.

These tests pin that behaviour so a future "wire it up" attempt
can't silently downgrade to a NotImplementedError stub again.

``ClobClient`` is patched so no SDK / network runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from polyecho.shared.config import get_settings


class _NoopClob:
    """Stub ClobClient that does nothing — keeps the ctor pure."""

    def __init__(self, **_kw: Any) -> None:  # noqa: D401 — stub
        pass


@pytest.fixture
def _client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Construct a PolymarketClient with a stubbed ClobClient."""
    import polyecho.shared.polymarket.client as mod

    monkeypatch.setattr(mod, "ClobClient", _NoopClob)
    # Avoid pulling in any proxy env from the host.
    for v in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
              "POLYECHO_POLYMARKET_PROXY_URL"):
        monkeypatch.delenv(v, raising=False)
    get_settings.cache_clear()

    return mod.PolymarketClient(
        signature_type=0, funder=None, creds=None, private_key=None
    )


@pytest.mark.asyncio
async def test_get_leaderboard_is_retired(_client: Any) -> None:
    with pytest.raises(RuntimeError) as exc:
        await _client.get_leaderboard()
    msg = str(exc.value)
    # Must NOT be a NotImplementedError-style "Phase N" placeholder.
    assert "intentionally not implemented" in msg
    # Must point callers at the canonical alternative.
    assert "LiveDataSource" in msg
    assert "get_leaderboard" in msg


@pytest.mark.asyncio
async def test_get_user_activity_is_retired(_client: Any) -> None:
    with pytest.raises(RuntimeError) as exc:
        await _client.get_user_activity("0xabc")
    msg = str(exc.value)
    assert "intentionally not implemented" in msg
    assert "LiveDataSource" in msg
    assert "get_user_activity" in msg


@pytest.mark.asyncio
async def test_get_positions_is_retired(_client: Any) -> None:
    with pytest.raises(RuntimeError) as exc:
        await _client.get_positions("0xabc")
    msg = str(exc.value)
    assert "intentionally not implemented" in msg
    assert "LiveDataSource" in msg
    assert "get_positions" in msg


@pytest.mark.asyncio
async def test_get_orderbook_is_retired(_client: Any) -> None:
    with pytest.raises(RuntimeError) as exc:
        await _client.get_orderbook("0xtoken")
    msg = str(exc.value)
    assert "intentionally not implemented" in msg
    # Orderbook points at the trader injection, not LiveDataSource.
    assert "orderbook_for" in msg
    assert "Trader" in msg


@pytest.mark.asyncio
async def test_retired_reads_never_raise_notimplementederror(
    _client: Any,
) -> None:
    """A defensive sweep: none of the four retired reads must raise
    ``NotImplementedError`` — that historical signal meant "TODO" and
    we never want it back on these methods.
    """
    # Each call: (callable, args)
    calls: list[tuple[str, tuple[Any, ...]]] = [
        ("get_leaderboard", ()),
        ("get_user_activity", ("0xabc",)),
        ("get_positions", ("0xabc",)),
        ("get_orderbook", ("0xtoken",)),
    ]
    for name, args in calls:
        meth = getattr(_client, name)
        with pytest.raises(RuntimeError):
            await meth(*args)
        # The above already covers "raised RuntimeError, not
        # NotImplementedError" because pytest.raises(RuntimeError) would
        # NOT catch NotImplementedError (it's a sibling of RuntimeError,
        # not a subclass).  Belt-and-braces explicit check below:
        try:
            await meth(*args)
        except NotImplementedError as exc:  # pragma: no cover — guard
            raise AssertionError(
                f"{name} regressed to NotImplementedError: {exc}"
            ) from exc
        except RuntimeError:
            pass  # expected

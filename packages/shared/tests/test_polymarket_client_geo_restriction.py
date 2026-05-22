"""
tests/test_polymarket_client_geo_restriction.py
===============================================
C5 (PolyHermes deep-dive) — :class:`PolymarketClient` must DETECT a CLOB
geo-restriction rejection (Polymarket geo-restricts trading) and re-raise
it as the dedicated :class:`GeoRestrictionError`, so the trading loop can
trip an emergency-stop instead of treating it as a transient failure.

The detector :func:`is_geo_restriction_error` must be tolerant of wording
yet CONSERVATIVE: a region-block matches, but a transient network blip
must NEVER match (a false halt is an availability bug — though a real
halt is fail-safe).

The ``ClobClient`` is replaced with a stub; no SDK / network runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from polyecho.shared.config import get_settings
from polyecho.shared.polymarket.client import (
    GeoRestrictionError,
    is_geo_restriction_error,
)

# ---------------------------------------------------------------------------
# is_geo_restriction_error — the conservative substring matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        # The canonical Polymarket / PolyHermes wording.
        "Trading restricted in your region",
        "trading is restricted in your region",
        # Tolerant of phrasing variants — restriction + geography words.
        "Orders are blocked in this country",
        "This service is not available in your jurisdiction",
        "Geo-blocked: trading prohibited in your location",
        "Access from your territory is restricted",
        "HTTP 403: trading unavailable in this region",
    ],
)
def test_detects_geo_restriction_messages(message: str) -> None:
    """A region/geo trading block is detected regardless of exact wording."""
    assert is_geo_restriction_error(message) is True
    assert is_geo_restriction_error(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        # Transient network errors — MUST NOT trip the emergency-stop.
        "Connection reset by peer",
        "Read timed out",
        "HTTP 503 Service Unavailable",  # "unavailable" alone — no geo word
        "Temporary failure in name resolution",
        "Max retries exceeded with url",
        # Ordinary CLOB rejections — restriction word, but no geo word.
        "Order size restricted: below minimum notional",
        "insufficient balance",
        "tick size invalid",
        "",  # empty
    ],
)
def test_does_not_false_positive_on_transient_or_unrelated(message: str) -> None:
    """A transient/unrelated error never looks like a geo-restriction.

    The matcher requires BOTH a restriction word AND a geography word, so
    a bare 'unavailable'/'restricted' (no region/country/geo) and every
    network blip stay False — no false-positive halt.
    """
    assert is_geo_restriction_error(message) is False
    assert is_geo_restriction_error(RuntimeError(message)) is False


def test_geo_restriction_error_is_always_detected() -> None:
    """An already-classified :class:`GeoRestrictionError` is always True."""
    assert is_geo_restriction_error(GeoRestrictionError("anything")) is True


def test_geo_restriction_error_is_a_runtimeerror() -> None:
    """Subclass of RuntimeError so existing broad ``except`` still catches it."""
    assert issubclass(GeoRestrictionError, RuntimeError)


# ---------------------------------------------------------------------------
# place_order / cancel_order re-raise a geo-restriction as GeoRestrictionError
# ---------------------------------------------------------------------------


class _GeoBlockedClob:
    """Stub ``ClobClient`` whose trading calls raise a geo-restriction."""

    def __init__(self, **_kw: Any) -> None:
        pass

    def create_and_post_order(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("Trading restricted in your region")

    def cancel_order(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("Trading restricted in your region")


class _TransientFailClob:
    """Stub ``ClobClient`` whose trading calls raise a transient error."""

    def __init__(self, **_kw: Any) -> None:
        pass

    def create_and_post_order(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("Connection reset by peer")


@pytest.fixture
def _clob_geo(monkeypatch: pytest.MonkeyPatch) -> None:
    import polyecho.shared.polymarket.client as mod

    monkeypatch.setattr(mod, "ClobClient", _GeoBlockedClob)
    for v in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "POLYECHO_POLYMARKET_PROXY_URL"):
        monkeypatch.delenv(v, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def _clob_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    import polyecho.shared.polymarket.client as mod

    monkeypatch.setattr(mod, "ClobClient", _TransientFailClob)
    for v in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "POLYECHO_POLYMARKET_PROXY_URL"):
        monkeypatch.delenv(v, raising=False)
    get_settings.cache_clear()


def _make_client() -> Any:
    from polyecho.shared.polymarket.client import PolymarketClient

    return PolymarketClient(signature_type=0, funder=None, creds=None, private_key=None)


@pytest.mark.asyncio
async def test_place_order_reraises_geo_restriction(_clob_geo: None) -> None:
    """A geo-restricted CLOB rejection surfaces as :class:`GeoRestrictionError`."""
    client = _make_client()
    with pytest.raises(GeoRestrictionError, match="geo-restricted"):
        await client.place_order(token_id="0xTOKEN", side="BUY", price=0.5, size=1.0)


@pytest.mark.asyncio
async def test_cancel_order_reraises_geo_restriction(_clob_geo: None) -> None:
    """``cancel_order`` likewise re-classifies a geo-restriction rejection."""
    client = _make_client()
    with pytest.raises(GeoRestrictionError):
        await client.cancel_order("0xORDER")


@pytest.mark.asyncio
async def test_place_order_does_not_reclassify_transient_error(
    _clob_transient: None,
) -> None:
    """A transient network error propagates UNCHANGED — NOT a geo halt.

    The executor's ordinary re-sign/retry path must still see a plain
    error for a network blip; only a real region-block becomes a
    :class:`GeoRestrictionError`.
    """
    client = _make_client()
    with pytest.raises(RuntimeError) as excinfo:
        await client.place_order(token_id="0xTOKEN", side="BUY", price=0.5, size=1.0)
    assert not isinstance(excinfo.value, GeoRestrictionError)
    assert "Connection reset" in str(excinfo.value)

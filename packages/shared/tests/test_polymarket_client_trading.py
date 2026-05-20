"""
tests/test_polymarket_client_trading.py
=======================================
Phase-4 W2 — :class:`polyecho.shared.polymarket.client.PolymarketClient`'s
trading chokepoint methods ``place_order`` / ``cancel_order`` must:

1. **Delegate to the SDK** — pass the order args through
   ``py_clob_client_v2.ClobClient.create_and_post_order(
   order_args, options, order_type)`` / ``cancel_order(OrderPayload)``
   verbatim, with **no** hand-rolled EIP-712 / HMAC / rounding
   (brief §2/§8/§9 — the SDK owns signing).
2. Validate the ``order_type`` string *before* any network work so a
   typo surfaces as :class:`ValueError` without touching the SDK.

Per-op operator confirmation lives at the web layer (``dangerous-action``
+ account-import passphrase); the SDK wrapper is no longer behind a
global trading lock.

The ``ClobClient`` is replaced with a recording stub; no SDK / network
runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from polyecho.shared.config import get_settings


class _RecordingClob:
    """Stub ``ClobClient``: records the args of trading calls."""

    instances: list[_RecordingClob] = []

    def __init__(self, **_kw: Any) -> None:
        type(self).instances.append(self)
        self.create_calls: list[tuple[Any, Any, Any]] = []
        self.cancel_calls: list[Any] = []
        # Default canned return — exercised when the wrapper delegates.
        self.create_return: dict[str, Any] = {
            "orderID": "0xORDER",
            "status": "matched",
        }
        self.cancel_return: dict[str, Any] = {"orderID": "0xORDER"}

    def create_and_post_order(
        self,
        order_args: Any,
        options: Any = None,
        order_type: Any = None,
        post_only: bool = False,
        defer_exec: bool = False,
    ) -> dict[str, Any]:
        del post_only, defer_exec  # kept for SDK shape compatibility
        self.create_calls.append((order_args, options, order_type))
        return self.create_return

    def cancel_order(self, payload: Any) -> dict[str, Any]:
        self.cancel_calls.append(payload)
        return self.cancel_return


@pytest.fixture
def _clob(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClob]:
    import polyecho.shared.polymarket.client as mod

    _RecordingClob.instances = []
    monkeypatch.setattr(mod, "ClobClient", _RecordingClob)
    # Avoid pulling host proxy env into the test process.
    for v in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "POLYECHO_POLYMARKET_PROXY_URL",
    ):
        monkeypatch.delenv(v, raising=False)
    get_settings.cache_clear()
    return _RecordingClob


def _make_client(signature_type: int = 0) -> Any:
    """Construct a ``PolymarketClient`` against the stubbed SDK.

    ``signature_type`` is passed straight through to the SDK — the
    place_order / cancel_order paths are intentionally *sigType-blind*
    (the SDK's internal hasher dispatches on 0/1/2/3), so the same
    delegation tests cover all four wallet modes.
    """
    from polyecho.shared.polymarket.client import PolymarketClient

    return PolymarketClient(
        signature_type=signature_type,
        funder=None,
        creds=None,
        private_key=None,
    )


# ---------------------------------------------------------------------------
# Delegation to py-clob-client-v2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_delegates_to_sdk_with_options(
    _clob: type[_RecordingClob],
) -> None:
    """``place_order`` forwards verbatim to
    ``ClobClient.create_and_post_order(order_args, options, order_type)``
    with per-market ``tick_size`` + ``neg_risk`` in ``options`` (brief
    §2 — static tick was the PolyHermes gap)."""
    client = _make_client()
    result = await client.place_order(
        token_id="0xTOKEN",
        side="BUY",
        price=0.42,
        size=12.5,
        order_type="FOK",
        tick_size="0.001",
        neg_risk=True,
    )

    sdk = _RecordingClob.instances[-1]
    assert len(sdk.create_calls) == 1
    order_args, options, order_type = sdk.create_calls[0]

    # OrderArgs is the SDK's OrderArgsV2 dataclass — assert the fields
    # we set went through unchanged (no hand-rolled rounding here; the
    # SDK does it inside create_order).
    assert order_args.token_id == "0xTOKEN"
    assert order_args.side == "BUY"
    assert order_args.price == 0.42
    assert order_args.size == 12.5

    # Per-market options.
    assert options.tick_size == "0.001"
    assert options.neg_risk is True

    # SDK OrderType string ("FOK"); we should map straight through.
    assert order_type == "FOK"

    # And the SDK return is propagated.
    assert result == {"orderID": "0xORDER", "status": "matched"}


@pytest.mark.asyncio
async def test_place_order_defaults_match_brief_defaults(
    _clob: type[_RecordingClob],
) -> None:
    """Default ``order_type=FOK`` (brief §2 taker default), default
    ``tick_size="0.01"``, default ``neg_risk=False``."""
    client = _make_client()
    await client.place_order(
        token_id="0xTOKEN", side="SELL", price=0.6, size=1.0
    )

    sdk = _RecordingClob.instances[-1]
    order_args, options, order_type = sdk.create_calls[0]
    assert order_args.side == "SELL"
    assert order_type == "FOK"
    assert options.tick_size == "0.01"
    assert options.neg_risk is False


@pytest.mark.asyncio
async def test_place_order_rejects_unknown_order_type(
    _clob: type[_RecordingClob],
) -> None:
    """A typo in ``order_type`` raises :class:`ValueError` *before* any
    SDK call (a typo is a programming bug, not a money decision)."""
    client = _make_client()
    with pytest.raises(ValueError, match="Unknown order_type"):
        await client.place_order(
            token_id="0xTOKEN",
            side="BUY",
            price=0.5,
            size=1.0,
            order_type="LIMIT",  # not a real SDK OrderType
        )

    sdk = _RecordingClob.instances[-1]
    assert sdk.create_calls == []  # SDK never touched


@pytest.mark.asyncio
async def test_cancel_order_delegates_to_sdk_with_orderpayload(
    _clob: type[_RecordingClob],
) -> None:
    """``cancel_order`` forwards verbatim to
    ``ClobClient.cancel_order(OrderPayload(orderID=...))``."""
    client = _make_client()
    result = await client.cancel_order("0xORDER_42")

    sdk = _RecordingClob.instances[-1]
    assert len(sdk.cancel_calls) == 1
    payload = sdk.cancel_calls[0]
    # SDK requires an OrderPayload dataclass, not a bare str.
    assert payload.orderID == "0xORDER_42"
    assert result == {"orderID": "0xORDER"}


# ---------------------------------------------------------------------------
# sigType coverage — delegation is mode-agnostic (SDK dispatches 0/1/2/3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("signature_type", [0, 1, 2, 3])
async def test_place_order_delegates_for_all_signature_types(
    _clob: type[_RecordingClob],
    signature_type: int,
) -> None:
    """The wrapper does NOT branch on ``signature_type`` — sigType
    routing lives inside the SDK's hasher.  All four wallet modes
    (0/EOA, 1/POLY_PROXY, 2/POLY_GNOSIS_SAFE, 3/POLY_1271) must reach
    the same ``create_and_post_order`` call site with identical args."""
    client = _make_client(signature_type=signature_type)
    await client.place_order(
        token_id="0xTOKEN", side="BUY", price=0.5, size=1.0
    )

    sdk = _RecordingClob.instances[-1]
    assert len(sdk.create_calls) == 1
    order_args, _options, order_type = sdk.create_calls[0]
    assert order_args.token_id == "0xTOKEN"
    assert order_type == "FOK"

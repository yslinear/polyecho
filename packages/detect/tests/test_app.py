"""Tests for the polyecho.detect aiohttp service.

Uses :mod:`aiohttp.test_utils` (TestServer + TestClient) directly rather
than the `pytest-aiohttp` plugin (not in our frozen uv.lock). pytest-
asyncio's `asyncio_mode = "auto"` (root pyproject) is enough — every
`async def test_*` is auto-awaited.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from polyecho.detect.app import W3_KEY, make_app
from polyecho.shared.polymarket.wallet import (
    WalletMode,
    deposit_wallet_address,
    magic_proxy_address,
)

# Fixed test EOA used across multiple tests for byte-for-byte parity checks
# against the shared wallet module. Using a real-looking but obviously-fake
# address (all zeros + 1) so anyone reading the test sees it's a fixture.
_TEST_EOA = "0x0000000000000000000000000000000000000001"


# ── Helpers ──────────────────────────────────────────────────────────────────
class _StubW3Eth:
    """Structural fake for ``w3.eth`` that satisfies
    :func:`polyecho.shared.polymarket.wallet.make_safe_proxy_resolver`.

    The resolver does
    ``w3.eth.contract(address, abi).functions.computeProxyAddress(eoa).call()``
    and awaits the result. We thread a single ``behaviour`` callable through
    so each test can decide whether to return a fixed address, raise, or
    hang.
    """

    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    def contract(self, address: str, abi: Any) -> _StubContract:
        return _StubContract(self._behaviour)


class _StubContract:
    def __init__(self, behaviour: Any) -> None:
        self.functions = _StubFunctions(behaviour)


class _StubFunctions:
    def __init__(self, behaviour: Any) -> None:
        self._behaviour = behaviour

    def computeProxyAddress(self, eoa: str) -> _StubCallable:  # noqa: N802 (mirror Solidity ABI)
        return _StubCallable(self._behaviour, eoa)


class _StubCallable:
    def __init__(self, behaviour: Any, eoa: str) -> None:
        self._behaviour = behaviour
        self._eoa = eoa

    async def call(self) -> str:
        result = self._behaviour(self._eoa)
        if asyncio.iscoroutine(result):
            return await result  # type: ignore[no-any-return]
        return result  # type: ignore[no-any-return]


class _StubW3:
    def __init__(self, behaviour: Any) -> None:
        self.eth = _StubW3Eth(behaviour)


def _make_app_with_stub(behaviour: Any) -> Any:
    """Build a detect app with a stubbed w3 client (no real RPC)."""
    app = make_app(rpc_url="http://stub.invalid")
    app[W3_KEY] = _StubW3(behaviour)
    return app


@pytest.fixture
async def client() -> Any:
    """Default client: SAFE resolver returns a fixed canned address."""

    def _ok(eoa: str) -> str:
        return "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    app = _make_app_with_stub(_ok)
    async with TestClient(TestServer(app)) as c:
        yield c


# ── Tests ────────────────────────────────────────────────────────────────────
async def test_healthz_returns_ok(client: TestClient[Any, Any]) -> None:
    """/healthz returns 200 ``{"ok": true}`` for the compose healthcheck."""
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True}


async def test_detect_with_valid_eoa_returns_4_candidates(client: TestClient[Any, Any]) -> None:
    """Happy path: all four sig-type candidates present, addresses lowercased."""
    resp = await client.get("/detect", params={"eoa": _TEST_EOA})
    assert resp.status == 200
    body = await resp.json()

    for key in ("sig0", "sig1", "sig2", "sig3"):
        assert key in body, f"missing {key}"

    assert body["sig0"] == {"addr": _TEST_EOA, "mode": WalletMode.EOA, "derived": False}
    assert body["sig1"]["addr"] == magic_proxy_address(_TEST_EOA)
    assert body["sig1"]["mode"] == WalletMode.MAGIC
    assert body["sig1"]["derived"] is True
    assert body["sig2"]["addr"] == "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert body["sig2"]["mode"] == WalletMode.SAFE
    assert body["sig3"]["addr"] == deposit_wallet_address(_TEST_EOA)
    assert body["sig3"]["mode"] == WalletMode.POLY_1271
    assert body["sig3"]["derived"] is True

    # Every address is lowercased.
    for sig_key in ("sig0", "sig1", "sig2", "sig3"):
        addr = body[sig_key]["addr"]
        assert addr is None or addr == addr.lower(), f"{sig_key} not lowercased"


async def test_detect_with_invalid_eoa_returns_400(client: TestClient[Any, Any]) -> None:
    """Garbage in → 400 + generic error, and the input is NEVER echoed."""
    garbage = "totally-not-an-eoa-but-could-be-log-poison"
    resp = await client.get("/detect", params={"eoa": garbage})
    assert resp.status == 400
    body = await resp.json()
    assert body == {"error": "invalid eoa"}
    # Critical: the offending value must not appear anywhere in the body.
    assert garbage not in str(body)


async def test_detect_with_missing_eoa_returns_400(client: TestClient[Any, Any]) -> None:
    """No `eoa` query string → 400. Same generic body."""
    resp = await client.get("/detect")
    assert resp.status == 400
    body = await resp.json()
    assert body == {"error": "invalid eoa"}


async def test_detect_with_short_hex_returns_400(client: TestClient[Any, Any]) -> None:
    """0x + 38 hex chars (1 byte short) is rejected — regex enforces exact 40."""
    resp = await client.get("/detect", params={"eoa": "0x" + "ab" * 19})
    assert resp.status == 400


async def test_detect_safe_rpc_failure_fail_soft() -> None:
    """SAFE resolver raises → sig2.addr=null, error present, other 3 intact.

    Verifies the fail-soft promise: sig0/1/3 still render correctly even
    when Polygon RPC is on fire.
    """

    def _boom(eoa: str) -> str:
        raise RuntimeError("simulated RPC fail — should NOT leak into response")

    app = _make_app_with_stub(_boom)
    async with TestClient(TestServer(app)) as c:
        resp = await c.get("/detect", params={"eoa": _TEST_EOA})
        assert resp.status == 200
        body = await resp.json()

        # sig2 fail-soft
        assert body["sig2"]["addr"] is None
        assert body["sig2"]["error"] == "RPC error"
        assert body["sig2"]["mode"] == WalletMode.SAFE
        # Generic — must not leak the underlying exception text.
        assert "simulated RPC fail" not in str(body)

        # Other 3 still computed correctly
        assert body["sig0"]["addr"] == _TEST_EOA
        assert body["sig1"]["addr"] == magic_proxy_address(_TEST_EOA)
        assert body["sig3"]["addr"] == deposit_wallet_address(_TEST_EOA)


async def test_detect_safe_rpc_timeout_fail_soft() -> None:
    """SAFE resolver hangs > 5s → sig2 has timeout error, other 3 intact.

    Uses a sleep that resolves after the timeout budget so the
    ``asyncio.wait_for`` path is exercised.  The actual sleep duration is
    short enough that pytest doesn't stall — we monkey-patch the timeout
    constant via a custom app to keep the test fast.
    """
    # Monkeypatch the timeout to 50ms by importing + temporarily mutating.
    # We instead build a hanging resolver and use a short asyncio timeout
    # by adjusting the constant at module level for the duration of this
    # test.
    from polyecho.detect import app as app_mod

    async def _hang(eoa: str) -> str:
        await asyncio.sleep(10.0)
        return "0xshouldneverreturn"

    original_timeout = app_mod._SAFE_RPC_TIMEOUT_S
    app_mod._SAFE_RPC_TIMEOUT_S = 0.05  # 50ms — well under default
    try:
        app = _make_app_with_stub(_hang)
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/detect", params={"eoa": _TEST_EOA})
            assert resp.status == 200
            body = await resp.json()
            assert body["sig2"]["addr"] is None
            assert body["sig2"]["error"] == "RPC timeout"
            # Other 3 still good
            assert body["sig0"]["addr"] == _TEST_EOA
            assert body["sig1"]["addr"] == magic_proxy_address(_TEST_EOA)
            assert body["sig3"]["addr"] == deposit_wallet_address(_TEST_EOA)
    finally:
        app_mod._SAFE_RPC_TIMEOUT_S = original_timeout


async def test_detect_parity_vs_python_wallet(client: TestClient[Any, Any]) -> None:
    """sig1 / sig3 outputs match direct calls to the shared wallet module.

    Parity guard: if anyone "optimises" detect by re-implementing CREATE2
    locally and drifts from the canonical wallet module, this test trips.
    """
    resp = await client.get("/detect", params={"eoa": _TEST_EOA})
    assert resp.status == 200
    body = await resp.json()
    assert body["sig1"]["addr"] == magic_proxy_address(_TEST_EOA)
    assert body["sig3"]["addr"] == deposit_wallet_address(_TEST_EOA)


async def test_detect_accepts_mixed_case_eoa(client: TestClient[Any, Any]) -> None:
    """An EIP-55 checksum-cased EOA is accepted and lowercased in the response."""
    mixed = "0x000000000000000000000000000000000000DEAD"
    resp = await client.get("/detect", params={"eoa": mixed})
    assert resp.status == 200
    body = await resp.json()
    assert body["sig0"]["addr"] == mixed.lower()

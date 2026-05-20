"""
tests/test_polymarket_wallet.py
===============================
P4 — SAFE (sigType 2) proxy resolver via Polygon RPC.

Covers :func:`polyecho.shared.polymarket.wallet.make_safe_proxy_resolver`:
the injected :data:`SafeProxyResolver` that issues a *view*
``computeProxyAddress(eoa)`` ``eth_call`` to the verified Polymarket Safe
Proxy Factory (brief §9 / :data:`constants.SAFE_PROXY_FACTORY` =
``0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b``).

The factory is the on-chain "Polymarket Contract Proxy Factory"
(PolygonScan-labelled "Polymarket : Safe Proxy Factory") whose source at
``Polymarket/proxy-factories`` exposes
``computeProxyAddress(address user) external view returns (address)``.

Tests pass a structural fake for the ``w3`` injection — no real RPC, no
network — so they are fully deterministic and CI-safe.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from polyecho.shared import constants
from polyecho.shared.polymarket.wallet import (
    SAFE_PROXY_FACTORY_ABI,
    AccountLike,
    WalletMode,
    deposit_wallet_address,
    magic_proxy_address,
    make_safe_proxy_resolver,
    resolve_wallet,
)

from .conftest import run

# Well-formed throwaway addresses (checksum-able).
_EOA = "0x000000000000000000000000000000000000dEaD"
_SAFE = "0x00000000000000000000000000000000DeadBeef"


# ---------------------------------------------------------------------------
# Structural fake for AsyncWeb3 — only what the resolver touches:
#     w3.eth.contract(address, abi).functions.computeProxyAddress(user).call()
#     (call() is awaited)
# ---------------------------------------------------------------------------
class _FakeCall:
    def __init__(self, ret: str) -> None:
        self._ret = ret

    async def call(self) -> str:
        return self._ret


class _FakeFunctions:
    def __init__(self, ret: str, captured: dict[str, Any]) -> None:
        self._ret = ret
        self._captured = captured

    def computeProxyAddress(self, user: str) -> _FakeCall:  # noqa: N802 - ABI name
        self._captured["user"] = user
        return _FakeCall(self._ret)


class _FakeContract:
    def __init__(self, ret: str, captured: dict[str, Any]) -> None:
        self.functions = _FakeFunctions(ret, captured)


class _FakeEth:
    def __init__(self, ret: str, captured: dict[str, Any]) -> None:
        self._ret = ret
        self._captured = captured

    def contract(self, *, address: str, abi: list[dict[str, Any]]) -> _FakeContract:
        self._captured["factory"] = address
        self._captured["abi"] = abi
        return _FakeContract(self._ret, self._captured)


class _FakeWeb3:
    def __init__(self, ret: str) -> None:
        self.captured: dict[str, Any] = {}
        self.eth = _FakeEth(ret, self.captured)


class _SafeAccount:
    """Minimal :class:`AccountLike` for the SAFE path.

    Annotated as ``| None`` to match the AccountLike protocol's mutable
    attribute invariance (mypy treats ``Protocol`` attrs as invariant), so
    callers cast through :class:`AccountLike` at the call site — mirrors
    the established pattern in ``test_phase4_contracts.py``.
    """

    def __init__(self, *, funder: str | None = None) -> None:
        self.eoa_address: str = _EOA
        self.funder_address: str | None = funder
        self.signature_type: int | None = 2
        self.wallet_mode: str | None = WalletMode.SAFE


# ---------------------------------------------------------------------------
# make_safe_proxy_resolver — direct behaviour
# ---------------------------------------------------------------------------
class TestSafeProxyResolverFactory:
    def test_abi_is_view_computeProxyAddress(self) -> None:
        """The wired ABI is exactly the one view function we call."""
        assert len(SAFE_PROXY_FACTORY_ABI) == 1
        fn = SAFE_PROXY_FACTORY_ABI[0]
        assert fn["name"] == "computeProxyAddress"
        assert fn["stateMutability"] == "view"
        assert fn["type"] == "function"
        # single ``address user`` input, single ``address`` output
        assert fn["inputs"][0]["type"] == "address"  # type: ignore[index]
        assert fn["outputs"][0]["type"] == "address"  # type: ignore[index]

    def test_factory_address_is_brief_section9_default(self) -> None:
        """The default factory is :data:`constants.SAFE_PROXY_FACTORY` (brief §9)."""
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3)
        addr = run(resolver(_EOA))
        assert addr == _SAFE.lower()
        # The factory address passed to ``eth.contract(...)`` is the
        # verified brief §9 constant (checksum-cased).
        assert w3.captured["factory"].lower() == constants.SAFE_PROXY_FACTORY.lower()

    def test_resolver_passes_checksum_eoa_to_factory(self) -> None:
        """The user arg is the checksum-cased EOA — what ``eth_call`` expects."""
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3)
        run(resolver(_EOA))
        # ``to_checksum_address`` is deterministic — the captured user must
        # match the EIP-55 checksum of the input EOA.  Use eth_utils (the
        # same lib the resolver uses) so the test stays web3-free.
        from eth_utils.address import to_checksum_address

        assert w3.captured["user"] == to_checksum_address(_EOA)

    def test_resolver_normalises_returned_address_lowercase(self) -> None:
        """Returned funder is lowercase — matches the WalletBinding contract."""
        # Return checksum-cased; resolver must lowercase.
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3)
        addr = run(resolver(_EOA))
        assert addr == _SAFE.lower()
        assert addr.startswith("0x")
        assert len(addr) == 42

    def test_resolver_rejects_invalid_returned_address(self) -> None:
        """A garbage RPC return fails closed (not silently propagated)."""
        w3 = _FakeWeb3("not-an-address")
        resolver = make_safe_proxy_resolver(w3)
        with pytest.raises(ValueError, match="valid EVM address"):
            run(resolver(_EOA))

    def test_resolver_rejects_invalid_eoa_input(self) -> None:
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3)
        with pytest.raises(ValueError, match="valid EVM address"):
            run(resolver("garbage"))

    def test_resolver_respects_factory_override(self) -> None:
        """Tests may override the factory (still validated as a real address)."""
        override = "0x000000000000000000000000000000000000BEEF"
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3, factory_address=override)
        run(resolver(_EOA))
        assert w3.captured["factory"].lower() == override.lower()


# ---------------------------------------------------------------------------
# Integration with resolve_wallet — SAFE path now self-resolves via RPC.
# ---------------------------------------------------------------------------
class TestSafeIntegrationWithResolveWallet:
    def test_safe_derives_via_injected_rpc(self) -> None:
        """With no explicit funder, SAFE uses ``computeProxyAddress`` via RPC."""
        w3 = _FakeWeb3(_SAFE)
        resolver = make_safe_proxy_resolver(w3)
        b = run(
            resolve_wallet(
                cast(AccountLike, _SafeAccount()),
                safe_proxy_resolver=resolver,
            )
        )
        assert b.signature_type == 2
        assert b.funder == _SAFE.lower()
        assert b.derived is True
        assert b.wallet_mode == WalletMode.SAFE

    def test_safe_prefers_explicit_operator_funder_over_rpc(self) -> None:
        """An explicit operator funder short-circuits BEFORE the RPC call
        (the standing rule, brief §6/§9).
        """
        op = "0x00000000000000000000000000000000CafeBabe"
        w3 = _FakeWeb3(_SAFE)
        # If the RPC were called, captured would be populated.
        resolver = make_safe_proxy_resolver(w3)
        b = run(
            resolve_wallet(
                cast(AccountLike, _SafeAccount(funder=op)),
                safe_proxy_resolver=resolver,
            )
        )
        assert b.funder == op.lower()
        assert b.derived is False  # operator-confirmed, not derived
        # And the resolver was NOT invoked.
        assert w3.captured == {}

    def test_safe_fail_closed_without_resolver_or_funder(self) -> None:
        """No resolver + no explicit funder ⇒ fail-closed (unchanged)."""
        with pytest.raises(ValueError, match="fail-closed"):
            run(resolve_wallet(cast(AccountLike, _SafeAccount())))


# ---------------------------------------------------------------------------
# Deposit-Wallet (sig 3 / POLY_1271) CREATE2 parity — Solady ERC-1967 clone
# ---------------------------------------------------------------------------
class TestDepositWalletDerivation:
    """Pin :func:`deposit_wallet_address` byte-for-byte.

    The address is a CREATE2 over ``DEPOSIT_WALLET_FACTORY`` of a Solady
    (v0.1.26) ERC-1967 minimal-proxy clone whose init code embeds two
    32-byte Solady literals + the 2-byte ``0x6009`` separator.  Any
    single bit-flip in those literals or in the abi.encode of
    ``(factory, walletId)`` changes the produced address — so this test
    is the primary safety-net for the sig-3 wire path.

    Expected value (``0xfaea0f08159fcf2f573fe24e9e989b0d48f7651b`` for
    EOA ``0x1111…1111``) was computed by the PolyEcho Python derivation
    against the verbatim brief / TS byte literals on 2026-05-20.  A
    production deposit-wallet sniffed from PolygonScan should ALSO match
    this scheme; if it does not, this test will tripwire the operator
    *before* a real /submit is posted.  See
    ``docs/reference/polymarket-sig1-sig3-redeem-wire.md`` for the full
    byte-literal derivation and the TS source link.
    """

    # Deterministic test EOA — easy to reproduce in any environment.
    _TEST_EOA = "0x1111111111111111111111111111111111111111"
    _EXPECTED_DEPOSIT_WALLET = "0xfaea0f08159fcf2f573fe24e9e989b0d48f7651b"

    def test_deposit_wallet_address_parity(self) -> None:
        addr = deposit_wallet_address(self._TEST_EOA)
        assert addr == self._EXPECTED_DEPOSIT_WALLET

    def test_deposit_wallet_rejects_garbage_eoa(self) -> None:
        with pytest.raises(ValueError, match="valid EVM address"):
            deposit_wallet_address("not-an-address")

    def test_deposit_wallet_is_deterministic(self) -> None:
        # CREATE2 must be deterministic.  Two calls for the same EOA
        # MUST return the same lowercased address.
        a = deposit_wallet_address(self._TEST_EOA)
        b = deposit_wallet_address(self._TEST_EOA.lower())
        assert a == b == self._EXPECTED_DEPOSIT_WALLET

    def test_magic_proxy_byte_stable_for_same_eoa(self) -> None:
        """Sig-1 (Magic) derivation regression — current brief constants
        must yield a stable byte-identical output for the same test EOA,
        so any future tweak to ``MAGIC_PROXY_FACTORY`` /
        ``MAGIC_PROXY_INIT_CODE_HASH`` is caught.
        """
        # Locked Python output for EOA 0x1111…1111 using the verified
        # brief §9 constants (factory + initCodeHash).
        expected = "0xf537a2b3159593a425e2fa8f5ba3bd3080d4a18a"
        assert magic_proxy_address(self._TEST_EOA) == expected

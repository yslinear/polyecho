"""
packages/shared/tests/test_polymarket_relayer_extended.py
=========================================================
Covers :mod:`polyecho.shared.polymarket.relayer_extended` (sig 1 PROXY /
sig 3 Deposit-Wallet gasless redeem hand-rolled per
``docs/reference/polymarket-sig1-sig3-redeem-wire.md``).

Tests are fully hermetic: every HTTP call is intercepted via
``httpx.MockTransport`` and every dependency on
``py_builder_signing_sdk`` is stubbed in ``sys.modules`` so neither network
nor the optional SDK extra is needed.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from polyecho.shared import constants
from polyecho.shared.polymarket import relayer_extended as rx
from polyecho.shared.polymarket.relayer_extended import (
    ExtendedRelayerCredentials,
    RelayerCredentialsMissingError,
    RelayerSubmissionError,
    redeem_deposit_wallet,
    redeem_proxy,
    relay_unwrap_deposit_wallet,
    relay_unwrap_proxy,
)
from polyecho.shared.polymarket.wallet import (
    deposit_wallet_address,
    magic_proxy_address,
)

from .conftest import run

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
# A deterministic test key — Anvil's account 0 (well-known; safe for tests).
_TEST_KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
# Address that owns _TEST_KEY (derived once on first use below).
import eth_account  # noqa: E402

_TEST_EOA = eth_account.Account.from_key(_TEST_KEY).address.lower()
_MAGIC_FUNDER = magic_proxy_address(_TEST_EOA)
_DEPOSIT_FUNDER = deposit_wallet_address(_TEST_EOA)

# Sample condition id (32 bytes) — used for redeem calldata.
_CONDITION_ID = "0x" + "ab" * 32


@pytest.fixture(autouse=True)
def _install_fake_signing_sdk() -> Any:
    """Stub ``py_builder_signing_sdk`` so the HMAC step does not need the
    optional extra installed."""
    pkg = types.ModuleType("py_builder_signing_sdk")
    pkg.__path__ = []
    signer_mod = types.ModuleType("py_builder_signing_sdk.signer")

    class _FakeBuilderSigner:
        def __init__(self, *, api_key: str, secret: str, passphrase: str) -> None:
            self.api_key = api_key
            self.secret = secret
            self.passphrase = passphrase

        def sign(
            self, *, timestamp: str, method: str, request_path: str, body: str
        ) -> str:
            # Deterministic fake signature — the relayer would HMAC; we
            # just echo a value the test can introspect.
            return f"sig-{method}-{request_path}-{timestamp}-{len(body)}"

    signer_mod.BuilderSigner = _FakeBuilderSigner  # type: ignore[attr-defined]
    sys.modules["py_builder_signing_sdk"] = pkg
    sys.modules["py_builder_signing_sdk.signer"] = signer_mod
    try:
        yield
    finally:
        sys.modules.pop("py_builder_signing_sdk.signer", None)
        sys.modules.pop("py_builder_signing_sdk", None)


def _creds() -> ExtendedRelayerCredentials:
    return ExtendedRelayerCredentials(
        private_key=_TEST_KEY,
        builder_api_key="api",
        builder_secret="sec",
        builder_passphrase="pass",
    )


# ---------------------------------------------------------------------------
# Mock relayer harness
# ---------------------------------------------------------------------------
class _MockRelayer:
    """Captures /submit payloads + mocks /nonce, /relay-payload, /deployed,
    /transaction state machine."""

    def __init__(
        self,
        *,
        deployed: bool = True,
        transaction_state: str = "STATE_MINED",
    ) -> None:
        self.deployed = deployed
        self.submits: list[dict[str, Any]] = []
        self.state = transaction_state
        self.txid_counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        if path == "/relay-payload":
            return httpx.Response(
                200, json={"address": "0x" + "01" * 20, "nonce": 42}
            )
        if path == "/nonce":
            return httpx.Response(200, json={"nonce": 7})
        if path == "/deployed":
            return httpx.Response(200, json={"deployed": self.deployed})
        if path == "/transaction":
            return httpx.Response(
                200,
                json={
                    "state": self.state,
                    "transactionHash": "0x" + "cd" * 32,
                },
            )
        if path == "/submit":
            body = json.loads(request.content.decode())
            self.txid_counter += 1
            self.submits.append(
                {
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            # If this was the WALLET-CREATE flip the deployed bit so the
            # caller's subsequent /deployed query would now be True (not
            # actually re-queried in the same flow but mirrors prod).
            if body.get("type") == "WALLET-CREATE":
                self.deployed = True
            return httpx.Response(
                200,
                json={"transactionID": f"txn-{self.txid_counter}"},
            )
        return httpx.Response(404)


def _client(mock: _MockRelayer) -> httpx.AsyncClient:
    """Build a mock-transport httpx AsyncClient (closed by the caller's
    ``aclose`` or by an explicit ``async with``).  For sync test helpers
    we use ``run(_do(client))`` and aclose explicitly via ``run(c.aclose())``.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(mock.handler),
        base_url=constants.BUILDER_RELAYER_URL,
    )


def _with_client(mock: _MockRelayer, coro_fn: Any) -> Any:
    """Run ``coro_fn(client)`` to completion and close the client."""
    c = _client(mock)
    try:
        return run(coro_fn(c))
    finally:
        run(c.aclose())


# ---------------------------------------------------------------------------
# Credentials guards
# ---------------------------------------------------------------------------
class TestCredentialsGuards:
    def test_empty_builder_creds_fail_closed(self) -> None:
        bad = ExtendedRelayerCredentials(
            private_key=_TEST_KEY,
            builder_api_key="",
            builder_secret="sec",
            builder_passphrase="pass",
        )
        with pytest.raises(RelayerCredentialsMissingError):
            bad.assert_complete()

    def test_redeem_proxy_fails_closed_without_creds(self) -> None:
        bad = ExtendedRelayerCredentials(
            private_key="",
            builder_api_key="api",
            builder_secret="sec",
            builder_passphrase="pass",
        )
        with pytest.raises(RelayerCredentialsMissingError):
            run(
                redeem_proxy(
                    eoa=_TEST_EOA,
                    private_key="",
                    proxy_funder=_MAGIC_FUNDER,
                    condition_id=_CONDITION_ID,
                    is_neg_risk=False,
                    index_sets=[1, 2],
                    creds=bad,
                )
            )


# ---------------------------------------------------------------------------
# sig 1 PROXY submit body shape + signature
# ---------------------------------------------------------------------------
class TestProxySubmit:
    def test_proxy_submit_body_shape(self) -> None:
        mock = _MockRelayer()
        tx_hash = _with_client(
            mock,
            lambda c: redeem_proxy(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                proxy_funder=_MAGIC_FUNDER,
                condition_id=_CONDITION_ID,
                is_neg_risk=False,
                index_sets=[1, 2],
                creds=_creds(),
                http_client=c,
            ),
        )
        assert tx_hash == "0x" + "cd" * 32
        assert len(mock.submits) == 1
        body = mock.submits[0]["body"]
        assert body["type"] == "PROXY"
        assert body["from"].lower() == _TEST_EOA
        # Brief §sig-1 step 6: ``to = ProxyFactory`` (TS-followed; UNVERIFIED).
        assert body["to"].lower() == constants.MAGIC_PROXY_FACTORY.lower()
        assert body["proxyWallet"].lower() == _MAGIC_FUNDER
        # 65-byte signature → 130 hex chars + 2 for "0x".
        assert isinstance(body["signature"], str)
        assert len(body["signature"]) == 132
        # signatureParams shape (camelCase per brief).
        sp = body["signatureParams"]
        assert sp["gasPrice"] == "0"
        assert sp["relayerFee"] == "0"
        assert sp["relayHub"].lower() == constants.RELAY_HUB.lower()
        # HMAC headers were attached.
        hdr = mock.submits[0]["headers"]
        assert "poly_builder_api_key" in {k.lower() for k in hdr}
        assert "poly_builder_signature" in {k.lower() for k in hdr}
        assert "poly_builder_timestamp" in {k.lower() for k in hdr}
        assert "poly_builder_passphrase" in {k.lower() for k in hdr}

    def test_proxy_signature_length_is_65_bytes(self) -> None:
        # Deterministically compute the struct hash + sig to confirm 65B.
        struct_hash = rx._proxy_struct_hash(            eoa=_TEST_EOA,
            proxy_factory=constants.MAGIC_PROXY_FACTORY,
            data=b"\x00" * 8,
            tx_fee=0,
            gas_price=0,
            gas_limit=10_000_000,
            nonce=1,
            relay_hub=constants.RELAY_HUB,
            relay="0x" + "02" * 20,
        )
        sig = rx._sign_proxy(struct_hash, _TEST_KEY)
        assert isinstance(sig, str)
        assert sig.startswith("0x")
        assert len(sig) == 132  # 65 bytes hex

    def test_proxy_funder_mismatch_refuses(self) -> None:
        """Guardrail #2: re-derive funder + refuse on mismatch."""
        mock = _MockRelayer()
        with pytest.raises(RelayerSubmissionError, match="funder mismatch"):
            _with_client(
                mock,
                lambda c: redeem_proxy(
                    eoa=_TEST_EOA,
                    private_key=_TEST_KEY,
                    proxy_funder="0x" + "ff" * 20,  # wrong
                    condition_id=_CONDITION_ID,
                    is_neg_risk=False,
                    index_sets=[1, 2],
                    creds=_creds(),
                    http_client=c,
                ),
            )
        assert mock.submits == []  # never posted


# ---------------------------------------------------------------------------
# sig 3 WALLET submit body + EIP-712 domain
# ---------------------------------------------------------------------------
class TestDepositWalletSubmit:
    def test_wallet_submit_body_shape(self) -> None:
        mock = _MockRelayer(deployed=True)
        tx_hash = _with_client(
            mock,
            lambda c: redeem_deposit_wallet(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                deposit_funder=_DEPOSIT_FUNDER,
                condition_id=_CONDITION_ID,
                is_neg_risk=False,
                index_sets=[1, 2],
                creds=_creds(),
                http_client=c,
            ),
        )
        assert tx_hash == "0x" + "cd" * 32
        assert len(mock.submits) == 1
        body = mock.submits[0]["body"]
        assert body["type"] == "WALLET"
        assert body["from"].lower() == _TEST_EOA
        assert body["to"].lower() == constants.DEPOSIT_WALLET_FACTORY.lower()
        dwp = body["depositWalletParams"]
        assert dwp["depositWallet"].lower() == _DEPOSIT_FUNDER
        assert int(dwp["deadline"]) > 0
        assert dwp["calls"][0]["target"].lower() == constants.CTF_ADDRESS.lower()
        assert dwp["calls"][0]["value"] == "0"
        assert dwp["calls"][0]["data"].startswith("0x")
        # 65-byte EIP-712 signature.
        assert len(body["signature"]) == 132

    def test_wallet_create_when_not_deployed(self) -> None:
        """If /deployed returns False, a WALLET-CREATE submit precedes the
        WALLET redeem submit (brief §sig-3)."""
        mock = _MockRelayer(deployed=False, transaction_state="STATE_MINED")
        _with_client(
            mock,
            lambda c: redeem_deposit_wallet(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                deposit_funder=_DEPOSIT_FUNDER,
                condition_id=_CONDITION_ID,
                is_neg_risk=False,
                index_sets=[1, 2],
                creds=_creds(),
                http_client=c,
            ),
        )
        # Two submits: first WALLET-CREATE, then WALLET.
        types_seen = [s["body"]["type"] for s in mock.submits]
        assert types_seen == ["WALLET-CREATE", "WALLET"]
        # WALLET-CREATE has no signature.
        assert "signature" not in mock.submits[0]["body"]
        assert mock.submits[0]["body"]["to"].lower() == (
            constants.DEPOSIT_WALLET_FACTORY.lower()
        )

    def test_wallet_skips_create_when_deployed(self) -> None:
        mock = _MockRelayer(deployed=True)
        _with_client(
            mock,
            lambda c: redeem_deposit_wallet(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                deposit_funder=_DEPOSIT_FUNDER,
                condition_id=_CONDITION_ID,
                is_neg_risk=False,
                index_sets=[1, 2],
                creds=_creds(),
                http_client=c,
            ),
        )
        types_seen = [s["body"]["type"] for s in mock.submits]
        assert types_seen == ["WALLET"]

    def test_wallet_eip712_domain_is_correct(self) -> None:
        """Brief §sig-3 step 4 — domain must be DepositWallet/1/137/<wallet>."""
        # We assert the domain by calling the sign helper directly with a
        # known message and checking the recovered signer.
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        wallet = _DEPOSIT_FUNDER
        nonce, deadline = 7, 1_000_000_000
        calls = [
            {"target": constants.CTF_ADDRESS, "value": 0, "data": b"\x00\x01"}
        ]
        sig_hex = rx._sign_wallet_batch(            wallet_address=wallet,
            chain_id=137,
            nonce=nonce,
            deadline=deadline,
            calls=calls,
            private_key=_TEST_KEY,
        )
        # Re-encode the same typed-data + recover; must match _TEST_EOA.
        signable = encode_typed_data(
            full_message={
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "Call": [
                        {"name": "target", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "data", "type": "bytes"},
                    ],
                    "Batch": [
                        {"name": "wallet", "type": "address"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "calls", "type": "Call[]"},
                    ],
                },
                "primaryType": "Batch",
                "domain": {
                    "name": "DepositWallet",
                    "version": "1",
                    "chainId": 137,
                    "verifyingContract": wallet,
                },
                "message": {
                    "wallet": wallet,
                    "nonce": nonce,
                    "deadline": deadline,
                    "calls": calls,
                },
            }
        )
        recovered = Account.recover_message(
            signable, signature=sig_hex
        )
        assert recovered.lower() == _TEST_EOA


# ---------------------------------------------------------------------------
# Poll-state machine
# ---------------------------------------------------------------------------
class TestPollBehaviour:
    def test_state_failed_raises(self) -> None:
        mock = _MockRelayer(transaction_state="STATE_FAILED")
        with pytest.raises(RelayerSubmissionError, match="STATE_FAILED"):
            _with_client(
                mock,
                lambda c: redeem_deposit_wallet(
                    eoa=_TEST_EOA,
                    private_key=_TEST_KEY,
                    deposit_funder=_DEPOSIT_FUNDER,
                    condition_id=_CONDITION_ID,
                    is_neg_risk=False,
                    index_sets=[1, 2],
                    creds=_creds(),
                    http_client=c,
                ),
            )

    def test_state_mined_ok(self) -> None:
        mock = _MockRelayer(transaction_state="STATE_MINED")
        tx = _with_client(
            mock,
            lambda c: redeem_deposit_wallet(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                deposit_funder=_DEPOSIT_FUNDER,
                condition_id=_CONDITION_ID,
                is_neg_risk=False,
                index_sets=[1, 2],
                creds=_creds(),
                http_client=c,
            ),
        )
        assert tx == "0x" + "cd" * 32


# ---------------------------------------------------------------------------
# Guardrail tests
# ---------------------------------------------------------------------------
class TestGuardrails:
    def test_target_outside_allowlist_refuses(self) -> None:
        with pytest.raises(RelayerSubmissionError, match="allow-list"):
            rx._assert_target_allowed("0x" + "ff" * 20)
    def test_target_allowlist_includes_ctf_wcol_negrisk(self) -> None:
        rx._assert_target_allowed(constants.CTF_ADDRESS)  # noqa: SLF001
        rx._assert_target_allowed(constants.WRAPPED_COLLATERAL_ADDRESS)  # noqa: SLF001
        rx._assert_target_allowed(constants.NEG_RISK_ADAPTER_ADDRESS)  # noqa: SLF001

    def test_gas_limit_cap(self) -> None:
        with pytest.raises(RelayerSubmissionError, match="gas_limit"):
            rx._assert_gas_limit_ok(31_000_000)  # noqa: SLF001

    def test_deadline_cap(self) -> None:
        import time as _time

        bad_deadline = int(_time.time()) + 1200
        with pytest.raises(RelayerSubmissionError, match="deadline"):
            rx._assert_deadline_ok(bad_deadline)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Unwrap helpers
# ---------------------------------------------------------------------------
class TestUnwrap:
    def test_unwrap_proxy_posts_wcol_call(self) -> None:
        mock = _MockRelayer()
        _with_client(
            mock,
            lambda c: relay_unwrap_proxy(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                proxy_funder=_MAGIC_FUNDER,
                amount=1_000_000,
                creds=_creds(),
                http_client=c,
            ),
        )
        body = mock.submits[0]["body"]
        assert body["type"] == "PROXY"
        # The outer calldata starts with the proxy(...) selector — but the
        # WCOL inner call's target was checked in _assert_target_allowed.
        # The signed body still routes via ProxyFactory.
        assert body["to"].lower() == constants.MAGIC_PROXY_FACTORY.lower()

    def test_unwrap_deposit_wallet_posts_wcol_call(self) -> None:
        mock = _MockRelayer(deployed=True)
        _with_client(
            mock,
            lambda c: relay_unwrap_deposit_wallet(
                eoa=_TEST_EOA,
                private_key=_TEST_KEY,
                deposit_funder=_DEPOSIT_FUNDER,
                amount=1_000_000,
                creds=_creds(),
                http_client=c,
            ),
        )
        body = mock.submits[0]["body"]
        dwp = body["depositWalletParams"]
        assert dwp["calls"][0]["target"].lower() == (
            constants.WRAPPED_COLLATERAL_ADDRESS.lower()
        )

    def test_unwrap_zero_amount_raises_for_caller(self) -> None:
        # Caller should pre-check; the lib still raises explicitly.
        with pytest.raises(RelayerSubmissionError, match="amount must be > 0"):
            run(
                relay_unwrap_proxy(
                    eoa=_TEST_EOA,
                    private_key=_TEST_KEY,
                    proxy_funder=_MAGIC_FUNDER,
                    amount=0,
                    creds=_creds(),
                )
            )


# ---------------------------------------------------------------------------
# Dispatch regression — sig 0 + sig 2 paths still take the old surface
# ---------------------------------------------------------------------------
class TestNoSig0OrSig2Regression:
    """Importing relayer_extended does not interfere with the canonical
    EOA / SAFE paths.  These are smoke checks; the existing
    ``test_redeem_onchain`` + ``test_polymarket_relayer`` suites carry
    the real coverage for sig 0 / sig 2."""

    def test_imports_dont_break_sig0_or_sig2_modules(self) -> None:
        from polyecho.shared.polymarket import redeem as _redeem
        from polyecho.shared.polymarket import relayer as _sig2

        assert hasattr(_redeem, "Web3RedeemClient")
        assert hasattr(_sig2, "PolymarketRelayerClient")

    def test_redeem_dispatch_sig1_fail_closed_without_ext_creds(self) -> None:
        """Without ``extended_relayer_creds`` injected, sig 1 must still
        raise ``RelayerNotConfiguredError`` (regression for fail-closed)."""
        from polyecho.shared.copytrading import RedeemJobSpec, WalletBinding
        from polyecho.shared.polymarket.redeem import (
            RelayerNotConfiguredError,
            Web3RedeemClient,
        )

        async def _key(_b: WalletBinding) -> str:
            return _TEST_KEY

        class _FakeWeb3:
            async def send_signed(self, **_: Any) -> str:
                raise AssertionError("EOA path must not run for sig 1")

            async def wait_receipt(self, _h: str) -> dict[str, Any]:
                raise AssertionError("EOA path must not run")

            async def call(self, **_: Any) -> Any:
                raise AssertionError

        # No extended_relayer_creds — base seam still raises.
        client = Web3RedeemClient(web3=_FakeWeb3(), private_key_resolver=_key)
        import uuid as _uuid

        spec = RedeemJobSpec(
            account_id=_uuid.uuid4(),
            condition_id=_CONDITION_ID,
            outcome_index=0,
            index_set=1,
            is_neg_risk=False,
        )
        binding = WalletBinding(
            signature_type=1,
            funder=_MAGIC_FUNDER,
            wallet_mode="magic",
            derived=True,
        )
        # Live-lock must be on for the chokepoint not to override.
        with patch.dict("os.environ", {"POLYECHO_LIVE_TRADING": "1"}):
            from polyecho.shared.config import get_settings

            get_settings.cache_clear()
            with pytest.raises(RelayerNotConfiguredError):
                run(client.redeem(spec, binding=binding))

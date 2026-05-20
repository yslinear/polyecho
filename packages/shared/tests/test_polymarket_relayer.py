"""
packages/shared/tests/test_polymarket_relayer.py
================================================
Phase-4 P3 — Builder-Relayer client for gasless SAFE redeem (brief §9).

Covers :mod:`polyecho.shared.polymarket.relayer` and the
``Web3RedeemClient._relay_*`` dispatch wiring in
:mod:`polyecho.shared.polymarket.redeem`.

Scope (matches the P3 brief):

* Calldata encoding is byte-stable across calls (regression-lock for
  ``redeemPositions`` and ``unwrap`` selectors).
* :class:`PolymarketRelayerClient` raises
  :class:`RelayerCredentialsMissingError` when any builder header or the
  EOA private key is empty.
* :class:`PolymarketRelayerClient.submit_safe_transaction` posts the
  expected ``SafeTransaction(to, data, value=0, op=Call)`` to the SDK
  (verified via a fake ``RelayClient`` injected into the module's lazy
  import path).
* :class:`PolymarketRelayerClient` retries transient errors and
  surfaces deterministic 4xx as :class:`RelayerSubmissionError`.
* ``Web3RedeemClient`` dispatch:
    - sig 0 (EOA) still uses the on-chain ``Web3Backend`` path (sig 0
      behaviour MUST NOT change in P3).
    - sig 2 (SAFE) **with** a relayer ⇒ ``relay_redeem`` / ``relay_unwrap``
      drive the relayer; the result carries the relayer ``transaction_hash``.
    - sig 1 (MAGIC) ⇒ ``RelayerNotConfiguredError`` even with a relayer
      injected (SDK exposes no PROXY surface).
    - sig 3 (POLY_1271) ⇒ ``RelayerNotConfiguredError`` even with a
      relayer injected (no SDK surface).
* When the SDK package is not installed the relayer module still
  imports cleanly (`RelayerNotAvailableError` is raised only on first
  use, not at import time).
"""

from __future__ import annotations

import sys
import types
import uuid
from typing import Any

import pytest

from polyecho.shared import constants
from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    WalletBinding,
)
from polyecho.shared.polymarket.redeem import (
    RelayerNotConfiguredError,
    Web3RedeemClient,
)
from polyecho.shared.polymarket.relayer import (
    PolymarketRelayerClient,
    RelayerCredentials,
    RelayerCredentialsMissingError,
    RelayerSubmissionError,
    encode_redeem_positions_calldata,
    encode_unwrap_calldata,
    relay_redeem,
    relay_unwrap,
)

from .conftest import run

# ---------------------------------------------------------------------------
# Fakes mirroring the FROZEN py-builder-relayer-client surface (verified
# against the SDK source 2026-05-20).
# ---------------------------------------------------------------------------


class _FakeRelayerResponse:
    """Mirrors ``ClientRelayerTransactionResponse``."""

    def __init__(self, *, transaction_id: str, transaction_hash: str) -> None:
        self.transaction_id = transaction_id
        self.transaction_hash = transaction_hash

    def wait(self) -> dict[str, Any]:
        # SDK returns the terminal transaction dict, including state.
        return {
            "id": self.transaction_id,
            "transactionHash": self.transaction_hash,
            "state": "STATE_CONFIRMED",
        }


class _FakeRelayClient:
    """Mirrors ``py_builder_relayer_client.client.RelayClient``."""

    def __init__(self, *, fail_4xx: bool = False, transient_then_ok: bool = False) -> None:
        self.executed: list[dict[str, Any]] = []
        self.nonce_calls: list[tuple[str, str]] = []
        self.deployed_calls: list[str] = []
        self._fail_4xx = fail_4xx
        self._transient_then_ok = transient_then_ok
        self._attempts = 0

    def get_deployed(self, address: str) -> dict[str, Any]:
        self.deployed_calls.append(address)
        return {"deployed": True}

    def get_nonce(self, signer_address: str, signer_type: str) -> dict[str, Any]:
        self.nonce_calls.append((signer_address, signer_type))
        return {"nonce": 7}

    def execute(self, transactions: list[Any], metadata: Any = None) -> Any:
        self._attempts += 1
        if self._fail_4xx:
            raise _make_api_exception(status_code=400, msg="bad nonce")
        if self._transient_then_ok and self._attempts == 1:
            raise _make_api_exception(status_code=503, msg="upstream down")
        first = transactions[0]
        self.executed.append(
            {
                "to": first.to,
                "data": first.data,
                "value": first.value,
                "operation": first.operation,
                "metadata": metadata,
            }
        )
        return _FakeRelayerResponse(
            transaction_id=f"txn-{self._attempts}",
            transaction_hash="0x" + "cd" * 32,
        )


# The relayer module imports the SDK lazily inside its methods.  We
# install fake submodules in ``sys.modules`` so the lazy imports resolve
# to our fakes without touching the real network.


def _install_fake_sdk(relay_client: _FakeRelayClient) -> None:
    """Install fake ``py_builder_relayer_client`` + ``py_builder_signing_sdk``
    modules into ``sys.modules`` so the lazy imports inside
    :class:`PolymarketRelayerClient` resolve to our fakes."""
    # --- py_builder_relayer_client ---------------------------------------
    pkg = types.ModuleType("py_builder_relayer_client")
    pkg.__path__ = []

    client_mod = types.ModuleType("py_builder_relayer_client.client")

    class _RelayClientWrap:
        def __init__(self, **_: Any) -> None:
            pass

        # Delegate every call to the shared fake (lets us assert on it).
        def __getattr__(self, name: str) -> Any:
            return getattr(relay_client, name)

    client_mod.RelayClient = _RelayClientWrap  # type: ignore[attr-defined]

    models_mod = types.ModuleType("py_builder_relayer_client.models")

    class _OpType:
        Call = 0
        DelegateCall = 1

    class _SafeTransaction:
        def __init__(self, *, to: str, operation: Any, data: str, value: str) -> None:
            self.to = to
            self.operation = operation
            self.data = data
            self.value = value

    models_mod.OperationType = _OpType  # type: ignore[attr-defined]
    models_mod.SafeTransaction = _SafeTransaction  # type: ignore[attr-defined]

    exc_mod = types.ModuleType("py_builder_relayer_client.exceptions")

    class _RelayerApiException(Exception):
        def __init__(
            self, *, status_code: int | None = None, error_msg: str | None = None
        ) -> None:
            super().__init__(error_msg or "")
            self.status_code = status_code
            self.error_msg = error_msg

    exc_mod.RelayerApiException = _RelayerApiException  # type: ignore[attr-defined]
    exc_mod.RelayerClientException = Exception  # type: ignore[attr-defined]

    # --- py_builder_signing_sdk.config -----------------------------------
    signing_pkg = types.ModuleType("py_builder_signing_sdk")
    signing_pkg.__path__ = []
    config_mod = types.ModuleType("py_builder_signing_sdk.config")

    class _BuilderConfig:
        def __init__(self, *, api_key: str, secret: str, passphrase: str) -> None:
            self.api_key = api_key
            self.secret = secret
            self.passphrase = passphrase

    config_mod.BuilderConfig = _BuilderConfig  # type: ignore[attr-defined]

    sys.modules["py_builder_relayer_client"] = pkg
    sys.modules["py_builder_relayer_client.client"] = client_mod
    sys.modules["py_builder_relayer_client.models"] = models_mod
    sys.modules["py_builder_relayer_client.exceptions"] = exc_mod
    sys.modules["py_builder_signing_sdk"] = signing_pkg
    sys.modules["py_builder_signing_sdk.config"] = config_mod


def _uninstall_fake_sdk() -> None:
    for m in [
        "py_builder_relayer_client",
        "py_builder_relayer_client.client",
        "py_builder_relayer_client.models",
        "py_builder_relayer_client.exceptions",
        "py_builder_signing_sdk",
        "py_builder_signing_sdk.config",
    ]:
        sys.modules.pop(m, None)


def _make_api_exception(*, status_code: int, msg: str) -> Exception:
    """Build the same exception type that's in ``sys.modules`` for the
    fake SDK; otherwise fall back to a marker class with ``status_code``
    so the retry path still surfaces it."""
    exc_mod = sys.modules.get("py_builder_relayer_client.exceptions")
    if exc_mod is not None and hasattr(exc_mod, "RelayerApiException"):
        exc: Exception = exc_mod.RelayerApiException(
            status_code=status_code, error_msg=msg
        )
        return exc
    err = RuntimeError(msg)
    err.status_code = status_code  # type: ignore[attr-defined]
    return err


@pytest.fixture
def fake_sdk() -> Any:
    relay = _FakeRelayClient()
    _install_fake_sdk(relay)
    try:
        yield relay
    finally:
        _uninstall_fake_sdk()


def _creds() -> RelayerCredentials:
    return RelayerCredentials(
        private_key="0x" + "11" * 32,
        builder_api_key="ak",
        builder_secret="sk",
        builder_passphrase="pass",
    )


# ---------------------------------------------------------------------------
# Calldata encoding regression-lock
# ---------------------------------------------------------------------------


def test_encode_redeem_positions_selector_and_shape() -> None:
    """Selector ``0x01b7037c`` for
    ``redeemPositions(address,bytes32,bytes32,uint256[])`` (Gnosis CTF) —
    byte-stable regression-lock so a future eth_abi/eth_utils bump can't
    silently shift the calldata."""
    data = encode_redeem_positions_calldata(
        collateral=constants.PUSD_ADDRESS,
        parent_collection_id=constants.PARENT_COLLECTION_ID,
        condition_id="0x" + "ab" * 32,
        index_sets=[1],
    )
    assert data.startswith("0x01b7037c"), data[:12]
    # Selector (4) + address(32) + bytes32(32) + bytes32(32) +
    # uint256[] offset(32) + length(32) + element(32) = 196 bytes
    # => 392 hex chars + 8 selector + 2 ("0x") = 402 chars.
    assert len(data) == 2 + 4 * 2 + 6 * 32 * 2


def test_encode_unwrap_selector_matches_wrapped_collateral_abi() -> None:
    """Selector ``0x39f47693`` for ``unwrap(address,uint256)``."""
    data = encode_unwrap_calldata(
        to_address="0x000000000000000000000000000000000000dead",
        amount=12345,
    )
    assert data.startswith("0x39f47693"), data[:12]


# ---------------------------------------------------------------------------
# Credential & lazy-import guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["private_key", "builder_api_key", "builder_secret", "builder_passphrase"],
)
def test_missing_credential_field_raises(field: str) -> None:
    kw: dict[str, str] = {
        "private_key": "0x" + "11" * 32,
        "builder_api_key": "ak",
        "builder_secret": "sk",
        "builder_passphrase": "pass",
    }
    kw[field] = ""
    with pytest.raises(RelayerCredentialsMissingError, match=field):
        PolymarketRelayerClient(RelayerCredentials(**kw))  # type: ignore[arg-type]


def test_relayer_module_imports_without_sdk_installed() -> None:
    """:mod:`polyecho.shared.polymarket.relayer` must import cleanly even
    when ``py-builder-relayer-client`` is missing (the SDK is an optional
    extra; the module fails closed only on first use)."""
    import importlib

    mod = importlib.import_module("polyecho.shared.polymarket.relayer")
    assert hasattr(mod, "PolymarketRelayerClient")
    # Construction with valid creds must not import the SDK.
    _uninstall_fake_sdk()
    client = PolymarketRelayerClient(_creds())
    assert client is not None


# ---------------------------------------------------------------------------
# submit_safe_transaction → SDK contract
# ---------------------------------------------------------------------------


def test_submit_safe_transaction_calls_sdk_with_safetransaction(fake_sdk: _FakeRelayClient) -> None:
    client = PolymarketRelayerClient(_creds())
    result = run(
        client.submit_safe_transaction(
            to=constants.CTF_ADDRESS,
            data="0xdeadbeef",
            metadata="probe",
        )
    )
    assert len(fake_sdk.executed) == 1
    call = fake_sdk.executed[0]
    assert call["to"] == constants.CTF_ADDRESS
    assert call["data"] == "0xdeadbeef"
    assert call["value"] == "0"
    assert call["metadata"] == "probe"
    assert result["transaction_hash"].startswith("0x")
    assert result["state"] == "STATE_CONFIRMED"


def test_submit_retries_on_transient_5xx(fake_sdk: _FakeRelayClient) -> None:
    fake_sdk._transient_then_ok = True
    client = PolymarketRelayerClient(_creds())
    result = run(
        client.submit_safe_transaction(to=constants.CTF_ADDRESS, data="0x")
    )
    # First attempt 503, second succeeds.
    assert result["state"] == "STATE_CONFIRMED"


def test_submit_4xx_surfaces_immediately(fake_sdk: _FakeRelayClient) -> None:
    fake_sdk._fail_4xx = True
    client = PolymarketRelayerClient(_creds())
    with pytest.raises(RelayerSubmissionError, match="4xx"):
        run(client.submit_safe_transaction(to=constants.CTF_ADDRESS, data="0x"))


def test_get_nonce_and_is_deployed(fake_sdk: _FakeRelayClient) -> None:
    client = PolymarketRelayerClient(_creds())
    nonce = run(client.get_safe_nonce("0xabc"))
    assert nonce == 7
    deployed = run(client.is_safe_deployed("0xsafe"))
    assert deployed is True
    assert fake_sdk.nonce_calls == [("0xabc", "SAFE")]
    assert fake_sdk.deployed_calls == ["0xsafe"]


# ---------------------------------------------------------------------------
# High-level relay_redeem / relay_unwrap helpers
# ---------------------------------------------------------------------------


_SAFE = WalletBinding(
    signature_type=2,
    funder="0x000000000000000000000000000000000000beef",
    wallet_mode="safe",
    derived=True,
)
_MAGIC = WalletBinding(
    signature_type=1,
    funder="0x000000000000000000000000000000000000abcd",
    wallet_mode="magic",
    derived=True,
)
_POLY_1271 = WalletBinding(
    signature_type=3,
    funder="0x0000000000000000000000000000000000001271",
    wallet_mode="poly_1271",
    derived=True,
)
_EOA = WalletBinding(
    signature_type=0,
    funder="0x000000000000000000000000000000000000dead",
    wallet_mode="eoa",
    derived=False,
)


def _spec(*, is_neg_risk: bool) -> RedeemJobSpec:
    return RedeemJobSpec(
        account_id=uuid.uuid4(),
        condition_id="0x" + "ab" * 32,
        outcome_index=0,
        index_set=1,
        is_neg_risk=is_neg_risk,
    )


def test_relay_redeem_safe_pusd(fake_sdk: _FakeRelayClient) -> None:
    relayer = PolymarketRelayerClient(_creds())
    result = run(relay_redeem(relayer, _spec(is_neg_risk=False), binding=_SAFE))
    assert result.status == RedeemJobStatus.CONFIRMED
    call = fake_sdk.executed[0]
    assert call["to"] == constants.CTF_ADDRESS
    assert call["data"].startswith("0x01b7037c")  # redeemPositions selector
    # Verify the collateral arg (pUSD) is the first 32-byte word after selector.
    addr_word = call["data"][2 + 8 : 2 + 8 + 64]
    assert addr_word.lower().endswith(constants.PUSD_ADDRESS[2:].lower())


def test_relay_redeem_safe_negrisk_uses_wcol(fake_sdk: _FakeRelayClient) -> None:
    relayer = PolymarketRelayerClient(_creds())
    result = run(relay_redeem(relayer, _spec(is_neg_risk=True), binding=_SAFE))
    assert result.status == RedeemJobStatus.CONFIRMED
    call = fake_sdk.executed[0]
    addr_word = call["data"][2 + 8 : 2 + 8 + 64]
    assert addr_word.lower().endswith(
        constants.WRAPPED_COLLATERAL_ADDRESS[2:].lower()
    )


def test_relay_redeem_magic_rejected_no_sdk_surface(fake_sdk: _FakeRelayClient) -> None:
    relayer = PolymarketRelayerClient(_creds())
    with pytest.raises(RelayerSubmissionError, match="signature_type=1"):
        run(relay_redeem(relayer, _spec(is_neg_risk=False), binding=_MAGIC))
    assert fake_sdk.executed == []


def test_relay_unwrap_safe(fake_sdk: _FakeRelayClient) -> None:
    relayer = PolymarketRelayerClient(_creds())
    result = run(relay_unwrap(relayer, binding=_SAFE, amount=1000))
    assert result.status == RedeemJobStatus.CONFIRMED
    call = fake_sdk.executed[0]
    assert call["to"] == constants.WRAPPED_COLLATERAL_ADDRESS
    assert call["data"].startswith("0x39f47693")  # unwrap selector


def test_relay_unwrap_zero_amount_is_a_noop(fake_sdk: _FakeRelayClient) -> None:
    relayer = PolymarketRelayerClient(_creds())
    result = run(relay_unwrap(relayer, binding=_SAFE, amount=0))
    assert result.status == RedeemJobStatus.CONFIRMED
    assert "skipped" in (result.detail or "")
    assert fake_sdk.executed == []


# ---------------------------------------------------------------------------
# Web3RedeemClient dispatch wiring — sig 0 unchanged, sig 2 concrete,
# sig 1 / sig 3 still fail-closed even with a relayer present.
# ---------------------------------------------------------------------------


class _FakeWeb3:
    """Same shape as test_redeem_onchain._FakeWeb3 — the sig 0 EOA path
    MUST continue to use Web3Backend, untouched by P3."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    async def send_signed(self, **kw: Any) -> str:
        self.sent.append(kw)
        return "0x" + "ab" * 32

    async def wait_receipt(self, _tx_hash: str) -> dict[str, Any]:
        return {"status": 1, "blockNumber": 1}

    async def call(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if kw["fn_name"] == "balanceOf":
            return 0
        return constants.MAX_UINT256


async def _key(_binding: WalletBinding) -> str:
    return "0x" + "11" * 32


def _enable_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy no-op shim — the global trading lock was removed.

    Retained so the existing test bodies that pass ``monkeypatch`` through
    keep their argument list / call style.
    """
    del monkeypatch


def test_sig0_eoa_path_unchanged_when_relayer_injected(
    fake_sdk: _FakeRelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a relayer and confirm sig 0 still goes through Web3Backend
    (the P3 work MUST NOT alter the sig 0 path)."""
    _enable_lock(monkeypatch)
    relayer = PolymarketRelayerClient(_creds())
    web3 = _FakeWeb3()
    client = Web3RedeemClient(
        web3=web3, private_key_resolver=_key, relayer=relayer
    )
    result = run(client.redeem(_spec(is_neg_risk=False), binding=_EOA))
    assert result.status == RedeemJobStatus.CONFIRMED
    # EOA path uses the web3 backend; relayer SDK must be untouched.
    assert len(web3.sent) == 1
    assert web3.sent[0]["fn_name"] == "redeemPositions"
    assert fake_sdk.executed == []


def test_sig2_safe_dispatches_to_relayer(
    fake_sdk: _FakeRelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_lock(monkeypatch)
    relayer = PolymarketRelayerClient(_creds())
    web3 = _FakeWeb3()
    client = Web3RedeemClient(
        web3=web3, private_key_resolver=_key, relayer=relayer
    )
    result = run(client.redeem(_spec(is_neg_risk=False), binding=_SAFE))
    assert result.status == RedeemJobStatus.CONFIRMED
    # Relayer SDK is hit; web3 backend untouched.
    assert len(fake_sdk.executed) == 1
    assert fake_sdk.executed[0]["to"] == constants.CTF_ADDRESS
    assert web3.sent == []


def test_sig2_safe_unwrap_dispatches_to_relayer(
    fake_sdk: _FakeRelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_lock(monkeypatch)
    relayer = PolymarketRelayerClient(_creds())
    client = Web3RedeemClient(
        web3=_FakeWeb3(), private_key_resolver=_key, relayer=relayer
    )
    result = run(client.unwrap_collateral(binding=_SAFE, amount=42))
    assert result.status == RedeemJobStatus.CONFIRMED
    assert fake_sdk.executed[0]["to"] == constants.WRAPPED_COLLATERAL_ADDRESS


def test_sig1_magic_still_fails_closed_with_relayer(
    fake_sdk: _FakeRelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK exposes no MAGIC PROXY surface — must stay fail-closed."""
    _enable_lock(monkeypatch)
    client = Web3RedeemClient(
        web3=_FakeWeb3(),
        private_key_resolver=_key,
        relayer=PolymarketRelayerClient(_creds()),
    )
    with pytest.raises(RelayerNotConfiguredError):
        run(client.redeem(_spec(is_neg_risk=False), binding=_MAGIC))
    assert fake_sdk.executed == []


def test_sig3_poly1271_still_fails_closed_with_relayer(
    fake_sdk: _FakeRelayClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POLY_1271 has no SDK surface and no PolyHermes blueprint."""
    _enable_lock(monkeypatch)
    client = Web3RedeemClient(
        web3=_FakeWeb3(),
        private_key_resolver=_key,
        relayer=PolymarketRelayerClient(_creds()),
    )
    with pytest.raises(RelayerNotConfiguredError):
        run(client.redeem(_spec(is_neg_risk=False), binding=_POLY_1271))
    assert fake_sdk.executed == []


def test_sig2_without_relayer_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No relayer injected ⇒ sig 2 SAFE goes through the base seam ⇒
    RelayerNotConfiguredError (no silent fallthrough)."""
    _enable_lock(monkeypatch)
    client = Web3RedeemClient(web3=_FakeWeb3(), private_key_resolver=_key)
    with pytest.raises(RelayerNotConfiguredError):
        run(client.redeem(_spec(is_neg_risk=False), binding=_SAFE))



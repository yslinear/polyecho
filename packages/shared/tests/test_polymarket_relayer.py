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
    SkipReason,
    WalletBinding,
)
from polyecho.shared.polymarket.redeem import (
    RelayerNotConfiguredError,
    Web3RedeemClient,
)
from polyecho.shared.polymarket.relayer import (
    PolymarketRelayerClient,
    QuotaCooldown,
    RelayerCredentials,
    RelayerCredentialsMissingError,
    RelayerQuotaExceededError,
    RelayerSubmissionError,
    encode_multisend_calldata,
    encode_redeem_positions_calldata,
    encode_unwrap_calldata,
    relay_redeem,
    relay_redeem_batch,
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

    def __init__(
        self,
        *,
        fail_4xx: bool = False,
        transient_then_ok: bool = False,
        fail_429: bool = False,
        quota_429_body: str = "quota exceeded",
    ) -> None:
        self.executed: list[dict[str, Any]] = []
        self.nonce_calls: list[tuple[str, str]] = []
        self.deployed_calls: list[str] = []
        self._fail_4xx = fail_4xx
        self._transient_then_ok = transient_then_ok
        self._fail_429 = fail_429
        self._quota_429_body = quota_429_body
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
        if self._fail_429:
            raise _make_api_exception(status_code=429, msg=self._quota_429_body)
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
        def __init__(self, *, status_code: int | None = None, error_msg: str | None = None) -> None:
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
        exc: Exception = exc_mod.RelayerApiException(status_code=status_code, error_msg=msg)
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
    result = run(client.submit_safe_transaction(to=constants.CTF_ADDRESS, data="0x"))
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
    assert addr_word.lower().endswith(constants.WRAPPED_COLLATERAL_ADDRESS[2:].lower())


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
    client = Web3RedeemClient(web3=web3, private_key_resolver=_key, relayer=relayer)
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
    client = Web3RedeemClient(web3=web3, private_key_resolver=_key, relayer=relayer)
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
    client = Web3RedeemClient(web3=_FakeWeb3(), private_key_resolver=_key, relayer=relayer)
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


# ---------------------------------------------------------------------------
# Quota-cooldown circuit-breaker (brief A4 — relayer 429)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic cooldown tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_quota_cooldown_parses_resets_in_n_seconds() -> None:
    """A 429 body with "resets in N seconds" sets the cooldown to N."""
    clock = _FakeClock()
    cd = QuotaCooldown(clock=clock)
    cooldown = cd.trip(body="quota exceeded, resets in 42 seconds")
    assert cooldown == 42.0
    assert cd.blocked() is True
    assert cd.retry_after_s() == 42.0


def test_quota_cooldown_default_when_body_has_no_hint() -> None:
    """No parseable hint ⇒ the default backoff is used (not zero)."""
    clock = _FakeClock()
    cd = QuotaCooldown(clock=clock, default_cooldown_s=60.0)
    cooldown = cd.trip(body="too many requests")
    assert cooldown == 60.0
    assert cd.blocked() is True


def test_quota_cooldown_clamped_to_max() -> None:
    """A hostile / huge reset hint is clamped to the defensive ceiling."""
    clock = _FakeClock()
    cd = QuotaCooldown(clock=clock, max_cooldown_s=3600.0)
    cooldown = cd.trip(body="resets in 999999 seconds")
    assert cooldown == 3600.0


def test_quota_cooldown_expires_after_window() -> None:
    """Once the clock passes the cutoff, the gate unblocks."""
    clock = _FakeClock()
    cd = QuotaCooldown(clock=clock)
    cd.trip(body="resets in 30 s")
    assert cd.blocked() is True
    clock.advance(29.0)
    assert cd.blocked() is True
    clock.advance(2.0)  # now 31s elapsed > 30s cutoff
    assert cd.blocked() is False
    assert cd.retry_after_s() == 0.0


def test_429_sets_cooldown_and_next_submit_is_skipped(
    fake_sdk: _FakeRelayClient,
) -> None:
    """A 429 on submit trips the cooldown; the SUBSEQUENT submit within
    the window is short-circuited (no second relayer call)."""
    clock = _FakeClock()
    fake_sdk._fail_429 = True
    fake_sdk._quota_429_body = "quota exceeded, resets in 50 seconds"
    relayer = PolymarketRelayerClient(_creds(), quota_cooldown=QuotaCooldown(clock=clock))
    # First submit: the 429 is retried (3 attempts) then surfaces as a
    # RelayerSubmissionError — and trips the cooldown along the way.
    with pytest.raises(RelayerSubmissionError):
        run(client_submit(relayer))
    assert relayer.quota_blocked() is True
    attempts_after_first = fake_sdk._attempts
    assert attempts_after_first >= 1

    # Second submit WITHIN the window: short-circuited before any SDK
    # call — the relayer's execute() attempt counter must NOT advance.
    with pytest.raises(RelayerQuotaExceededError) as exc_info:
        run(client_submit(relayer))
    assert exc_info.value.retry_after_s > 0
    assert fake_sdk._attempts == attempts_after_first  # no new call


def test_submit_resumes_after_cooldown_expires(
    fake_sdk: _FakeRelayClient,
) -> None:
    """After the cooldown window elapses, submissions resume normally."""
    clock = _FakeClock()
    fake_sdk._fail_429 = True
    fake_sdk._quota_429_body = "resets in 40 seconds"
    relayer = PolymarketRelayerClient(_creds(), quota_cooldown=QuotaCooldown(clock=clock))
    with pytest.raises(RelayerSubmissionError):
        run(client_submit(relayer))
    assert relayer.quota_blocked() is True

    # Advance past the window and let the relayer succeed this time.
    clock.advance(41.0)
    assert relayer.quota_blocked() is False
    fake_sdk._fail_429 = False
    result = run(client_submit(relayer))
    assert result["state"] == "STATE_CONFIRMED"


def test_relay_redeem_quota_cooldown_surfaces_skip_reason(
    fake_sdk: _FakeRelayClient,
) -> None:
    """relay_redeem under an active cooldown returns a PENDING result
    tagged QUOTA_COOLDOWN — the deferral is observable on the redeem job,
    not silently swallowed, and no retry budget is burned (not FAILED)."""
    clock = _FakeClock()
    relayer = PolymarketRelayerClient(_creds(), quota_cooldown=QuotaCooldown(clock=clock))
    # Pre-trip the cooldown so the next relay_redeem is short-circuited.
    relayer._quota.trip(body="resets in 60 seconds")
    result = run(relay_redeem(relayer, _spec(is_neg_risk=False), binding=_SAFE))
    assert result.status == RedeemJobStatus.PENDING
    assert result.skip_reason == SkipReason.QUOTA_COOLDOWN
    assert "quota-cooldown" in (result.detail or "")
    # No submission reached the SDK.
    assert fake_sdk.executed == []


def test_relay_unwrap_quota_cooldown_surfaces_skip_reason(
    fake_sdk: _FakeRelayClient,
) -> None:
    """relay_unwrap under an active cooldown likewise defers (PENDING +
    QUOTA_COOLDOWN) rather than marking the job FAILED."""
    clock = _FakeClock()
    relayer = PolymarketRelayerClient(_creds(), quota_cooldown=QuotaCooldown(clock=clock))
    relayer._quota.trip(body=None)  # default backoff
    result = run(relay_unwrap(relayer, binding=_SAFE, amount=1000))
    assert result.status == RedeemJobStatus.PENDING
    assert result.skip_reason == SkipReason.QUOTA_COOLDOWN
    assert fake_sdk.executed == []


def client_submit(relayer: PolymarketRelayerClient) -> Any:
    """Helper: a minimal submit_safe_transaction coroutine for the
    quota-cooldown tests (CTF target, throwaway calldata)."""
    return relayer.submit_safe_transaction(
        to=constants.CTF_ADDRESS, data="0x", metadata="quota-probe"
    )


# ---------------------------------------------------------------------------
# Deep-dive B6 — SAFE redeem MultiSend batching
# ---------------------------------------------------------------------------


def _batch_specs(account_id: uuid.UUID, n: int) -> list[RedeemJobSpec]:
    """N distinct redeem jobs for ONE account (distinct condition ids)."""
    return [
        RedeemJobSpec(
            account_id=account_id,
            condition_id="0x" + f"{i:02x}" * 32,
            outcome_index=0,
            index_set=1,
            is_neg_risk=False,
        )
        for i in range(n)
    ]


def test_encode_multisend_packs_each_call() -> None:
    """MultiSend calldata = ``multiSend(bytes)`` selector + ABI-wrapped
    packed ``op(1)||to(20)||value(32)||len(32)||data`` per inner call."""
    inner = encode_redeem_positions_calldata(
        collateral=constants.PUSD_ADDRESS,
        parent_collection_id=constants.PARENT_COLLECTION_ID,
        condition_id="0x" + "ab" * 32,
        index_sets=[1],
    )
    data = encode_multisend_calldata([(constants.CTF_ADDRESS, inner)])
    # multiSend(bytes) selector = keccak256("multiSend(bytes)")[:4].
    assert data.startswith("0x8d80ff0a")
    # The inner CTF address (20 bytes, lowercased hex) appears in the body.
    assert constants.CTF_ADDRESS[2:].lower() in data.lower()
    # Two-call batch is strictly longer than a one-call batch.
    data2 = encode_multisend_calldata(
        [(constants.CTF_ADDRESS, inner), (constants.CTF_ADDRESS, inner)]
    )
    assert len(data2) > len(data)


def test_encode_multisend_rejects_empty_batch() -> None:
    with pytest.raises(ValueError, match="empty batch"):
        encode_multisend_calldata([])


def test_relay_redeem_batch_n_jobs_one_multisend_tx(
    fake_sdk: _FakeRelayClient,
) -> None:
    """N SAFE redeem jobs for one account ⇒ exactly ONE relayer tx, sent
    to MultiSend with operation=DelegateCall; every job gets its own
    result row sharing that one tx hash (per-job idempotency preserved)."""
    relayer = PolymarketRelayerClient(_creds())
    account_id = uuid.uuid4()
    specs = _batch_specs(account_id, 3)
    results = run(relay_redeem_batch(relayer, specs, binding=_SAFE))

    # Exactly one on-chain submission for 3 jobs.
    assert len(fake_sdk.executed) == 1
    call = fake_sdk.executed[0]
    assert call["to"] == constants.SAFE_MULTISEND_CALL_ONLY
    assert call["operation"] == 1  # _OpType.DelegateCall in the fake SDK
    assert call["data"].startswith("0x8d80ff0a")  # multiSend(bytes)

    # One result per job, keyed by (condition_id, outcome_index); each is
    # individually CONFIRMED and shares the single batch tx hash.
    assert len(results) == 3
    tx_hashes = set()
    for spec in specs:
        res = results[(spec.condition_id, spec.outcome_index)]
        assert res.status == RedeemJobStatus.CONFIRMED
        assert res.tx_hash is not None
        tx_hashes.add(res.tx_hash)
        assert "MultiSend batch" in (res.detail or "")
    assert len(tx_hashes) == 1  # all jobs share the one batched tx


def test_relay_redeem_batch_single_job_uses_plain_redeem(
    fake_sdk: _FakeRelayClient,
) -> None:
    """A 1-element batch skips the MultiSend wrapper — it is a plain
    single-tx redeem (to=CTF, operation=Call), unchanged from pre-B6."""
    relayer = PolymarketRelayerClient(_creds())
    specs = _batch_specs(uuid.uuid4(), 1)
    results = run(relay_redeem_batch(relayer, specs, binding=_SAFE))
    assert len(fake_sdk.executed) == 1
    assert fake_sdk.executed[0]["to"] == constants.CTF_ADDRESS
    assert fake_sdk.executed[0]["operation"] == 0  # Call, not DelegateCall
    assert results[(specs[0].condition_id, 0)].status == RedeemJobStatus.CONFIRMED


def test_relay_redeem_batch_rejects_magic_sig1(
    fake_sdk: _FakeRelayClient,
) -> None:
    """Magic (sig 1) must NEVER be batched — PolyHermes excludes it."""
    relayer = PolymarketRelayerClient(_creds())
    specs = _batch_specs(uuid.uuid4(), 3)
    with pytest.raises(RelayerSubmissionError, match="SAFE-only"):
        run(relay_redeem_batch(relayer, specs, binding=_MAGIC))
    assert fake_sdk.executed == []


def test_relay_redeem_batch_rejects_mixed_accounts(
    fake_sdk: _FakeRelayClient,
) -> None:
    """All specs in a batch must share one account_id."""
    relayer = PolymarketRelayerClient(_creds())
    specs = _batch_specs(uuid.uuid4(), 2) + _batch_specs(uuid.uuid4(), 1)
    with pytest.raises(ValueError, match="one account_id"):
        run(relay_redeem_batch(relayer, specs, binding=_SAFE))
    assert fake_sdk.executed == []


def test_relay_redeem_batch_quota_cooldown_fans_deferral(
    fake_sdk: _FakeRelayClient,
) -> None:
    """Under an active 429 cooldown a batch defers EVERY job (PENDING +
    QUOTA_COOLDOWN) rather than marking any FAILED — no submission made."""
    clock = _FakeClock()
    relayer = PolymarketRelayerClient(_creds(), quota_cooldown=QuotaCooldown(clock=clock))
    relayer._quota.trip(body="resets in 60 seconds")
    specs = _batch_specs(uuid.uuid4(), 3)
    results = run(relay_redeem_batch(relayer, specs, binding=_SAFE))
    assert len(results) == 3
    for spec in specs:
        res = results[(spec.condition_id, spec.outcome_index)]
        assert res.status == RedeemJobStatus.PENDING
        assert res.skip_reason == SkipReason.QUOTA_COOLDOWN
    assert fake_sdk.executed == []


def test_relay_redeem_batch_submit_failure_fans_failed(
    fake_sdk: _FakeRelayClient,
) -> None:
    """A batch-level submit failure fans the SAME failed status to every
    job — none is silently dropped."""
    fake_sdk._fail_4xx = True
    relayer = PolymarketRelayerClient(_creds())
    specs = _batch_specs(uuid.uuid4(), 3)
    results = run(relay_redeem_batch(relayer, specs, binding=_SAFE))
    assert len(results) == 3
    for spec in specs:
        res = results[(spec.condition_id, spec.outcome_index)]
        assert res.status == RedeemJobStatus.FAILED


def test_submit_safe_multisend_rejects_empty_batch(
    fake_sdk: _FakeRelayClient,
) -> None:
    relayer = PolymarketRelayerClient(_creds())
    with pytest.raises(ValueError, match="empty batch"):
        run(relayer.submit_safe_multisend(calls=[]))
    assert fake_sdk.executed == []


def test_redeem_client_redeem_batch_safe_dispatches_to_multisend(
    fake_sdk: _FakeRelayClient,
) -> None:
    """Web3RedeemClient.redeem_batch for a SAFE wallet routes through the
    MultiSend batch path (one DelegateCall tx for N jobs)."""
    relayer = PolymarketRelayerClient(_creds())
    client = Web3RedeemClient(web3=_FakeWeb3(), private_key_resolver=_key, relayer=relayer)
    specs = _batch_specs(uuid.uuid4(), 4)
    results = run(client.redeem_batch(specs, binding=_SAFE))
    assert len(fake_sdk.executed) == 1
    assert fake_sdk.executed[0]["to"] == constants.SAFE_MULTISEND_CALL_ONLY
    assert len(results) == 4
    assert all(r.status == RedeemJobStatus.CONFIRMED for r in results.values())


def test_redeem_client_redeem_batch_magic_stays_sequential(
    fake_sdk: _FakeRelayClient,
) -> None:
    """Web3RedeemClient.redeem_batch for Magic (sig 1) does NOT batch — it
    falls back to the per-job loop, which fails closed per job (no SDK
    PROXY surface).  Crucially: no MultiSend tx is ever sent."""
    relayer = PolymarketRelayerClient(_creds())
    client = Web3RedeemClient(web3=_FakeWeb3(), private_key_resolver=_key, relayer=relayer)
    specs = _batch_specs(uuid.uuid4(), 3)
    # The per-job sequential fallback raises on the first Magic job.
    with pytest.raises(RelayerNotConfiguredError):
        run(client.redeem_batch(specs, binding=_MAGIC))
    assert fake_sdk.executed == []  # never batched, never DelegateCall

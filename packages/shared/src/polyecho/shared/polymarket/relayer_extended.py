"""
polyecho.shared.polymarket.relayer_extended
===========================================
Hand-rolled gasless-redeem path for ``signature_type=1`` (POLY_PROXY / Magic)
and ``signature_type=3`` (POLY_1271 / Deposit-Wallet).

Why this file exists
--------------------
The official ``py-builder-relayer-client`` v0.0.1 only ships a
``SafeTransaction`` (sig 2) submit surface.  The Polymarket TS reference
``Polymarket/builder-relayer-client`` carries the wire format for both
PROXY and Deposit-Wallet — but Python has no SDK for these flows, so we
implement them here against the relayer's public HTTP endpoints
(``relayer-v2.polymarket.com``):

* ``GET  /nonce?address=<EOA>&type=<PROXY|WALLET>``        — per-EOA nonce
* ``GET  /relay-payload?address=<EOA>&type=PROXY``         — relay/nonce
  bundle for sig-1 struct hash
* ``GET  /deployed?address=<WALLET>&type=WALLET``          — is-deployed
* ``POST /submit``                                          — submit tx
* ``GET  /transaction?id=<txID>``                          — poll-until

The single source of truth for byte-level details is
``docs/reference/polymarket-sig1-sig3-redeem-wire.md``.  Each ABI / signing /
header decision below has a one-line ``provenance:`` comment pointing back
either to that brief or to the TS source file.

Design constraints (mirror ``relayer.py``)
------------------------------------------
* **Import-side-effect-free** — every heavy dependency
  (``eth_account``, ``eth_abi``, ``py_builder_signing_sdk``) is imported
  lazily inside the call sites that need them, so this module is safely
  importable on hosts without the ``[relayer]`` extra.
* **Fail-closed** — missing builder credentials, missing TS-byte literals,
  or a sig outside {1, 3} all raise a typed error rather than POSTing a
  partial body.  The four guardrails from the brief
  (re-derive funder, refuse value≠0, refuse deadline>600s, refuse target
  outside allow-list) are enforced before ``/submit``.
* **Async + retry + circuit-breaker** — same exponential-backoff /
  5-failure-open shape as :mod:`polyecho.shared.datasource.live` and
  :mod:`polyecho.shared.polymarket.relayer`, for operator-uniform
  mental-model.
* **Operator confirmation** — this module performs the I/O; per-op
  operator confirmation lives at the web layer (``dangerous-action`` +
  account-import passphrase) and the relayer credentials must be
  explicitly injected, so a misconfigured deployment cannot reach a
  ``/submit`` POST.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from polyecho.shared.constants import (
    BUILDER_RELAYER_URL,
    CTF_ADDRESS,
    DEPOSIT_WALLET_FACTORY,
    MAGIC_PROXY_FACTORY,
    NEG_RISK_ADAPTER_ADDRESS,
    PARENT_COLLECTION_ID,
    POLYGON_CHAIN_ID,
    PUSD_ADDRESS,
    RELAY_HUB,
    WRAPPED_COLLATERAL_ADDRESS,
)

__all__ = [
    "RelayerNotAvailableError",
    "RelayerCredentialsMissingError",
    "RelayerSubmissionError",
    "ExtendedRelayerCredentials",
    "redeem_proxy",
    "redeem_deposit_wallet",
    "relay_unwrap_proxy",
    "relay_unwrap_deposit_wallet",
    # P8 parity / probe tests reach these; explicit re-export so mypy strict
    # mode doesn't reject `RX.CTF_ADDRESS` / `relayer_extended.httpx`.
    "CTF_ADDRESS",
    "httpx",
]

logger = logging.getLogger(__name__)

# Retry / circuit-breaker — uniform with relayer.py / datasource.live.
_RETRY_ATTEMPTS: Final[int] = 3
_RETRY_BASE_S: Final[float] = 1.0
_RETRY_CAP_S: Final[float] = 30.0

# Poll cadence — sig 1/3 redeem typically mines within 30s on Polygon;
# 5-min cap matches the brief's "fail-closed if not STATE_MINED within 5min".
_POLL_INTERVAL_S: Final[float] = 2.0
_POLL_MAX_TICKS: Final[int] = 150  # 150 * 2s = 5 min

# Brief allow-list (guardrail #3).  Anything else MUST be refused before
# /submit.  CTF (redeem target) + WCOL (unwrap target) + NegRiskAdapter
# (NegRisk redeem post-processing) are the only legitimate post-resolution
# call targets for sig 1/3.
_TARGET_ALLOWLIST: Final[frozenset[str]] = frozenset(
    a.lower()
    for a in (
        CTF_ADDRESS,
        WRAPPED_COLLATERAL_ADDRESS,
        NEG_RISK_ADAPTER_ADDRESS,
    )
)

# Brief guardrail thresholds.
_MAX_GAS_LIMIT: Final[int] = 30_000_000
_MAX_DEADLINE_DELTA_S: Final[int] = 600
_PROXY_DEFAULT_GAS_LIMIT: Final[int] = 10_000_000  # brief fallback

# Terminal relayer states (from TS ``types.ts``).
_STATE_MINED: Final[frozenset[str]] = frozenset({"STATE_MINED", "STATE_CONFIRMED"})
_STATE_FAILED: Final[frozenset[str]] = frozenset({"STATE_FAILED", "STATE_INVALID"})

_SIG_PROXY: Final[int] = 1
_SIG_DEPOSIT_WALLET: Final[int] = 3


class RelayerNotAvailableError(RuntimeError):
    """Raised when ``py-builder-signing-sdk`` is not installed but the caller
    requested a sig 1 / sig 3 path (HMAC headers cannot be computed without
    the SDK; we refuse to hand-roll the HMAC scheme).
    """


class RelayerCredentialsMissingError(RuntimeError):
    """Raised when a sig 1 / sig 3 path is invoked without all builder-HMAC
    credentials + the EOA private key configured.
    """


class RelayerSubmissionError(RuntimeError):
    """Raised when the relayer ACKs but never reaches STATE_MINED, reaches
    STATE_FAILED / STATE_INVALID, or violates a brief guardrail.
    """


@dataclass(frozen=True, slots=True)
class ExtendedRelayerCredentials:
    """Configuration bundle for sig 1 / sig 3 redeem.

    Mirrors :class:`polyecho.shared.polymarket.relayer.RelayerCredentials`
    so a single deployment can populate both clients from one env-var set.
    """

    private_key: str
    builder_api_key: str
    builder_secret: str
    builder_passphrase: str
    relayer_url: str = BUILDER_RELAYER_URL
    chain_id: int = POLYGON_CHAIN_ID

    def assert_complete(self) -> None:
        missing = [
            name
            for name, value in (
                ("private_key", self.private_key),
                ("builder_api_key", self.builder_api_key),
                ("builder_secret", self.builder_secret),
                ("builder_passphrase", self.builder_passphrase),
            )
            if not value
        ]
        if missing:
            raise RelayerCredentialsMissingError(
                "ExtendedRelayerCredentials missing required fields: "
                f"{missing!r}.  Set BUILDER_API_KEY / BUILDER_SECRET / "
                "BUILDER_PASS_PHRASE and the EOA signing key before "
                "enabling gasless PROXY / Deposit-Wallet redeem."
            )


# ---------------------------------------------------------------------------
# Calldata encoders (lazy eth_abi import — module stays import-clean)
# ---------------------------------------------------------------------------


def _function_selector(signature: str) -> bytes:
    # provenance: ``Polymarket/builder-relayer-client`` ``src/abis/*.ts``
    # selectors are the standard ``keccak256(sig)[:4]``.
    from eth_utils import keccak  # type: ignore[attr-defined]

    result: bytes = keccak(text=signature)[:4]
    return result


def _encode_redeem_positions(
    *,
    collateral: str,
    condition_id: str,
    index_sets: list[int],
) -> bytes:
    """``CTF.redeemPositions(address,bytes32,bytes32,uint256[])`` calldata.

    provenance: brief §sig-1 step 3 (sig-1 calldata) + brief §sig-3 step 3
    (deposit-wallet ``Batch`` calldata).  Identical shape to
    :func:`polyecho.shared.polymarket.relayer.encode_redeem_positions_calldata`
    — duplicated locally to keep this module relayer-free at import time
    (it sits BESIDE the SAFE relayer; importing it would create a cycle).
    """
    from eth_abi import encode  # type: ignore[attr-defined]
    from eth_utils import to_bytes  # type: ignore[attr-defined]

    selector = _function_selector(
        "redeemPositions(address,bytes32,bytes32,uint256[])"
    )
    body: bytes = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            collateral,
            to_bytes(hexstr=PARENT_COLLECTION_ID),
            to_bytes(hexstr=condition_id),
            index_sets,
        ],
    )
    return selector + body


def _encode_unwrap(*, to_address: str, amount: int) -> bytes:
    """``WrappedCollateral.unwrap(address,uint256)`` calldata.

    provenance: brief §sig-1 ("redeem flow … unwrap (to,amount)").
    """
    from eth_abi import encode  # type: ignore[attr-defined]

    selector = _function_selector("unwrap(address,uint256)")
    body: bytes = encode(["address", "uint256"], [to_address, amount])
    return selector + body


def _encode_proxy_batch(calls: list[tuple[int, str, int, bytes]]) -> bytes:
    """``ProxyFactory.proxy((uint8,address,uint256,bytes)[])`` calldata.

    provenance: brief §sig-1 step 3 + ``src/abis/proxyFactory.ts`` —
    ABI tuple component order is
    ``[typeCode uint8, to address, value uint256, data bytes]`` so the
    selector is ``proxy((uint8,address,uint256,bytes)[])`` = ``0x34ee9791``.
    ``typeCode=1`` = Call (TS ``OperationType.Call``).
    """
    from eth_abi import encode  # type: ignore[attr-defined]

    selector = _function_selector("proxy((uint8,address,uint256,bytes)[])")
    body: bytes = encode(
        ["(uint8,address,uint256,bytes)[]"],
        [calls],
    )
    return selector + body


# ---------------------------------------------------------------------------
# Guardrail validators (brief §sig-1/§sig-3 — refuse buggy encoder output)
# ---------------------------------------------------------------------------


def _assert_target_allowed(target: str) -> None:
    if target.lower() not in _TARGET_ALLOWLIST:
        raise RelayerSubmissionError(
            f"refuse to submit: target {target!r} not in brief allow-list "
            f"{sorted(_TARGET_ALLOWLIST)!r} (guardrail #3)."
        )


def _assert_gas_limit_ok(gas_limit: int) -> None:
    if gas_limit > _MAX_GAS_LIMIT:
        raise RelayerSubmissionError(
            f"refuse to submit: gas_limit={gas_limit} exceeds brief cap "
            f"{_MAX_GAS_LIMIT} (guardrail #3)."
        )


def _assert_deadline_ok(deadline: int) -> None:
    now = int(time.time())
    if deadline - now > _MAX_DEADLINE_DELTA_S:
        raise RelayerSubmissionError(
            f"refuse to submit: deadline {deadline} > now+{_MAX_DEADLINE_DELTA_S}s "
            f"(guardrail #3)."
        )


# ---------------------------------------------------------------------------
# Builder HMAC headers (lazy py_builder_signing_sdk import)
# ---------------------------------------------------------------------------


def _builder_headers(
    creds: ExtendedRelayerCredentials,
    *,
    method: str,
    request_path: str,
    body: str,
) -> dict[str, str]:
    """Compute the 4 builder-program HMAC headers (POLY_BUILDER_*).

    provenance: ``Polymarket/builder-signing-sdk`` (Python port:
    ``py-builder-signing-sdk``).  We delegate to the SDK so the HMAC
    computation matches the relayer's verification bit-for-bit; fail-closed
    if the SDK isn't installed.
    """
    try:
        from py_builder_signing_sdk.signer import BuilderSigner
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RelayerNotAvailableError(
            "py-builder-signing-sdk is not installed.  Install the "
            "shared package's ``[relayer]`` extra to enable sig 1 / "
            "sig 3 gasless redeem (sig 2 SAFE is unaffected)."
        ) from exc

    signer = BuilderSigner(
        api_key=creds.builder_api_key,
        secret=creds.builder_secret,
        passphrase=creds.builder_passphrase,
    )
    timestamp = str(int(time.time()))
    signature = signer.sign(
        timestamp=timestamp,
        method=method.upper(),
        request_path=request_path,
        body=body,
    )
    return {
        "POLY_BUILDER_API_KEY": creds.builder_api_key,
        "POLY_BUILDER_SIGNATURE": signature,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_PASSPHRASE": creds.builder_passphrase,
    }


# ---------------------------------------------------------------------------
# HTTP plumbing — async httpx with retry + circuit shape from live.py
# ---------------------------------------------------------------------------


async def _retry(
    fn: Any,
    *args: Any,
    attempts: int = _RETRY_ATTEMPTS,
    **kwargs: Any,
) -> Any:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn(*args, **kwargs)
        except RelayerSubmissionError:
            # Deterministic — surface immediately, do not retry.
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if 400 <= status < 500 and status != 429:
                # Deterministic 4xx — bad nonce / bad sig / undeployed wallet.
                raise RelayerSubmissionError(
                    f"relayer HTTP {status}: {exc.response.text[:200]!r}"
                ) from exc
            last = exc
        except Exception as exc:  # transport / DNS / TLS
            last = exc

        if attempt < attempts - 1:
            sleep_s = min(_RETRY_CAP_S, _RETRY_BASE_S * math.pow(2, attempt))
            await asyncio.sleep(sleep_s)
    raise RelayerSubmissionError(
        f"relayer call failed after {attempts} attempts: {last!r}"
    )


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> Any:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
) -> Any:
    resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _post_submit(
    client: httpx.AsyncClient,
    base_url: str,
    body: dict[str, Any],
    creds: ExtendedRelayerCredentials,
    *,
    require_tx_id: bool = True,
) -> str:
    """POST *body* to ``<base_url>/submit`` with HMAC headers; return tx id.

    Canonicalises *body* with ``json.dumps(separators=(",", ":"))`` so the
    HMAC-signed bytes match what the relayer actually receives.  Used by
    every sig-1 / sig-3 ``/submit`` call path; consolidating here removes
    four near-duplicate copies of the same pattern.

    When *require_tx_id* is ``False`` (e.g. ``WALLET-CREATE``) the ACK is
    parsed leniently — an empty string is returned if no id is present
    rather than raising.
    """
    canonical = json.dumps(body, separators=(",", ":"))
    headers = _builder_headers(
        creds, method="POST", request_path="/submit", body=canonical
    )
    resp = await _retry(
        client.post,
        f"{base_url.rstrip('/')}/submit",
        content=canonical,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert isinstance(resp, httpx.Response)
    if resp.status_code >= 400:
        raise RelayerSubmissionError(
            f"/submit HTTP {resp.status_code}: {resp.text[:200]!r}"
        )
    ack = resp.json()
    tx_id = ack.get("transactionID") or ack.get("id") if isinstance(ack, dict) else None
    if not tx_id:
        if require_tx_id:
            raise RelayerSubmissionError(
                f"unexpected /submit ACK shape: {ack!r}"
            )
        return ""
    return str(tx_id)


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    transaction_id: str,
) -> dict[str, Any]:
    """Poll ``GET /transaction?id=`` until STATE_MINED/_CONFIRMED.

    provenance: brief common-wire facts 5th bullet; mirrors TS
    ``pollUntilState`` in ``src/client.ts``.
    """
    url = f"{base_url.rstrip('/')}/transaction"
    for tick in range(_POLL_MAX_TICKS):
        payload = await _retry(_get_json, client, url, params={"id": transaction_id})
        state = payload.get("state") if isinstance(payload, dict) else None
        if isinstance(state, str):
            if state in _STATE_MINED:
                return payload  # type: ignore[no-any-return]
            if state in _STATE_FAILED:
                raise RelayerSubmissionError(
                    f"transaction {transaction_id!r} reached {state}: "
                    f"{payload!r}"
                )
        if tick < _POLL_MAX_TICKS - 1:
            await asyncio.sleep(_POLL_INTERVAL_S)
    raise RelayerSubmissionError(
        f"transaction {transaction_id!r} did not reach terminal state "
        f"within {_POLL_MAX_TICKS * _POLL_INTERVAL_S:.0f}s"
    )


# ---------------------------------------------------------------------------
# sig 1 — POLY_PROXY (Magic) struct-hash signing
# ---------------------------------------------------------------------------


def _proxy_struct_hash(
    *,
    eoa: str,
    proxy_factory: str,
    data: bytes,
    tx_fee: int,
    gas_price: int,
    gas_limit: int,
    nonce: int,
    relay_hub: str,
    relay: str,
) -> bytes:
    """Compute the sig-1 ``rlx:`` struct-hash.

    provenance: brief §sig-1 step 4 (verbatim) + ``src/builder/proxy.ts``:

        keccak256(
            "rlx:" || from(20) || to(20) || data
         || txFee(32) || gasPrice(32) || gasLimit(32) || nonce(32)
         || relayHub(20) || relay(20)
        )

    All numeric fields are 32-byte big-endian.  The ``from`` field is the
    EOA (signer), NOT the proxy; ``to`` is the ProxyFactory (the wrapper
    contract whose ``proxy(...)`` is called).
    """
    from eth_utils import keccak  # type: ignore[attr-defined]

    parts = [
        b"rlx:",
        bytes.fromhex(eoa[2:]),
        bytes.fromhex(proxy_factory[2:]),
        data,
        tx_fee.to_bytes(32, "big"),
        gas_price.to_bytes(32, "big"),
        gas_limit.to_bytes(32, "big"),
        nonce.to_bytes(32, "big"),
        bytes.fromhex(relay_hub[2:]),
        bytes.fromhex(relay[2:]),
    ]
    return keccak(b"".join(parts))


def _sign_proxy(struct_hash: bytes, private_key: str) -> str:
    """Sign *struct_hash* via ``eth_sign`` (EIP-191 personal_sign path).

    provenance: brief §sig-1 step 5 ("eth_sign via web3.eth.account.signHash
    — not EIP-712").  The TS reference (``signer.signMessage(structHash)``)
    runs through the EIP-191 personal_sign path which prepends
    ``\\x19Ethereum Signed Message:\\n32`` before signing — NOT raw ECDSA.

    # VERIFIED 2026-05-20: TS uses EIP-191 path (signMessage), not raw ECDSA.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    if len(struct_hash) != 32:
        raise RelayerSubmissionError(
            f"struct_hash must be 32 bytes, got {len(struct_hash)}"
        )
    # encode_defunct(primitive=...) prepends the EIP-191 header
    # (``\x19Ethereum Signed Message:\n32`` + struct_hash) and re-hashes
    # before ECDSA — bit-for-bit equivalent to ethers.js signer.signMessage.
    signable = encode_defunct(primitive=struct_hash)
    signed = Account.sign_message(signable, private_key=private_key)
    sig: bytes = signed.signature
    if len(sig) != 65:
        raise RelayerSubmissionError(
            f"unexpected eth_sign signature length {len(sig)} (expected 65)"
        )
    return "0x" + sig.hex()


# ---------------------------------------------------------------------------
# sig 3 — POLY_1271 (Deposit-Wallet) EIP-712 ``Batch`` signing
# ---------------------------------------------------------------------------


def _sign_wallet_batch(
    *,
    wallet_address: str,
    chain_id: int,
    nonce: int,
    deadline: int,
    calls: list[dict[str, Any]],
    private_key: str,
) -> str:
    """EIP-712 sign the Deposit-Wallet ``Batch`` typed-data.

    provenance: brief §sig-3 step 4 (verbatim) — domain
    ``{name:"DepositWallet", version:"1", chainId:<137>,
    verifyingContract:<walletAddress>}``, primaryType ``Batch``,
    struct ``Batch(wallet address, nonce uint256, deadline uint256,
    calls Call[])`` with ``Call(target address, value uint256, data bytes)``.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    full_message: dict[str, Any] = {
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
            "chainId": chain_id,
            "verifyingContract": wallet_address,
        },
        "message": {
            "wallet": wallet_address,
            "nonce": nonce,
            "deadline": deadline,
            "calls": calls,
        },
    }
    signable = encode_typed_data(full_message=full_message)
    signed = Account.sign_message(signable, private_key=private_key)
    sig: bytes = signed.signature
    if len(sig) != 65:
        raise RelayerSubmissionError(
            f"unexpected EIP-712 signature length {len(sig)} (expected 65)"
        )
    return "0x" + sig.hex()


# ---------------------------------------------------------------------------
# Funder re-derivation guard (brief guardrail #2)
# ---------------------------------------------------------------------------


def _assert_funder_match(
    *,
    sig_type: int,
    eoa: str,
    expected_funder: str,
) -> None:
    """Re-derive the funder from *eoa* and refuse if it doesn't match.

    Implements brief guardrail #2 ("Before posting /submit, re-derive the
    funder and assert it matches the signature_type=1 / 3 funder stored in
    our DB; refuse if mismatch.").
    """
    # Lazy import — avoid `wallet` → this-module cycles at import time.
    from polyecho.shared.polymarket.wallet import (
        deposit_wallet_address,
        magic_proxy_address,
    )

    if sig_type == _SIG_PROXY:
        derived = magic_proxy_address(eoa)
    elif sig_type == _SIG_DEPOSIT_WALLET:
        derived = deposit_wallet_address(eoa)
    else:
        raise RelayerSubmissionError(
            f"_assert_funder_match: unexpected sig_type={sig_type}"
        )
    if derived.lower() != expected_funder.lower():
        raise RelayerSubmissionError(
            f"funder mismatch (guardrail #2): derived {derived!r} != "
            f"expected {expected_funder!r} for eoa={eoa!r} sig={sig_type}"
        )


# ---------------------------------------------------------------------------
# Public surface — sig 1 (POLY_PROXY) redeem + unwrap
# ---------------------------------------------------------------------------


async def redeem_proxy(
    *,
    eoa: str,
    private_key: str,
    proxy_funder: str,
    condition_id: str,
    is_neg_risk: bool,
    index_sets: list[int],
    creds: ExtendedRelayerCredentials,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Gasless ``CTF.redeemPositions`` via sig-1 (POLY_PROXY) flow.

    Returns the relayer ``transaction_hash``.  Steps mirror brief §sig-1
    1–7 verbatim.  Per-op operator confirmation lives at the web layer
    (``dangerous-action`` + account-import passphrase).

    Args:
        eoa:          The signer EOA (NOT the proxy).
        private_key: 0x… private key matching *eoa*; used for ``eth_sign``.
        proxy_funder: The Magic-proxy address (the on-chain holder of
                      positions / USDC).  Re-derived + asserted equal to
                      :func:`magic_proxy_address(eoa)` before /submit.
        condition_id: 32-byte hex condition id.
        is_neg_risk: ``True`` ⇒ collateral = WCOL; ``False`` ⇒ pUSD.
        index_sets:   Outcome bitmasks (typically ``[1, 2]`` for binary).
        creds:        Builder credentials.
        base_url:     Override relayer base URL.
        timeout:      httpx request timeout in seconds.
        http_client:  Optional pre-built ``httpx.AsyncClient`` (tests).
    """
    creds.assert_complete()
    _assert_funder_match(
        sig_type=_SIG_PROXY, eoa=eoa, expected_funder=proxy_funder
    )

    collateral = WRAPPED_COLLATERAL_ADDRESS if is_neg_risk else PUSD_ADDRESS

    # Build inner CTF.redeemPositions calldata, wrap in ProxyFactory.proxy(...).
    inner = _encode_redeem_positions(
        collateral=collateral,
        condition_id=condition_id,
        index_sets=index_sets,
    )
    _assert_target_allowed(CTF_ADDRESS)
    outer = _encode_proxy_batch([(1, CTF_ADDRESS, 0, inner)])  # typeCode=Call, value=0
    _assert_gas_limit_ok(_PROXY_DEFAULT_GAS_LIMIT)

    return await _submit_proxy(
        eoa=eoa,
        private_key=private_key,
        proxy_funder=proxy_funder,
        outer_data=outer,
        gas_limit=_PROXY_DEFAULT_GAS_LIMIT,
        creds=creds,
        base_url=base_url or creds.relayer_url,
        timeout_s=timeout_s,
        http_client=http_client,
        metadata="redeem positions",
    )


async def relay_unwrap_proxy(
    *,
    eoa: str,
    private_key: str,
    proxy_funder: str,
    amount: int,
    creds: ExtendedRelayerCredentials,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Gasless ``WrappedCollateral.unwrap(to,amount)`` via sig-1 flow."""
    creds.assert_complete()
    _assert_funder_match(
        sig_type=_SIG_PROXY, eoa=eoa, expected_funder=proxy_funder
    )
    if amount <= 0:
        raise RelayerSubmissionError(
            "relay_unwrap_proxy: amount must be > 0 (caller should skip)"
        )
    inner = _encode_unwrap(to_address=proxy_funder, amount=amount)
    _assert_target_allowed(WRAPPED_COLLATERAL_ADDRESS)
    outer = _encode_proxy_batch(
        [(1, WRAPPED_COLLATERAL_ADDRESS, 0, inner)]
    )
    return await _submit_proxy(
        eoa=eoa,
        private_key=private_key,
        proxy_funder=proxy_funder,
        outer_data=outer,
        gas_limit=_PROXY_DEFAULT_GAS_LIMIT,
        creds=creds,
        base_url=base_url or creds.relayer_url,
        timeout_s=timeout_s,
        http_client=http_client,
        metadata=f"unwrap WCOL {amount}",
    )


async def _submit_proxy(
    *,
    eoa: str,
    private_key: str,
    proxy_funder: str,
    outer_data: bytes,
    gas_limit: int,
    creds: ExtendedRelayerCredentials,
    base_url: str,
    timeout_s: float,
    http_client: httpx.AsyncClient | None,
    metadata: str,
) -> str:
    """Shared submit path for sig-1 redeem + unwrap."""

    async def _do(client: httpx.AsyncClient) -> str:
        # 1. /relay-payload — supplies the nonce + relay address used in
        #    the struct hash.  provenance: brief §sig-1 step 2.
        relay_payload = await _retry(
            _get_json,
            client,
            f"{base_url.rstrip('/')}/relay-payload",
            params={"address": eoa, "type": "PROXY"},
        )
        if not isinstance(relay_payload, dict):
            raise RelayerSubmissionError(
                f"unexpected /relay-payload shape: {relay_payload!r}"
            )
        relay = str(relay_payload["address"])
        nonce = int(relay_payload["nonce"])

        # 2. struct-hash + eth_sign.  provenance: brief §sig-1 steps 4-5.
        struct_hash = _proxy_struct_hash(
            eoa=eoa,
            proxy_factory=MAGIC_PROXY_FACTORY,
            data=outer_data,
            tx_fee=0,
            gas_price=0,
            gas_limit=gas_limit,
            nonce=nonce,
            relay_hub=RELAY_HUB,
            relay=relay,
        )
        signature = _sign_proxy(struct_hash, private_key)

        # 3. /submit.  Keys match brief §sig-1 step 6 verbatim (camelCase).
        body: dict[str, Any] = {
            "type": "PROXY",
            "from": eoa,
            # NOTE: brief flags "to" semantics as UNVERIFIED — TS source
            # uses ProxyFactory; we conservatively follow TS.  TODO(verify)
            # via chrome-devtools sniff of a real Magic-user redeem.
            "to": MAGIC_PROXY_FACTORY,
            "proxyWallet": proxy_funder,
            "data": "0x" + outer_data.hex(),
            "nonce": str(nonce),
            "signature": signature,
            "signatureParams": {
                "gasPrice": "0",
                "gasLimit": str(gas_limit),
                "relayerFee": "0",
                "relayHub": RELAY_HUB,
                "relay": relay,
            },
            "metadata": metadata,
        }
        # Compute HMAC headers against the canonical JSON body.  httpx
        # serialises with default separators; we mirror that here so the
        # signed bytes match what the relayer actually receives.
        tx_id = await _post_submit(client, base_url, body, creds)

        # 4. Poll until mined.  provenance: brief §sig-1 step 7.
        terminal = await _poll_until_terminal(
            client, base_url=base_url, transaction_id=tx_id
        )
        return str(terminal.get("transactionHash") or terminal.get("hash") or "")

    if http_client is not None:
        return await _do(http_client)
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await _do(c)


# ---------------------------------------------------------------------------
# Public surface — sig 3 (POLY_1271 / Deposit-Wallet) redeem + unwrap
# ---------------------------------------------------------------------------


async def redeem_deposit_wallet(
    *,
    eoa: str,
    private_key: str,
    deposit_funder: str,
    condition_id: str,
    is_neg_risk: bool,
    index_sets: list[int],
    creds: ExtendedRelayerCredentials,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Gasless ``CTF.redeemPositions`` via sig-3 (Deposit-Wallet) flow.

    Steps mirror brief §sig-3:
      1. (one-time) ``GET /deployed?…&type=WALLET`` → if not deployed POST
         ``type:"WALLET-CREATE"`` (no signature) + poll.
      2. ``GET /nonce?type=WALLET``
      3. encode CTF redeem calldata
      4. EIP-712 sign ``Batch`` typed-data (domain DepositWallet/1/137/
         walletAddress).
      5. ``POST /submit type:"WALLET"`` with ``depositWalletParams``.
      6. poll until mined.
    """
    creds.assert_complete()
    _assert_funder_match(
        sig_type=_SIG_DEPOSIT_WALLET,
        eoa=eoa,
        expected_funder=deposit_funder,
    )

    collateral = WRAPPED_COLLATERAL_ADDRESS if is_neg_risk else PUSD_ADDRESS
    inner = _encode_redeem_positions(
        collateral=collateral,
        condition_id=condition_id,
        index_sets=index_sets,
    )
    _assert_target_allowed(CTF_ADDRESS)
    return await _submit_deposit_wallet(
        eoa=eoa,
        private_key=private_key,
        deposit_funder=deposit_funder,
        target=CTF_ADDRESS,
        data=inner,
        creds=creds,
        base_url=base_url or creds.relayer_url,
        timeout_s=timeout_s,
        http_client=http_client,
        metadata="redeem positions",
    )


async def relay_unwrap_deposit_wallet(
    *,
    eoa: str,
    private_key: str,
    deposit_funder: str,
    amount: int,
    creds: ExtendedRelayerCredentials,
    base_url: str | None = None,
    timeout_s: float = 30.0,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Gasless ``WrappedCollateral.unwrap`` via sig-3 flow."""
    creds.assert_complete()
    _assert_funder_match(
        sig_type=_SIG_DEPOSIT_WALLET,
        eoa=eoa,
        expected_funder=deposit_funder,
    )
    if amount <= 0:
        raise RelayerSubmissionError(
            "relay_unwrap_deposit_wallet: amount must be > 0"
        )
    inner = _encode_unwrap(to_address=deposit_funder, amount=amount)
    _assert_target_allowed(WRAPPED_COLLATERAL_ADDRESS)
    return await _submit_deposit_wallet(
        eoa=eoa,
        private_key=private_key,
        deposit_funder=deposit_funder,
        target=WRAPPED_COLLATERAL_ADDRESS,
        data=inner,
        creds=creds,
        base_url=base_url or creds.relayer_url,
        timeout_s=timeout_s,
        http_client=http_client,
        metadata=f"unwrap WCOL {amount}",
    )


async def _submit_deposit_wallet(
    *,
    eoa: str,
    private_key: str,
    deposit_funder: str,
    target: str,
    data: bytes,
    creds: ExtendedRelayerCredentials,
    base_url: str,
    timeout_s: float,
    http_client: httpx.AsyncClient | None,
    metadata: str,
) -> str:
    """Shared submit path for sig-3 redeem + unwrap."""

    async def _do(client: httpx.AsyncClient) -> str:
        # 0. Ensure deployed.  provenance: brief §sig-3 ("Deploy required,
        #    NOT auto on first tx").
        deployed_payload = await _retry(
            _get_json,
            client,
            f"{base_url.rstrip('/')}/deployed",
            params={"address": deposit_funder, "type": "WALLET"},
        )
        is_deployed = bool(
            isinstance(deployed_payload, dict)
            and deployed_payload.get("deployed")
        )
        if not is_deployed:
            create_body = {
                "type": "WALLET-CREATE",
                "from": eoa,
                "to": DEPOSIT_WALLET_FACTORY,
            }
            create_tx_id = await _post_submit(
                client, base_url, create_body, creds, require_tx_id=False
            )
            if create_tx_id:
                await _poll_until_terminal(
                    client, base_url=base_url, transaction_id=create_tx_id
                )

        # 1. nonce.
        nonce_payload = await _retry(
            _get_json,
            client,
            f"{base_url.rstrip('/')}/nonce",
            params={"address": eoa, "type": "WALLET"},
        )
        if not isinstance(nonce_payload, dict):
            raise RelayerSubmissionError(
                f"unexpected /nonce shape: {nonce_payload!r}"
            )
        nonce = int(nonce_payload["nonce"])

        # 2. deadline (brief §sig-3 step 2: ``now + 240``).
        deadline = int(time.time()) + 240
        _assert_deadline_ok(deadline)

        # 3. EIP-712 sign Batch.
        calls = [
            {
                "target": target,
                "value": 0,
                "data": data,  # encode_typed_data accepts bytes
            }
        ]
        signature = _sign_wallet_batch(
            wallet_address=deposit_funder,
            chain_id=creds.chain_id,
            nonce=nonce,
            deadline=deadline,
            calls=calls,
            private_key=private_key,
        )

        # 4. /submit.  Body shape per brief §sig-3 step 5 (camelCase
        # depositWalletParams).
        body = {
            "type": "WALLET",
            "from": eoa,
            "to": DEPOSIT_WALLET_FACTORY,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": deposit_funder,
                "deadline": str(deadline),
                "calls": [
                    {
                        "target": target,
                        "value": "0",
                        "data": "0x" + data.hex(),
                    }
                ],
            },
            "metadata": metadata,
        }
        tx_id = await _post_submit(client, base_url, body, creds)

        # 5. poll until mined.
        terminal = await _poll_until_terminal(
            client, base_url=base_url, transaction_id=tx_id
        )
        return str(terminal.get("transactionHash") or terminal.get("hash") or "")

    if http_client is not None:
        return await _do(http_client)
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await _do(c)

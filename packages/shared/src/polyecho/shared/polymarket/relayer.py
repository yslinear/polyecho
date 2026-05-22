"""
polyecho.shared.polymarket.relayer
==================================
Polymarket Builder-Relayer client (brief §9 — Phase-4; P3 wave concrete
gasless redeem path for ``signature_type=2`` SAFE wallets).

Status (2026-05-20)
-------------------
* **SDK upstream** — ``py-builder-relayer-client`` v0.0.1, official
  Polymarket repo ``github.com/Polymarket/py-builder-relayer-client``;
  exposes ``RelayClient`` with endpoints ``/nonce``, ``/transaction``,
  ``/transactions``, ``/submit``, ``/deployed`` (verified by inspecting
  the package source 2026-05-20).  The SDK signs SAFE EIP-712 transactions
  and POSTs to the official Builder-Relayer at
  :data:`polyecho.shared.constants.BUILDER_RELAYER_URL`
  (``https://relayer-v2.polymarket.com``).
* **What is concrete via this module** — ``signature_type=2`` SAFE redeem
  + WCOL unwrap.  ``RelayClient.execute([SafeTransaction(...)])`` is the
  only SDK-exposed dispatch path; we wrap it with async + retry/circuit.
* **What remains fail-closed** — ``signature_type=1`` MAGIC (PROXY /
  EIP-191 personal_sign) and ``signature_type=3`` POLY_1271.  The SDK
  has **no exposed surface** for these and the brief / PolyHermes
  analysis explicitly do not give a verified endpoint shape, so we keep
  the chokepoint error rather than invent a transaction format.
* **Builder-program credentials** — the relayer REQUIRES
  ``BUILDER_API_KEY`` / ``BUILDER_SECRET`` / ``BUILDER_PASS_PHRASE``
  (HMAC headers); without them the relayer rejects every submit.  This
  module fails closed with :class:`RelayerCredentialsMissingError` so a
  partially-configured deployment never silently no-ops.

Design constraints (mirrors :mod:`redeem`)
------------------------------------------
* **Import-side-effect-free** — :mod:`py_builder_relayer_client`,
  :mod:`eth_abi` and :mod:`eth_utils` are imported lazily inside the
  methods that need them; this module costs ~zero at import time and
  works in environments where the SDK is not installed (it just
  fail-closes on first use, exactly like the prior ``# TODO(verify)``
  seam did).
* **Async surface** — the upstream SDK is sync (uses ``requests``); we
  offload each call via ``asyncio.to_thread`` (same pattern as
  :mod:`polyecho.shared.polymarket.client`).
* **Retry + circuit-breaker** — same constants as
  :mod:`polyecho.shared.datasource.live` (3 attempts, exp backoff cap
  30s, breaker opens after 5 consecutive failures for 60s).  Critically,
  a ``RelayerApiException`` with a 4xx status (other than 429) is
  **deterministic** — bad nonce, undeployed safe, bad signature — and is
  surfaced immediately, *not* retried (avoiding the
  /activity-over-pagination-400 anti-pattern from ``live.py``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from polyecho.shared.constants import (
    BUILDER_RELAYER_URL,
    CTF_ADDRESS,
    MAX_UINT256,
    PARENT_COLLECTION_ID,
    POLYGON_CHAIN_ID,
    PUSD_ADDRESS,
    SAFE_MULTISEND_CALL_ONLY,
    WRAPPED_COLLATERAL_ADDRESS,
)
from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    SkipReason,
    WalletBinding,
)

__all__ = [
    "RelayerNotAvailableError",
    "RelayerCredentialsMissingError",
    "RelayerSubmissionError",
    "RelayerQuotaExceededError",
    "RelayerCredentials",
    "PolymarketRelayerClient",
    "QuotaCooldown",
    "encode_redeem_positions_calldata",
    "encode_unwrap_calldata",
    "encode_multisend_calldata",
    "relay_redeem_batch",
]

logger = logging.getLogger(__name__)

# Retry / circuit-breaker constants — mirror datasource.live.* to keep
# operator mental-model uniform (brief §9 — "polls 20s, retries, then
# circuit").  The numbers are deliberately conservative: redeem is a
# user-visible money path, not a high-throughput read.
_RETRY_ATTEMPTS = 3
_RETRY_BASE_S = 1.0
_RETRY_CAP_S = 30.0
_CB_THRESHOLD = 5
_CB_RESET_S = 60.0

# Quota-cooldown (brief A4 — relayer 429 circuit-breaker).  When the
# Builder-Relayer returns a 429 / "quota exceeded, resets in N seconds",
# we record a process-local cutoff and short-circuit NEW redeem submits
# until it expires (mirrors PolyHermes' AtomicLong cutoff + isQuotaBlocked
# check).  This sits ABOVE the existing retry/circuit logic: it prevents
# fresh submissions; an in-flight retry is unchanged.  The default is
# used only when the 429 body gives no parseable reset hint.
_QUOTA_DEFAULT_COOLDOWN_S = 60.0
# Defensive ceiling so a malformed / hostile "resets in 999999 s" hint
# cannot wedge the redeem path open forever.
_QUOTA_MAX_COOLDOWN_S = 3600.0
# Patterns that match the relayer's reset hint, e.g.
# "quota exceeded, resets in 42 seconds" / "retry after 42s".  Tried in
# order; first hit wins.  Case-insensitive.
_QUOTA_RESET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"resets?\s*in\s*(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"retry[\s-]*after\s*(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"(\d+(?:\.\d+)?)\s*seconds?", re.IGNORECASE),
)

# Polymarket signature_type for gasless SAFE wallets — the only sig the
# upstream SDK actually exposes a submit path for (verified by
# RelayClient.execute() taking only ``list[SafeTransaction]`` and
# building a SafeTransactionArgs with TransactionType.SAFE).
_SIG_SAFE = 2

# Default TX value for redeem/unwrap — neither moves native MATIC; the
# CTF and WCOL ERC-20 calls are value=0.
_ZERO_VALUE = "0"


class RelayerNotAvailableError(RuntimeError):
    """Raised when the official ``py-builder-relayer-client`` package
    is not installed in the runtime.

    Fail-closed: the relayer client refuses to import a guessed
    alternative or hand-roll the HMAC + EIP-712 signing surface.  Install
    the optional extra (``packages/shared`` ``[relayer]`` extra) or pin
    the package in deployment to enable gasless SAFE redeem.
    """


class RelayerCredentialsMissingError(RuntimeError):
    """Raised when the Builder-Relayer is reachable but the operator did
    not configure ``BUILDER_API_KEY`` / ``BUILDER_SECRET`` /
    ``BUILDER_PASS_PHRASE`` (or did not provide the EOA private key the
    SDK signs the SafeTx with).

    The relayer rejects unauthenticated submits with a 401 — this guard
    surfaces the misconfiguration *before* the network round-trip so the
    operator sees a clear cause, not a generic 4xx.
    """


class RelayerSubmissionError(RuntimeError):
    """Raised when the relayer ACKs but the transaction never reaches
    ``STATE_MINED`` / ``STATE_CONFIRMED`` within the SDK poll window, or
    reaches ``STATE_FAILED`` / ``STATE_INVALID``.

    This is distinct from :class:`RelayerCredentialsMissingError`
    (configuration) and :class:`RelayerNotAvailableError` (SDK missing)
    so the redeem-jobs worker can categorise the failure correctly.
    """


class RelayerQuotaExceededError(RelayerSubmissionError):
    """Raised when a NEW relayer redeem submission is short-circuited
    because a 429 quota-cooldown is still active (brief A4).

    Subclasses :class:`RelayerSubmissionError` so existing
    ``except RelayerSubmissionError`` handlers in the redeem path keep
    catching it; the dedicated type lets ``relay_redeem`` / ``relay_unwrap``
    tag the :class:`~polyecho.shared.copytrading.RedeemResult` with
    :attr:`~polyecho.shared.copytrading.SkipReason.QUOTA_COOLDOWN` so the
    deferral is observable on the redeem job rather than silently swallowed.

    Attributes:
        retry_after_s: Best-effort seconds until the cooldown lifts (for
            operator-facing logs / dashboards).
    """

    def __init__(self, message: str, *, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class RelayerCredentials:
    """Configuration bundle for :class:`PolymarketRelayerClient` (brief §9).

    Attributes:
        relayer_url: Full base URL (default
            :data:`polyecho.shared.constants.BUILDER_RELAYER_URL`).
        chain_id: EVM chain id (default Polygon mainnet 137).
        private_key: ``0x…`` EOA key the SDK uses to sign SafeTx
            EIP-712 hashes.  Required for ``execute()``.
        builder_api_key: HMAC header — issued by the Polymarket
            builder-program (this is **not** the same as a CLOB API key).
        builder_secret: HMAC header secret.
        builder_passphrase: HMAC header passphrase.

    All builder-* fields are **required** for ``submit_safe_transactions``
    — the relayer rejects requests missing any header with a 401.
    """

    private_key: str
    builder_api_key: str
    builder_secret: str
    builder_passphrase: str
    relayer_url: str = BUILDER_RELAYER_URL
    chain_id: int = POLYGON_CHAIN_ID


# ---------------------------------------------------------------------------
# Calldata encoding (lazy eth_abi import — module stays import-clean)
# ---------------------------------------------------------------------------


def _function_selector(signature: str) -> bytes:
    """Solidity 4-byte function selector ``keccak256(sig)[:4]``."""
    from eth_utils import keccak  # type: ignore[attr-defined] # lazy; transitive via web3.py

    result: bytes = keccak(text=signature)[:4]
    return result


def encode_redeem_positions_calldata(
    *,
    collateral: str,
    parent_collection_id: str,
    condition_id: str,
    index_sets: list[int],
) -> str:
    """Encode ``CTF.redeemPositions(address,bytes32,bytes32,uint256[])``.

    Returns the ``0x…`` hex string accepted by the relayer's
    ``SafeTransaction.data`` field.  The 4-byte selector + ABI-encoded
    args match the Gnosis-CTF surface used by Polymarket (verified
    against ``CTF_REDEEM_ABI`` in :mod:`redeem` — same shape, same
    function name).
    """
    from eth_abi import encode  # type: ignore[attr-defined] # lazy
    from eth_utils import to_bytes  # type: ignore[attr-defined] # lazy

    selector = _function_selector("redeemPositions(address,bytes32,bytes32,uint256[])")
    body: bytes = encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            collateral,
            to_bytes(hexstr=parent_collection_id),
            to_bytes(hexstr=condition_id),
            index_sets,
        ],
    )
    return "0x" + (selector + body).hex()


def encode_unwrap_calldata(*, to_address: str, amount: int) -> str:
    """Encode ``WrappedCollateral.unwrap(address,uint256)``.

    Returns the ``0x…`` hex string accepted by the relayer's
    ``SafeTransaction.data`` field.
    """
    from eth_abi import encode  # type: ignore[attr-defined] # lazy

    selector = _function_selector("unwrap(address,uint256)")
    body: bytes = encode(["address", "uint256"], [to_address, amount])
    return "0x" + (selector + body).hex()


def encode_erc20_approve_calldata(*, spender: str, amount: int) -> str:
    """Encode ``ERC20.approve(address,uint256)`` (utility for the
    one-time max-allowance setup; mirrors :data:`redeem.ERC20_APPROVE_ABI`)."""
    from eth_abi import encode  # type: ignore[attr-defined] # lazy

    selector = _function_selector("approve(address,uint256)")
    body: bytes = encode(["address", "uint256"], [spender, amount])
    return "0x" + (selector + body).hex()


def encode_multisend_calldata(calls: list[tuple[str, str]]) -> str:
    """Encode ``MultiSendCallOnly.multiSend(bytes)`` for a batch of calls.

    Deep-dive B6 — batch SAFE redeems via MultiSend so many resolved
    markets settle in ONE ``execTransaction`` (lower gas + fewer relayer
    quota hits).

    ``calls`` is a list of ``(to, data)`` pairs.  Each inner call is
    **always** ``operation=0`` (Call — never DelegateCall: the canonical
    ``MultiSendCallOnly`` contract reverts on a non-zero inner operation,
    a defence-in-depth match for our redeem-only use) and **value=0** (CTF
    ``redeemPositions`` and WCOL ``unwrap`` never move native MATIC).

    The packed encoding is the Gnosis-Safe ``MultiSend`` ABI:
    each transaction is ``operation(1) || to(20) || value(32) ||
    dataLength(32) || data`` concatenated, then ABI-wrapped as a single
    ``bytes`` argument to ``multiSend(bytes)``.  Returns the ``0x…`` hex
    string for the outer ``SafeTransaction.data`` field; the Safe must
    invoke ``MultiSendCallOnly`` with ``operation=DelegateCall`` so the
    batched calls run in the Safe's own context.

    Raises:
        ValueError: If *calls* is empty (caller must not batch nothing).
    """
    from eth_abi import encode  # type: ignore[attr-defined] # lazy
    from eth_utils import to_bytes  # type: ignore[attr-defined] # lazy

    if not calls:
        raise ValueError("encode_multisend_calldata: refuse to encode an empty batch")
    packed = b""
    for to_addr, data_hex in calls:
        data_bytes = to_bytes(hexstr=data_hex)
        packed += (
            (0).to_bytes(1, "big")  # operation = Call (CallOnly enforces)
            + bytes.fromhex(to_addr[2:] if to_addr.startswith("0x") else to_addr)
            + (0).to_bytes(32, "big")  # value = 0
            + len(data_bytes).to_bytes(32, "big")
            + data_bytes
        )
    selector = _function_selector("multiSend(bytes)")
    body: bytes = encode(["bytes"], [packed])
    return "0x" + (selector + body).hex()


# ---------------------------------------------------------------------------
# Quota-cooldown circuit-breaker (brief A4)
# ---------------------------------------------------------------------------


def _api_exception_body(exc: Exception) -> str | None:
    """Best-effort extract of the human-readable body from a relayer
    ``RelayerApiException``.

    The upstream SDK is not strict about which attribute carries the
    server message, so we probe the common shapes (``error_msg``,
    ``message``, ``body``, ``text``, ``detail``) and finally fall back to
    ``str(exc)``.  Used only to mine the "resets in N s" reset hint — a
    miss just means the default cooldown is applied.
    """
    for attr in ("error_msg", "message", "body", "text", "detail"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    text = str(exc)
    return text or None


def _parse_reset_hint(body: str | None) -> float | None:
    """Best-effort parse of "resets in N s" from a 429 response body.

    The Builder-Relayer's 429 body may carry a human reset hint such as
    "quota exceeded, resets in 42 seconds".  We extract the first numeric
    seconds value; ``None`` if the body is empty or unparseable (the
    caller then falls back to :data:`_QUOTA_DEFAULT_COOLDOWN_S`).
    """
    if not body:
        return None
    for pattern in _QUOTA_RESET_PATTERNS:
        match = pattern.search(body)
        if match is not None:
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


class QuotaCooldown:
    """Process-local 429 quota-cooldown gate (brief A4).

    Mirrors PolyHermes' ``AtomicLong`` cutoff + ``isQuotaBlocked()`` check.
    On a 429 the relayer records a monotonic cutoff via :meth:`trip`; a new
    redeem submission calls :meth:`blocked` first and is short-circuited
    while the cutoff is still in the future.  Deliberately in-memory only
    (no DB) — exactly like PolyHermes; a restart simply clears it, which is
    safe because the durable ``redeem_jobs`` row stays ``pending`` and is
    re-polled.

    The gate sits ABOVE the existing retry / circuit-breaker: it prevents
    NEW submissions; an in-flight retry inside :meth:`_run_with_retry` is
    unchanged.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_cooldown_s: float = _QUOTA_DEFAULT_COOLDOWN_S,
        max_cooldown_s: float = _QUOTA_MAX_COOLDOWN_S,
    ) -> None:
        self._clock = clock
        self._default_cooldown_s = default_cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._cutoff: float = 0.0  # monotonic seconds; 0 ⇒ never tripped

    def trip(self, *, body: str | None = None) -> float:
        """Record a cooldown after a 429; return the effective cooldown (s).

        Parses "resets in N s" from *body* when present; otherwise uses the
        default backoff.  The value is clamped to ``max_cooldown_s`` so a
        malformed or hostile hint cannot wedge the redeem path shut
        forever.  A shorter freshly-parsed hint never shortens an
        already-longer active cutoff (we keep the later of the two).
        """
        hinted = _parse_reset_hint(body)
        cooldown = self._default_cooldown_s if hinted is None else hinted
        cooldown = max(0.0, min(cooldown, self._max_cooldown_s))
        new_cutoff = self._clock() + cooldown
        self._cutoff = max(self._cutoff, new_cutoff)
        return cooldown

    def blocked(self) -> bool:
        """True iff the cooldown is still active (PolyHermes isQuotaBlocked)."""
        return self._cutoff > self._clock()

    def retry_after_s(self) -> float:
        """Seconds remaining on the cooldown (``0.0`` once expired)."""
        return max(0.0, self._cutoff - self._clock())


# ---------------------------------------------------------------------------
# Async wrapper around py-builder-relayer-client.RelayClient
# ---------------------------------------------------------------------------


class PolymarketRelayerClient:
    """Async wrapper around the official ``py-builder-relayer-client``
    SDK (brief §9 — Phase-4 P3 concrete gasless redeem path).

    Lifecycle is **stateless from the caller's view** — the SDK's
    ``RelayClient`` is constructed once per :class:`PolymarketRelayerClient`
    instance (the SDK does its own ``requests`` session management), and
    every async method offloads the sync call via ``asyncio.to_thread``.

    Args:
        credentials: Configuration bundle (URL, chain id, EOA key,
            builder API key/secret/passphrase).
        max_polls: How many ``poll_until_state`` ticks before declaring
            the relayer submission failed (the SDK's default is 10 at
            2s/tick = 20s; we default to 30 at 2s = 60s, matching the
            brief's "polls ~20s for WCOL balance" relax window).

    Raises:
        RelayerNotAvailableError: At first use if the SDK package is
            not installed.
        RelayerCredentialsMissingError: At construction if any of the
            three builder-* fields or the private key is empty.
    """

    def __init__(
        self,
        credentials: RelayerCredentials,
        *,
        max_polls: int = 30,
        quota_cooldown: QuotaCooldown | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("private_key", credentials.private_key),
                ("builder_api_key", credentials.builder_api_key),
                ("builder_secret", credentials.builder_secret),
                ("builder_passphrase", credentials.builder_passphrase),
            )
            if not value
        ]
        if missing:
            raise RelayerCredentialsMissingError(
                f"PolymarketRelayerClient: missing required builder "
                f"credentials: {missing!r}.  Set BUILDER_API_KEY, "
                f"BUILDER_SECRET, BUILDER_PASS_PHRASE and the EOA "
                f"signing key before enabling gasless SAFE redeem."
            )
        self._creds = credentials
        self._max_polls = max_polls
        self._client: Any | None = None  # lazy py_builder_relayer_client.RelayClient

        # Circuit-breaker state — mirrors LiveDataSource so operator
        # dashboards report the same shape across read + write paths.
        self._cb_failures: int = 0
        self._cb_open_until: float = 0.0  # monotonic seconds

        # Quota-cooldown gate (brief A4) — process-local 429 circuit-
        # breaker.  Tripped inside _run_with_retry on a 429; checked by
        # submit_safe_transaction before a NEW submission.
        self._quota: QuotaCooldown = quota_cooldown or QuotaCooldown()

    # ------------------------------------------------------------------
    # SDK construction (lazy — keeps import side-effect-free)
    # ------------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            # Lazy import: this module must be importable even on hosts
            # that have not installed the optional relayer SDK.
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RelayerNotAvailableError(
                "py-builder-relayer-client is not installed.  Install "
                "`py-builder-relayer-client>=0.0.1` to enable gasless "
                "SAFE redeem (signature_type=2).  EOA (signature_type=0) "
                "remains the only concrete path without this dependency."
            ) from exc

        builder_config = BuilderConfig(
            api_key=self._creds.builder_api_key,
            secret=self._creds.builder_secret,
            passphrase=self._creds.builder_passphrase,
        )
        self._client = RelayClient(
            relayer_url=self._creds.relayer_url,
            chain_id=self._creds.chain_id,
            private_key=self._creds.private_key,
            builder_config=builder_config,
        )
        return self._client

    # ------------------------------------------------------------------
    # Circuit breaker (same shape as datasource.live for uniformity)
    # ------------------------------------------------------------------

    def _check_circuit(self) -> None:
        now = time.monotonic()
        if self._cb_open_until > now:
            raise RelayerSubmissionError(
                f"PolymarketRelayerClient circuit breaker is OPEN "
                f"(retry after {self._cb_open_until - now:.0f}s)."
            )

    def _record_success(self) -> None:
        self._cb_failures = 0

    def _record_failure(self) -> None:
        self._cb_failures += 1
        if self._cb_failures >= _CB_THRESHOLD:
            self._cb_open_until = time.monotonic() + _CB_RESET_S
            logger.warning(
                "relayer_circuit_breaker_opened",
                extra={
                    "failures": self._cb_failures,
                    "reset_s": _CB_RESET_S,
                },
            )

    # ------------------------------------------------------------------
    # Quota-cooldown gate (brief A4 — 429 quota circuit-breaker)
    # ------------------------------------------------------------------

    def quota_blocked(self) -> bool:
        """True iff a 429 quota-cooldown is still active (PolyHermes
        ``isQuotaBlocked()``).  A NEW redeem submission should be skipped /
        deferred while this holds."""
        return self._quota.blocked()

    def quota_retry_after_s(self) -> float:
        """Seconds remaining on the active 429 quota-cooldown (``0.0`` if
        not blocked) — for operator-facing logs / redeem-job detail."""
        return self._quota.retry_after_s()

    def _check_quota(self) -> None:
        """Short-circuit a NEW submission while the 429 cooldown holds.

        Raised BEFORE any network round-trip — this is the whole point of
        the breaker: stop hammering the relayer with submissions it will
        only 429 again.  Distinct from :meth:`_check_circuit` (the generic
        consecutive-failure breaker)."""
        if self._quota.blocked():
            retry_after = self._quota.retry_after_s()
            raise RelayerQuotaExceededError(
                f"PolymarketRelayerClient: relayer 429 quota-cooldown is "
                f"active; new redeem submission skipped "
                f"(retry after {retry_after:.0f}s).",
                retry_after_s=retry_after,
            )

    # ------------------------------------------------------------------
    # Internal sync-call offloader with retry + deterministic-error gate
    # ------------------------------------------------------------------

    async def _run_with_retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Offload a sync SDK call to a thread with retry + circuit.

        4xx (other than 429) is treated as a deterministic client error
        (bad nonce, undeployed safe, malformed signature) and surfaced
        immediately — retrying is futile and would burn the circuit on
        an upstream-healthy condition.
        """
        from py_builder_relayer_client.exceptions import RelayerApiException

        self._check_circuit()
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                result = await asyncio.to_thread(fn, *args, **kwargs)
                self._record_success()
                return result
            except RelayerApiException as exc:
                status = getattr(exc, "status_code", None)
                # Deterministic 4xx (not 429) ⇒ surface immediately.
                if status is not None and 400 <= status < 500 and status != 429:
                    raise RelayerSubmissionError(f"Relayer 4xx (not retried): {exc!r}") from exc
                # 429 quota exceeded ⇒ trip the process-local cooldown so
                # the NEXT submission is short-circuited (brief A4).  The
                # in-flight call still finishes its retry budget below;
                # the cooldown only gates fresh submissions.
                if status == 429:
                    body = _api_exception_body(exc)
                    cooldown = self._quota.trip(body=body)
                    logger.warning(
                        "relayer_quota_cooldown_tripped",
                        extra={
                            "cooldown_s": cooldown,
                            "parsed_hint": _parse_reset_hint(body) is not None,
                            "fn": getattr(fn, "__name__", repr(fn)),
                        },
                    )
                last_exc = exc
                self._record_failure()
            except Exception as exc:  # transport / DNS / TLS / unexpected
                last_exc = exc
                self._record_failure()

            if attempt < _RETRY_ATTEMPTS - 1:
                sleep_s = min(_RETRY_CAP_S, _RETRY_BASE_S * math.pow(2, attempt))
                logger.debug(
                    "relayer_http_retry",
                    extra={
                        "attempt": attempt + 1,
                        "sleep_s": sleep_s,
                        "fn": getattr(fn, "__name__", repr(fn)),
                    },
                )
                await asyncio.sleep(sleep_s)

        raise RelayerSubmissionError(
            f"Relayer call {getattr(fn, '__name__', fn)!r} failed after "
            f"{_RETRY_ATTEMPTS} attempts: {last_exc!r}"
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def is_safe_deployed(self, safe_address: str) -> bool:
        """``GET /deployed?address=…`` (SDK ``get_deployed``).

        Used by the redeem worker to short-circuit when a wallet has not
        yet been deployed (the SDK's own ``execute`` raises in that case;
        this lets us surface a tidy ``SkipReason`` upstream).
        """
        client = self._ensure_client()
        return bool(await self._run_with_retry(client.get_deployed, safe_address))

    async def get_safe_nonce(self, signer_address: str) -> int:
        """``GET /nonce?address=…&type=SAFE`` (SDK ``get_nonce``).

        The SDK consumes this internally inside ``execute()``; we expose
        it for the operator-visible health probe and tests.
        """
        client = self._ensure_client()
        payload = await self._run_with_retry(client.get_nonce, signer_address, "SAFE")
        if payload is None or payload.get("nonce") is None:
            raise RelayerSubmissionError(f"Relayer returned an invalid nonce payload: {payload!r}")
        return int(payload["nonce"])

    async def submit_safe_transaction(
        self,
        *,
        to: str,
        data: str,
        value: str = _ZERO_VALUE,
        metadata: str | None = None,
        wait_for_confirmation: bool = True,
    ) -> dict[str, Any]:
        """Submit a single ``SafeTransaction`` via ``RelayClient.execute``.

        ``execute()`` signs the SafeTx EIP-712 hash with the configured
        EOA private key, attaches the builder HMAC headers, and POSTs
        ``/submit``.  When ``wait_for_confirmation`` is True we then poll
        ``/transaction`` until STATE_MINED / STATE_CONFIRMED (or fail).

        Args:
            to: Target contract (CTF for redeem, WCOL for unwrap).
            data: ``0x…`` ABI-encoded calldata (use
                :func:`encode_redeem_positions_calldata` etc.).
            value: Native MATIC value (default ``"0"``).
            metadata: Free-form audit string forwarded to the relayer.
            wait_for_confirmation: If True (default) poll until terminal
                state; otherwise return as soon as the relayer ACKs.

        Returns:
            Dict with at minimum ``transaction_id``, ``transaction_hash``,
            and (when ``wait_for_confirmation``) ``state`` + raw
            ``transaction`` payload from the relayer.

        Raises:
            RelayerQuotaExceededError: If a 429 quota-cooldown from a prior
                submission is still active — the NEW submission is
                short-circuited (brief A4) before any network round-trip.
        """
        from py_builder_relayer_client.models import OperationType, SafeTransaction

        # Brief A4 — short-circuit a NEW submission while the 429 quota-
        # cooldown holds, instead of hammering the relayer.  Checked
        # before _ensure_client / network I/O.
        self._check_quota()

        client = self._ensure_client()
        safe_tx = SafeTransaction(
            to=to,
            operation=OperationType.Call,
            data=data,
            value=value,
        )
        # execute() does network I/O + retries via our wrapper.
        response = await self._run_with_retry(client.execute, [safe_tx], metadata)
        result: dict[str, Any] = {
            "transaction_id": getattr(response, "transaction_id", None),
            "transaction_hash": getattr(response, "transaction_hash", None),
        }
        if not wait_for_confirmation:
            return result

        # SDK's ClientRelayerTransactionResponse.wait() is sync + polls
        # /transaction every 2s up to max_polls.  Offload to a thread.
        terminal = await asyncio.to_thread(response.wait)
        result["transaction"] = terminal
        result["state"] = terminal.get("state") if isinstance(terminal, dict) else None
        if terminal is None:
            raise RelayerSubmissionError(
                f"Relayer transaction {result['transaction_id']!r} did not "
                f"reach a terminal state within the poll window."
            )
        return result

    async def submit_safe_multisend(
        self,
        *,
        calls: list[tuple[str, str]],
        metadata: str | None = None,
        wait_for_confirmation: bool = True,
    ) -> dict[str, Any]:
        """Submit a batch of calls as ONE ``execTransaction`` via MultiSend.

        Deep-dive B6 — batch SAFE redeems so N resolved markets settle in a
        single relayer transaction (lower gas, fewer relayer-quota hits).

        The SDK exposes no dedicated MultiSend primitive, but it does NOT
        need one: a Gnosis-Safe batch is just an ordinary ``SafeTransaction``
        whose ``to`` is the canonical ``MultiSendCallOnly`` contract and
        whose ``operation`` is ``DelegateCall`` (so the packed inner calls
        run in the Safe's own context).  We reuse the exact same
        ``RelayClient.execute([SafeTransaction(...)])`` dispatch, retry,
        circuit-breaker and 429 quota-cooldown gate as
        :meth:`submit_safe_transaction` — the only difference is the
        operation type and the MultiSend-encoded calldata.

        Args:
            calls: ``[(to, data)]`` inner calls (e.g. one CTF
                ``redeemPositions`` per resolved market).  Each runs as
                ``operation=Call`` value=0 inside the batch.
            metadata: Free-form audit string forwarded to the relayer.
            wait_for_confirmation: If True (default) poll until terminal
                state; otherwise return as soon as the relayer ACKs.

        Returns:
            Same dict shape as :meth:`submit_safe_transaction`.

        Raises:
            ValueError: If *calls* is empty.
            RelayerQuotaExceededError: If a 429 quota-cooldown is active —
                the NEW batch submission is short-circuited (brief A4).
        """
        from py_builder_relayer_client.models import OperationType, SafeTransaction

        if not calls:
            raise ValueError("submit_safe_multisend: refuse to submit an empty batch")

        # Brief A4 — same quota-cooldown gate as the single-tx path; a
        # batched submit is still ONE relayer submission and must respect
        # the 429 cooldown before any network I/O.
        self._check_quota()

        client = self._ensure_client()
        data = encode_multisend_calldata(calls)
        safe_tx = SafeTransaction(
            to=SAFE_MULTISEND_CALL_ONLY,
            operation=OperationType.DelegateCall,
            data=data,
            value=_ZERO_VALUE,
        )
        response = await self._run_with_retry(client.execute, [safe_tx], metadata)
        result: dict[str, Any] = {
            "transaction_id": getattr(response, "transaction_id", None),
            "transaction_hash": getattr(response, "transaction_hash", None),
        }
        if not wait_for_confirmation:
            return result

        terminal = await asyncio.to_thread(response.wait)
        result["transaction"] = terminal
        result["state"] = terminal.get("state") if isinstance(terminal, dict) else None
        if terminal is None:
            raise RelayerSubmissionError(
                f"Relayer multisend transaction {result['transaction_id']!r} "
                f"did not reach a terminal state within the poll window."
            )
        return result


# ---------------------------------------------------------------------------
# High-level redeem / unwrap helpers (used by Web3RedeemClient._relay_*)
# ---------------------------------------------------------------------------


async def relay_redeem(
    relayer: PolymarketRelayerClient,
    spec: RedeemJobSpec,
    *,
    binding: WalletBinding,
) -> RedeemResult:
    """Submit one CTF ``redeemPositions`` via the Builder-Relayer.

    Restricted to ``signature_type=2`` SAFE wallets — the only sig the
    upstream SDK exposes.  MAGIC (sig 1) and POLY_1271 (sig 3) raise
    a clear error in :meth:`Web3RedeemClient._relay_redeem`.

    The collateral choice is the brief's highest-risk detail and follows
    :func:`polyecho.shared.polymarket.redeem.collateral_for` (pUSD for
    normal markets, WrappedCollateral for NegRisk).
    """
    if binding.signature_type != _SIG_SAFE:
        raise RelayerSubmissionError(
            f"relay_redeem: signature_type={binding.signature_type} "
            f"(wallet_mode={binding.wallet_mode!r}) has no Builder-Relayer "
            f"SDK surface; only SAFE (signature_type=2) is concrete."
        )
    collateral = WRAPPED_COLLATERAL_ADDRESS if spec.is_neg_risk else PUSD_ADDRESS
    data = encode_redeem_positions_calldata(
        collateral=collateral,
        parent_collection_id=PARENT_COLLECTION_ID,
        condition_id=spec.condition_id,
        index_sets=[spec.index_set],
    )
    metadata = (
        f"polyecho-redeem account_id={spec.account_id} "
        f"condition_id={spec.condition_id} "
        f"outcome_index={spec.outcome_index} "
        f"neg_risk={spec.is_neg_risk}"
    )
    try:
        resp = await relayer.submit_safe_transaction(
            to=CTF_ADDRESS,
            data=data,
            metadata=metadata,
        )
    except RelayerQuotaExceededError as exc:
        # Brief A4 — a 429 quota-cooldown is active.  This is a DEFERRAL,
        # not a failure: keep the job ``pending`` (not ``failed``, which
        # would burn a retry attempt) and surface the reason so the
        # deferral is observable on the redeem job, not silently swallowed.
        logger.info(
            "redeem_deferred_quota_cooldown",
            extra={
                "account_id": str(spec.account_id),
                "condition_id": spec.condition_id,
                "retry_after_s": exc.retry_after_s,
            },
        )
        return RedeemResult(
            status=RedeemJobStatus.PENDING,
            skip_reason=SkipReason.QUOTA_COOLDOWN,
            detail=(
                f"redeem deferred: relayer 429 quota-cooldown active, "
                f"retry after {exc.retry_after_s:.0f}s; no submission made."
            ),
        )
    except RelayerSubmissionError as exc:
        return RedeemResult(
            status=RedeemJobStatus.FAILED,
            detail=f"redeem via relayer failed: {exc!r}",
        )
    return RedeemResult(
        status=RedeemJobStatus.CONFIRMED,
        tx_hash=resp.get("transaction_hash"),
        detail=(
            f"redeemPositions via Builder-Relayer "
            f"(transaction_id={resp.get('transaction_id')!r}, "
            f"state={resp.get('state')!r}, "
            f"collateral={'WCOL' if spec.is_neg_risk else 'pUSD'})"
        ),
    )


async def relay_redeem_batch(
    relayer: PolymarketRelayerClient,
    specs: Sequence[RedeemJobSpec],
    *,
    binding: WalletBinding,
) -> dict[tuple[str, int], RedeemResult]:
    """Batch many CTF ``redeemPositions`` for one SAFE account into ONE tx.

    Deep-dive B6 — when a SAFE account has several resolved markets ready
    to redeem, settle them all in a single MultiSend ``execTransaction``
    instead of N separate relayer submissions (lower gas, fewer relayer
    quota hits).  This is SAFE-ONLY: Magic (sig 1) is excluded by design
    (PolyHermes excludes it too) — the caller keeps Magic sequential.

    Per-job idempotency is preserved: the return value maps each job's
    ``(condition_id, outcome_index)`` key to its own :class:`RedeemResult`,
    so every ``(account_id, condition_id, outcome_index)`` ``redeem_jobs``
    row is still individually tracked / status-written even though the N
    jobs shared one on-chain transaction (they all carry the same
    ``tx_hash``).  A batch-level failure / quota-deferral fans the SAME
    terminal status out to every job — no job is silently dropped.

    A single-element *specs* is delegated to :func:`relay_redeem` so the
    common (one resolved market) case is unchanged and never pays the
    MultiSend wrapper overhead.

    Args:
        relayer: Configured :class:`PolymarketRelayerClient`.
        specs:   ≥1 redeem jobs, ALL for the same SAFE *account_id*.
        binding: The account's SAFE wallet binding (``signature_type=2``).

    Returns:
        ``{(condition_id, outcome_index): RedeemResult}`` — one entry per
        input spec, in 1:1 correspondence.

    Raises:
        ValueError: If *specs* is empty, or the specs span more than one
            ``account_id`` (batching is per-account by contract).
        RelayerSubmissionError: If *binding* is not a SAFE wallet — Magic
            (sig 1) must never be batched.
    """
    if not specs:
        raise ValueError("relay_redeem_batch: specs must be non-empty")
    if binding.signature_type != _SIG_SAFE:
        # Defence-in-depth: Magic (sig 1) / POLY_1271 (sig 3) must NEVER
        # reach the MultiSend path — PolyHermes explicitly excludes sig 1
        # from batching and the SDK has no surface for sig 1/3 anyway.
        raise RelayerSubmissionError(
            f"relay_redeem_batch: signature_type={binding.signature_type} "
            f"(wallet_mode={binding.wallet_mode!r}) cannot be batched; "
            f"MultiSend redeem batching is SAFE-only (signature_type=2). "
            f"Magic (sig 1) must stay sequential."
        )
    account_ids = {spec.account_id for spec in specs}
    if len(account_ids) != 1:
        raise ValueError(
            f"relay_redeem_batch: all specs must share one account_id, "
            f"got {sorted(str(a) for a in account_ids)!r}"
        )

    # Single resolved market ⇒ no MultiSend overhead; identical to the
    # pre-B6 one-tx-per-job path.
    if len(specs) == 1:
        only = specs[0]
        result = await relay_redeem(relayer, only, binding=binding)
        return {(only.condition_id, only.outcome_index): result}

    calls: list[tuple[str, str]] = []
    for spec in specs:
        collateral = WRAPPED_COLLATERAL_ADDRESS if spec.is_neg_risk else PUSD_ADDRESS
        calls.append(
            (
                CTF_ADDRESS,
                encode_redeem_positions_calldata(
                    collateral=collateral,
                    parent_collection_id=PARENT_COLLECTION_ID,
                    condition_id=spec.condition_id,
                    index_sets=[spec.index_set],
                ),
            )
        )
    account_id = specs[0].account_id
    metadata = f"polyecho-redeem-batch account_id={account_id} n_jobs={len(specs)}"
    keys = [(spec.condition_id, spec.outcome_index) for spec in specs]
    try:
        resp = await relayer.submit_safe_multisend(
            calls=calls,
            metadata=metadata,
        )
    except RelayerQuotaExceededError as exc:
        # Brief A4 — 429 quota-cooldown active.  Fan a DEFERRAL out to
        # EVERY job in the batch (keep them all ``pending``, not
        # ``failed``); the whole batch is re-attempted on a later poll.
        logger.info(
            "redeem_batch_deferred_quota_cooldown",
            extra={
                "account_id": str(account_id),
                "n_jobs": len(specs),
                "retry_after_s": exc.retry_after_s,
            },
        )
        deferred = RedeemResult(
            status=RedeemJobStatus.PENDING,
            skip_reason=SkipReason.QUOTA_COOLDOWN,
            detail=(
                f"batch redeem deferred: relayer 429 quota-cooldown active, "
                f"retry after {exc.retry_after_s:.0f}s; no submission made."
            ),
        )
        return {key: deferred for key in keys}
    except RelayerSubmissionError as exc:
        # A batch-level submit failure fans the SAME ``failed`` status to
        # every job — none is silently dropped; each row is re-tried under
        # its own bounded ``max_attempts`` budget on the next poll.
        failed = RedeemResult(
            status=RedeemJobStatus.FAILED,
            detail=f"batch redeem via relayer failed: {exc!r}",
        )
        return {key: failed for key in keys}

    tx_hash = resp.get("transaction_hash")
    out: dict[tuple[str, int], RedeemResult] = {}
    for spec, key in zip(specs, keys, strict=True):
        # Every job gets the SAME shared tx_hash but its OWN result row so
        # per-(account,condition,outcome) status tracking is preserved.
        out[key] = RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=(
                f"redeemPositions via Builder-Relayer MultiSend batch "
                f"(n_jobs={len(specs)}, "
                f"transaction_id={resp.get('transaction_id')!r}, "
                f"state={resp.get('state')!r}, "
                f"collateral={'WCOL' if spec.is_neg_risk else 'pUSD'})"
            ),
        )
    return out


async def relay_unwrap(
    relayer: PolymarketRelayerClient,
    *,
    binding: WalletBinding,
    amount: int,
) -> RedeemResult:
    """Submit one ``WrappedCollateral.unwrap`` via the Builder-Relayer.

    Restricted to SAFE wallets; same sig constraints as
    :func:`relay_redeem`.
    """
    if binding.signature_type != _SIG_SAFE:
        raise RelayerSubmissionError(
            f"relay_unwrap: signature_type={binding.signature_type} "
            f"(wallet_mode={binding.wallet_mode!r}) has no Builder-Relayer "
            f"SDK surface; only SAFE (signature_type=2) is concrete."
        )
    if amount <= 0:
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            detail="relay_unwrap skipped: WCOL balance is 0",
        )
    if amount > MAX_UINT256:
        raise RelayerSubmissionError(f"relay_unwrap: amount {amount} exceeds uint256 max")
    data = encode_unwrap_calldata(to_address=binding.funder, amount=amount)
    metadata = f"polyecho-unwrap funder={binding.funder} amount={amount}"
    try:
        resp = await relayer.submit_safe_transaction(
            to=WRAPPED_COLLATERAL_ADDRESS,
            data=data,
            metadata=metadata,
        )
    except RelayerQuotaExceededError as exc:
        # Brief A4 — 429 quota-cooldown active; defer (keep ``pending``),
        # do not mark ``failed``.  The NegRisk unwrap will be retried on a
        # later poll once the cooldown lifts.
        logger.info(
            "unwrap_deferred_quota_cooldown",
            extra={
                "funder": binding.funder,
                "amount": amount,
                "retry_after_s": exc.retry_after_s,
            },
        )
        return RedeemResult(
            status=RedeemJobStatus.PENDING,
            skip_reason=SkipReason.QUOTA_COOLDOWN,
            detail=(
                f"unwrap deferred: relayer 429 quota-cooldown active, "
                f"retry after {exc.retry_after_s:.0f}s; no submission made."
            ),
        )
    except RelayerSubmissionError as exc:
        return RedeemResult(
            status=RedeemJobStatus.FAILED,
            detail=f"unwrap via relayer failed: {exc!r}",
        )
    return RedeemResult(
        status=RedeemJobStatus.CONFIRMED,
        tx_hash=resp.get("transaction_hash"),
        detail=(
            f"WCOL.unwrap via Builder-Relayer (amount={amount}, "
            f"transaction_id={resp.get('transaction_id')!r}, "
            f"state={resp.get('state')!r})"
        ),
    )

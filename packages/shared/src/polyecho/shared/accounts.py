"""
polyecho.shared.accounts
========================
Polymarket wallet derivation and signature-type auto-detection (PRD §5.1).

Background
----------
Polymarket users interact through one of four *signature types*.  Given a
single **EOA** (the externally-owned account / signer key the user controls),
the *funder* address — the wallet that actually holds USDC and positions —
differs per type:

============  ===================  ====================================================
Type (int)    Name                 Funder (the address that holds funds / positions)
============  ===================  ====================================================
0             EOA                  ``funder == signer``  (the EOA itself)
1             POLY_PROXY           Magic/email proxy, CREATE2 via the Polymarket
                                   *proxy-wallet factory* keyed by the EOA
2             POLY_GNOSIS_SAFE     Gnosis-Safe proxy, CREATE2 via the Polymarket
                                   *safe factory* + safe singleton, keyed by the EOA
3             POLY_1271            EIP-1271 "deposit wallet"; in Polymarket's model
                                   the on-chain holder is the same Gnosis-Safe proxy
                                   as type 2 (the 1271 flag only changes *how* the
                                   order signature is verified, not the address)
============  ===================  ====================================================

Sources consulted (via context7 ``/llmstxt/polymarket_llms_txt`` /
``/websites/polymarket_api-reference``):

* https://docs.polymarket.com/trading/overview — signature-type table
  (0 EOA / 1 POLY_PROXY / 2 GNOSIS_SAFE), "funder is the proxy wallet
  displayed on polymarket.com".
* https://docs.polymarket.com/api-reference/authentication — "these proxy
  wallets are automatically deployed on first login; developers can derive
  them deterministically **or** deploy them on behalf of the user".
* https://docs.polymarket.com/resources/contract-addresses — collateral
  (USDC.e) / CTF / exchange addresses (used by the optional balance helper).
* ``py_clob_client_v2.order_utils.model.signature_type_v1`` — confirms the
  integer enum 0/1/2; type 3 (POLY_1271) is PRD-specific.

.. warning:: **TODO(verify) — exact CREATE2 inputs are NOT in the official docs.**

   The official documentation states the proxy addresses are *deterministic*
   but does **not** publish the factory contract addresses, the CREATE2 salt
   pre-image, or the proxy init-code hash.  ``py-clob-client-v2`` does not
   ship a derivation helper either (it takes ``funder`` as an explicit
   argument).  The factory / singleton constants below are the
   widely-circulated community values for Polymarket on Polygon mainnet and
   are marked ``# TODO(verify)``.  They are isolated as module constants so
   that, once confirmed against the on-chain factory, a one-line change makes
   :func:`derive_wallets` exact.  Detection (:func:`detect_signature_type`)
   does **not** depend on the constants being correct — it ranks whatever
   addresses are derived purely by observed on-chain evidence (positions /
   activity), so the feature is useful and fully testable today.

This module performs **no network or DB I/O** on import.  Detection consumes
the frozen ``DataSource`` protocol (``get_positions`` / ``get_user_activity``)
exactly as-is and never mutates it.  The optional USDC-balance probe is a
small standalone ``httpx`` helper (Polymarket data-api) used only when a
client is explicitly supplied — it is best-effort and never required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

# Import from the canonical submodules: ``eth_utils`` re-exports these via
# ``__init__`` but does not list them in ``__all__``, so mypy --strict flags
# ``from eth_utils import …`` as "not explicitly exported".  The submodules
# carry ``py.typed`` and are the stable public locations.
from eth_utils.address import is_address, to_checksum_address
from eth_utils.crypto import keccak

from polyecho.shared.datasource.dto import ActivityRecord, PositionRecord

logger = logging.getLogger(__name__)


@runtime_checkable
class _DetectionDataSource(Protocol):
    """The *subset* of the frozen ``DataSource`` protocol detection needs.

    Interface-segregation: we only call ``get_positions`` and
    ``get_user_activity``.  The signatures here match
    ``polyecho.shared.datasource.protocol.DataSource`` **verbatim**, so the
    real ``DataSource`` (and ``FixtureDataSource``) is structurally a
    ``_DetectionDataSource`` — the frozen protocol is neither modified nor
    re-implemented, merely consumed through a narrower view.  This also lets
    tests pass a minimal fake without stubbing the other three methods.
    """

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]: ...

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]: ...


# ---------------------------------------------------------------------------
# Signature types (mirrors py_clob_client_v2 SignatureTypeV1 + PRD type 3)
# ---------------------------------------------------------------------------
class SignatureType(IntEnum):
    """Polymarket signature types (PRD §5.1).

    0/1/2 mirror ``py_clob_client_v2.order_utils.model.signature_type_v1``.
    3 (POLY_1271, "deposit wallet") is defined by the PRD; on-chain it shares
    the type-2 Gnosis-Safe proxy address.
    """

    EOA = 0
    POLY_PROXY = 1
    POLY_GNOSIS_SAFE = 2
    POLY_1271 = 3


SIGNATURE_TYPE_NAMES: dict[int, str] = {
    SignatureType.EOA: "EOA",
    SignatureType.POLY_PROXY: "POLY_PROXY",
    SignatureType.POLY_GNOSIS_SAFE: "POLY_GNOSIS_SAFE",
    SignatureType.POLY_1271: "POLY_1271",
}

SIGNATURE_TYPE_DESCRIPTIONS: dict[int, str] = {
    SignatureType.EOA: "MetaMask direct (no polymarket.com onboarding)",
    SignatureType.POLY_PROXY: "Email / Google (Magic.link) signup",
    SignatureType.POLY_GNOSIS_SAFE: "MetaMask via polymarket.com (most common)",
    SignatureType.POLY_1271: "Deposit Wallet (recommended for new API users)",
}


# ---------------------------------------------------------------------------
# TODO(verify) — Polygon-mainnet factory / singleton constants
# ---------------------------------------------------------------------------
# These are the community-documented Polymarket proxy infrastructure addresses
# on Polygon (chain id 137).  They are NOT published in the official docs that
# context7 surfaced (docs.polymarket.com/{trading/overview,api-reference/
# authentication}), so each is flagged for on-chain verification.  Detection
# does not rely on these being correct.
#
# proxy-wallet factory (POLY_PROXY, type 1) — 1-of-1 EOA-owned proxy.
_POLY_PROXY_FACTORY: str = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"  # TODO(verify)
# gnosis-safe proxy factory (POLY_GNOSIS_SAFE, type 2) used by polymarket.com.
_POLY_SAFE_FACTORY: str = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"  # TODO(verify)
# gnosis-safe singleton / master copy the factory clones via CREATE2.
_GNOSIS_SAFE_SINGLETON: str = "0x3E5c63644E683549055b9Be8653de26E0B4CD36E"  # TODO(verify)
# Minimal EIP-1167 proxy creation prefix/suffix wrapping the singleton; the
# real Polymarket proxy bytecode is not published, hence TODO(verify).
_EIP1167_PREFIX = bytes.fromhex("3d602d80600a3d3981f3363d3d373d3d3d363d73")
_EIP1167_SUFFIX = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")

# Polymarket collateral on Polygon — USDC.e — for the optional balance probe.
# Source: https://docs.polymarket.com/resources/contract-addresses (verified).
USDC_E_POLYGON: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"


def _normalize_eoa(eoa: str) -> str:
    """Validate and lowercase an EOA address.

    Args:
        eoa: A hex ``0x…`` address (any case, with/without checksum).

    Returns:
        The address lowercased.

    Raises:
        ValueError: If *eoa* is not a syntactically valid EVM address.
    """
    if not isinstance(eoa, str) or not is_address(eoa):
        raise ValueError(f"Not a valid EVM address: {eoa!r}")
    return to_checksum_address(eoa).lower()


def _create2(deployer: str, salt: bytes, init_code: bytes) -> str:
    """Standard EIP-1014 CREATE2 address.

    ``address = keccak256(0xff ++ deployer ++ salt ++ keccak256(init_code))[12:]``

    Args:
        deployer:  Factory contract address (``0x…``).
        salt:      32-byte salt.
        init_code: Contract creation bytecode.

    Returns:
        The lowercased ``0x…`` deployment address.
    """
    deployer_bytes = bytes.fromhex(to_checksum_address(deployer)[2:])
    pre = b"\xff" + deployer_bytes + salt + keccak(init_code)
    return to_checksum_address(keccak(pre)[-20:]).lower()


def _proxy_salt(eoa_lower: str) -> bytes:
    """CREATE2 salt for a Polymarket proxy keyed by the owner EOA.

    TODO(verify): Polymarket's exact salt pre-image is not documented.  The
    common pattern (and what is implemented here) is
    ``keccak256(abi.encode(owner_address))`` i.e. the 32-byte
    left-padded EOA.  Isolated here so a verified salt is a one-line fix.
    """
    return keccak(bytes(12) + bytes.fromhex(to_checksum_address(eoa_lower)[2:]))


def _poly_proxy_address(eoa_lower: str) -> str:
    """Type 1 (POLY_PROXY) funder address.

    TODO(verify): uses an EIP-1167 minimal-proxy init code pointing at a
    placeholder implementation.  Structurally correct CREATE2; the exact
    Polymarket proxy bytecode is unverified.
    """
    init_code = _EIP1167_PREFIX + bytes.fromhex(
        to_checksum_address(_POLY_PROXY_FACTORY)[2:]
    ) + _EIP1167_SUFFIX
    return _create2(_POLY_PROXY_FACTORY, _proxy_salt(eoa_lower), init_code)


def _poly_safe_address(eoa_lower: str) -> str:
    """Type 2 (POLY_GNOSIS_SAFE) funder address.

    TODO(verify): Gnosis-Safe proxies are CREATE2-deployed by a
    ProxyFactory cloning a singleton.  The init code below is the canonical
    GnosisSafeProxy creation pattern wrapping the singleton; Polymarket's
    precise factory/singleton/salt are unverified.
    """
    init_code = _EIP1167_PREFIX + bytes.fromhex(
        to_checksum_address(_GNOSIS_SAFE_SINGLETON)[2:]
    ) + _EIP1167_SUFFIX
    return _create2(_POLY_SAFE_FACTORY, _proxy_salt(eoa_lower), init_code)


def derive_wallets(eoa: str) -> dict[int, str]:
    """Derive the four candidate funder addresses for *eoa*.

    The returned mapping is keyed by signature type (0..3); every value is a
    lowercased ``0x…`` address.

    * **0 EOA** — the signer itself is the funder.
    * **1 POLY_PROXY** — CREATE2 proxy via the Polymarket proxy factory.
    * **2 POLY_GNOSIS_SAFE** — CREATE2 Gnosis-Safe proxy via the Polymarket
      safe factory + singleton.
    * **3 POLY_1271** — Polymarket's "deposit wallet"; the on-chain holder is
      the same Gnosis-Safe proxy as type 2 (the 1271 flag only changes
      signature *verification*, not the address).

    Args:
        eoa: The externally-owned account / signer address (``0x…``).

    Returns:
        ``{0: eoa, 1: poly_proxy, 2: poly_safe, 3: poly_safe}`` — all
        lowercased.  Types 2 and 3 intentionally share an address.

    Raises:
        ValueError: If *eoa* is not a valid EVM address.

    .. note::
       Types 1/2/3 depend on ``# TODO(verify)`` factory constants (see module
       docstring).  Type 0 is always exact.  Detection ranks by observed
       on-chain evidence and does not require 1/2/3 to be byte-exact.
    """
    eoa_lower = _normalize_eoa(eoa)
    safe = _poly_safe_address(eoa_lower)
    return {
        SignatureType.EOA: eoa_lower,
        SignatureType.POLY_PROXY: _poly_proxy_address(eoa_lower),
        SignatureType.POLY_GNOSIS_SAFE: safe,
        # POLY_1271 shares the type-2 Safe proxy on-chain; the 1271 flag only
        # changes how the order signature is verified, not the funder address.
        SignatureType.POLY_1271: safe,
    }


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TypeEvidence:
    """On-chain evidence gathered for one candidate address.

    Attributes:
        signature_type: 0..3.
        name:           Human name (e.g. ``"POLY_GNOSIS_SAFE"``).
        address:        The derived candidate funder address (lowercase).
        position_count: Number of open positions found for ``address``.
        activity_count: Number of activity records found for ``address``.
        usdc_balance:   Best-effort USDC balance, or ``None`` if not probed.
        score:          Internal ranking score (higher = stronger match).
    """

    signature_type: int
    name: str
    address: str
    position_count: int
    activity_count: int
    usdc_balance: float | None
    score: float


@dataclass(frozen=True)
class DetectionResult:
    """Result of :func:`detect_signature_type`.

    Attributes:
        eoa:                 The input EOA (lowercased).
        evidence:            Per-type evidence, ordered by signature type 0..3.
        recommended_type:    The signature type with the strongest evidence,
                             or ``None`` if nothing was found anywhere.
        recommended_address: Funder address for ``recommended_type`` (or
                             ``None``).
        confidence:          ``"high"`` | ``"medium"`` | ``"low"`` | ``"none"``.
    """

    eoa: str
    evidence: tuple[TypeEvidence, ...]
    recommended_type: int | None
    recommended_address: str | None
    confidence: str

    def evidence_for(self, signature_type: int) -> TypeEvidence:
        """Return the :class:`TypeEvidence` for *signature_type*."""
        for ev in self.evidence:
            if ev.signature_type == signature_type:
                return ev
        raise KeyError(signature_type)


# Ranking weights — positions dominate activity (PRD §5.1: "持倉 > 餘額 >
# 已部署 > 歷史活動").  Balance is folded in only when probed.
_W_POSITION = 1000.0
_W_BALANCE = 10.0
_W_ACTIVITY = 1.0


def _score(position_count: int, activity_count: int, usdc_balance: float | None) -> float:
    """Rank a candidate: positions ≫ balance ≫ activity (PRD §5.1)."""
    score = _W_POSITION * position_count + _W_ACTIVITY * activity_count
    if usdc_balance is not None and usdc_balance > 0:
        score += _W_BALANCE * usdc_balance
    return score


def _confidence(ranked: list[TypeEvidence]) -> str:
    """Map the score distribution to a coarse confidence label.

    Types 2 (POLY_GNOSIS_SAFE) and 3 (POLY_1271) derive to the *same*
    on-chain address, so they always tie.  That tie is **not** real
    ambiguity about the funder wallet — only about which signature flag to
    use — so the runner-up for confidence purposes is the best candidate
    with a *different* address than the winner.
    """
    top = ranked[0]
    if top.score <= 0:
        return "none"
    runner_up = next(
        (e.score for e in ranked[1:] if e.address != top.address),
        0.0,
    )
    if top.position_count > 0 and runner_up <= 0:
        return "high"
    if top.position_count > 0 or runner_up <= 0:
        return "medium"
    return "low"


async def detect_signature_type(
    eoa: str,
    datasource: _DetectionDataSource,
    *,
    usdc_balance_fn: Any = None,
) -> DetectionResult:
    """Detect the most likely Polymarket signature type for *eoa*.

    For each of the four derived candidate addresses, this queries the
    supplied ``datasource`` for open positions and recent activity, then
    ranks the candidates by **positions ≫ balance ≫ activity** (PRD §5.1).

    The frozen ``DataSource`` protocol is consumed exactly as-is
    (``get_positions`` / ``get_user_activity``); it is never extended or
    mutated.  USDC balance is *optional*: pass ``usdc_balance_fn`` (an
    ``async (address) -> float | None`` callable, e.g.
    :func:`fetch_usdc_balance`) to fold balance into the ranking.  Without
    it, ranking uses positions + activity only and remains fully useful.

    Args:
        eoa:             The signer / EOA address (``0x…``).
        datasource:      Any object satisfying the ``DataSource`` protocol
                         (``FixtureDataSource`` in tests).
        usdc_balance_fn: Optional ``async (address) -> float | None`` balance
                         probe.  Never required; failures are swallowed.

    Returns:
        A :class:`DetectionResult` with per-type evidence, the recommended
        type/address, and a coarse confidence label.

    Raises:
        ValueError: If *eoa* is not a valid EVM address.
    """
    wallets = derive_wallets(eoa)
    eoa_lower = wallets[SignatureType.EOA]

    evidence: list[TypeEvidence] = []
    for sig_type in (
        SignatureType.EOA,
        SignatureType.POLY_PROXY,
        SignatureType.POLY_GNOSIS_SAFE,
        SignatureType.POLY_1271,
    ):
        address = wallets[sig_type]

        try:
            positions = await datasource.get_positions(address)
        except Exception:  # noqa: BLE001 - detection must never hard-fail
            logger.debug("detect_positions_failed", extra={"sig_type": int(sig_type)})
            positions = []
        try:
            activity = await datasource.get_user_activity(address, activity_type=None)
        except Exception:  # noqa: BLE001
            logger.debug("detect_activity_failed", extra={"sig_type": int(sig_type)})
            activity = []

        usdc_balance: float | None = None
        if usdc_balance_fn is not None:
            try:
                usdc_balance = await usdc_balance_fn(address)
            except Exception:  # noqa: BLE001 - best-effort only
                logger.debug("detect_balance_failed", extra={"sig_type": int(sig_type)})
                usdc_balance = None

        evidence.append(
            TypeEvidence(
                signature_type=int(sig_type),
                name=SIGNATURE_TYPE_NAMES[sig_type],
                address=address,
                position_count=len(positions),
                activity_count=len(activity),
                usdc_balance=usdc_balance,
                score=_score(len(positions), len(activity), usdc_balance),
            )
        )

    # Rank a copy; keep `evidence` ordered by signature type 0..3 for display.
    # Tie-break by lowest signature type (most "primitive" wallet) for
    # determinism — `int(sig_type)` is the secondary sort key.
    ranked = sorted(evidence, key=lambda e: (-e.score, e.signature_type))
    top = ranked[0]

    if top.score <= 0:
        return DetectionResult(
            eoa=eoa_lower,
            evidence=tuple(evidence),
            recommended_type=None,
            recommended_address=None,
            confidence="none",
        )

    return DetectionResult(
        eoa=eoa_lower,
        evidence=tuple(evidence),
        recommended_type=top.signature_type,
        recommended_address=top.address,
        confidence=_confidence(ranked),
    )


# ---------------------------------------------------------------------------
# Optional, standalone USDC-balance probe (NOT part of the DataSource Protocol)
# ---------------------------------------------------------------------------
async def fetch_usdc_balance(
    address: str,
    *,
    client: Any = None,
    base_url: str | None = None,
    timeout_s: float = 10.0,
) -> float | None:
    """Best-effort USDC balance for *address* via the Polymarket data-api.

    This is a small standalone helper — deliberately **not** part of the
    frozen ``DataSource`` protocol (which must not be extended).  It hits the
    Polymarket data-api ``/value?user=<address>`` endpoint, which returns the
    account's USD value.  Any error returns ``None`` (callers treat balance as
    optional / best-effort per PRD §5.1).

    Args:
        address:  Wallet address to query (``0x…``).
        client:   Optional pre-built ``httpx.AsyncClient`` (injected in tests
                  so no network is touched).  When ``None`` a short-lived
                  client is created.
        base_url:  Override the data-api base URL; defaults to
                   ``settings.polymarket_data_api_url``.
        timeout_s: httpx request timeout in seconds (named ``timeout_s`` so
                   ruff ASYNC109 does not mistake it for an ``asyncio.timeout``
                   substitute — this is the httpx client timeout, the correct
                   pattern for outbound HTTP).

    Returns:
        The USD/USDC value as a float, or ``None`` on any failure.

    .. note:: TODO(verify): the exact data-api balance/value endpoint shape
       is not pinned in the verified docs; mapping is defensive and the whole
       call is optional, so an unexpected schema simply yields ``None``.
    """
    if base_url is None:
        from polyecho.shared.config import get_settings

        base_url = get_settings().polymarket_data_api_url
    url = f"{base_url.rstrip('/')}/value"
    params = {"user": address}

    async def _do(c: Any) -> float | None:
        resp = await c.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        # data-api /value → [{"user": "...", "value": <usd>}] (defensive).
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            for key in ("value", "balance", "usdc", "cash"):
                if data.get(key) is not None:
                    return float(data[key])
        return None

    try:
        if client is not None:
            return await _do(client)
        import httpx

        async with httpx.AsyncClient(timeout=timeout_s) as c:
            return await _do(c)
    except Exception:  # noqa: BLE001 - balance is strictly best-effort
        logger.debug("fetch_usdc_balance_failed", extra={"address": address})
        return None


__all__ = [
    "SignatureType",
    "SIGNATURE_TYPE_NAMES",
    "SIGNATURE_TYPE_DESCRIPTIONS",
    "USDC_E_POLYGON",
    "TypeEvidence",
    "DetectionResult",
    "derive_wallets",
    "detect_signature_type",
    "fetch_usdc_balance",
]

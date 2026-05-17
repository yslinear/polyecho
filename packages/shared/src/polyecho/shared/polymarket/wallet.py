"""
polyecho.shared.polymarket.wallet
=================================
Per-account wallet-mode resolver (PolyHermes brief §9 — Phase-4 binding).

Order signing is **mode-agnostic** (brief §9): wallet mode only sets the
order's ``signatureType`` int and the maker/``funder`` address.  py-clob-
client-v2 owns EIP-712 hash/domain/exchange/rounding/salt — we do NOT
reimplement any of it (brief §8).  Phase-4 needs only this resolver:

    wallet_mode → WalletBinding(signature_type, funder)

per the brief §9 table:

    | Mode      | sig_type | funder (maker)                                |
    |-----------|----------|-----------------------------------------------|
    | EOA       | 0        | signer EOA itself                             |
    | MAGIC     | 1        | Magic proxy = CREATE2(factory, keccak(eoa),   |
    |           |          |   initCodeHash) — brief §9 constants          |
    | SAFE      | 2        | Safe proxy via RPC computeProxyAddress(eoa)   |
    | POLY_1271 | 3        | explicit operator-supplied funder (no ref)    |

Standing rule (brief §6/§9): **prefer an explicit operator-confirmed
funder; treat derivation as a convenience to verify, never to trust
blindly**, especially type-3.  Fail-closed on unknown mode (PolyHermes
silently defaults null→SAFE — we must NOT).

No network I/O on import.  The SAFE path needs a Polygon RPC call
(``computeProxyAddress``); that call is injected so this module stays
import-side-effect-free and unit-testable without a node.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from eth_utils.address import is_address, to_checksum_address
from eth_utils.crypto import keccak

from polyecho.shared.accounts import SignatureType
from polyecho.shared.constants import (
    MAGIC_PROXY_FACTORY,
    MAGIC_PROXY_INIT_CODE_HASH,
)
from polyecho.shared.copytrading import WalletBinding

__all__ = [
    "WalletBinding",
    "WalletMode",
    "AccountLike",
    "SafeProxyResolver",
    "resolve_wallet",
    "magic_proxy_address",
]


# ---------------------------------------------------------------------------
# wallet_mode string ↔ signature_type int (brief §9 table)
# ---------------------------------------------------------------------------
class WalletMode:
    """Canonical ``wallet_mode`` string values (mirror ``accounts.wallet_mode``)."""

    EOA = "eoa"
    MAGIC = "magic"
    SAFE = "safe"
    POLY_1271 = "poly_1271"


_MODE_BY_SIGTYPE: dict[int, str] = {
    SignatureType.EOA: WalletMode.EOA,
    SignatureType.POLY_PROXY: WalletMode.MAGIC,
    SignatureType.POLY_GNOSIS_SAFE: WalletMode.SAFE,
    SignatureType.POLY_1271: WalletMode.POLY_1271,
}
_SIGTYPE_BY_MODE: dict[str, int] = {v: k for k, v in _MODE_BY_SIGTYPE.items()}


@runtime_checkable
class AccountLike(Protocol):
    """The subset of ``accounts`` columns the resolver reads.

    Interface-segregation so tests can pass a tiny fake and the real
    :class:`polyecho.shared.models.Account` is structurally compatible.
    """

    eoa_address: str
    funder_address: str | None
    signature_type: int | None
    wallet_mode: str | None


#: ``async (eoa) -> 0x… safe-proxy address`` — injected RPC call to the
#: Safe factory's ``computeProxyAddress``.  Brief §9 (type-2 path).
SafeProxyResolver = Callable[[str], Awaitable[str]]


def _norm(addr: str) -> str:
    if not isinstance(addr, str) or not is_address(addr):
        raise ValueError(f"Not a valid EVM address: {addr!r}")
    return to_checksum_address(addr).lower()


def magic_proxy_address(eoa: str) -> str:
    """Type-1 (MAGIC) funder via CREATE2 (brief §9).

    ``addr = keccak256(0xff ++ factory ++ keccak256(eoaAddr) ++
    initCodeHash)[12:]`` using the brief §9 Magic factory + init-code-hash.

    Args:
        eoa: The signer EOA (``0x…``).

    Returns:
        Lowercased ``0x…`` Magic proxy address.

    .. note:: Brief §9: these CREATE2 constants partially resolve the
       earlier "factory undocumented" caveat, BUT the standing rule holds —
       this is a convenience to *verify* an operator-supplied funder, not
       to trust blindly.
    """
    eoa_l = _norm(eoa)
    eoa_bytes = bytes.fromhex(to_checksum_address(eoa_l)[2:])
    factory_bytes = bytes.fromhex(to_checksum_address(MAGIC_PROXY_FACTORY)[2:])
    init_hash = bytes.fromhex(MAGIC_PROXY_INIT_CODE_HASH[2:])
    pre = b"\xff" + factory_bytes + keccak(eoa_bytes) + init_hash
    return to_checksum_address(keccak(pre)[-20:]).lower()


def _resolved_mode(account: AccountLike) -> str:
    """Derive the canonical wallet_mode, fail-closed on unknown.

    Prefers an explicit ``account.wallet_mode``; falls back to mapping
    ``signature_type``.  Brief §9: unknown ⇒ raise (do NOT default to SAFE).
    """
    raw = (account.wallet_mode or "").strip().lower()
    if raw:
        if raw not in _SIGTYPE_BY_MODE:
            raise ValueError(f"Unknown wallet_mode: {account.wallet_mode!r} (fail-closed)")
        return raw
    sig = account.signature_type
    if sig is None or int(sig) not in _MODE_BY_SIGTYPE:
        raise ValueError(
            f"Cannot resolve wallet mode: wallet_mode unset and "
            f"signature_type={account.signature_type!r} (fail-closed)"
        )
    return _MODE_BY_SIGTYPE[int(sig)]


async def resolve_wallet(
    account: AccountLike,
    *,
    safe_proxy_resolver: SafeProxyResolver | None = None,
) -> WalletBinding:
    """Resolve ``(signature_type, funder)`` for *account* (brief §9).

    Implements the brief §9 per-wallet-mode table:

    * **EOA (0)** — funder = the signer EOA itself (always exact).
    * **MAGIC (1)** — funder = CREATE2 Magic proxy
      (:func:`magic_proxy_address`).  An explicit ``funder_address`` on the
      account, if present, is **preferred** (operator-confirmed) and the
      derived value is only used when none is supplied; a mismatch is
      surfaced via :attr:`WalletBinding.derived`.
    * **SAFE (2)** — funder via the injected ``safe_proxy_resolver`` RPC
      ``computeProxyAddress(eoa)``; if no resolver is given, an explicit
      ``funder_address`` is required (fail-closed).
    * **POLY_1271 (3)** — funder = the explicit operator-supplied
      ``funder_address`` (no PolyHermes reference; our differentiator).
      **Required** — fail-closed if absent.

    Args:
        account:            Anything satisfying :class:`AccountLike`.
        safe_proxy_resolver: Optional ``async (eoa) -> 0x…`` RPC call used
                            only for the SAFE path.

    Returns:
        A :class:`WalletBinding`.  ``derived`` is ``True`` only when the
        funder came from address derivation rather than an operator value.

    Raises:
        ValueError: Invalid EOA, unknown wallet mode, or a required
                    explicit funder is missing (fail-closed).
    """
    eoa = _norm(account.eoa_address)
    mode = _resolved_mode(account)
    sig_type = _SIGTYPE_BY_MODE[mode]
    explicit = account.funder_address
    explicit_l = _norm(explicit) if explicit else None

    if mode == WalletMode.EOA:
        # Type 0 is always exact: the signer is the funder.
        return WalletBinding(
            signature_type=sig_type, funder=eoa, wallet_mode=mode, derived=False
        )

    if mode == WalletMode.MAGIC:
        if explicit_l is not None:
            # Operator-confirmed funder preferred (brief §6/§9).
            return WalletBinding(
                signature_type=sig_type,
                funder=explicit_l,
                wallet_mode=mode,
                derived=False,
            )
        return WalletBinding(
            signature_type=sig_type,
            funder=magic_proxy_address(eoa),
            wallet_mode=mode,
            derived=True,
        )

    if mode == WalletMode.SAFE:
        if explicit_l is not None:
            return WalletBinding(
                signature_type=sig_type,
                funder=explicit_l,
                wallet_mode=mode,
                derived=False,
            )
        if safe_proxy_resolver is None:
            raise ValueError(
                "SAFE wallet: no explicit funder_address and no "
                "safe_proxy_resolver supplied (fail-closed; brief §9)"
            )
        funder = _norm(await safe_proxy_resolver(eoa))
        return WalletBinding(
            signature_type=sig_type, funder=funder, wallet_mode=mode, derived=True
        )

    # POLY_1271 (3): explicit operator-supplied funder is MANDATORY — no
    # PolyHermes reference exists; we never derive it (brief §6/§9).
    if explicit_l is None:
        raise ValueError(
            "POLY_1271 wallet: an explicit operator-supplied "
            "funder_address is REQUIRED (no derivation; fail-closed; "
            "brief §9 — our differentiator)"
        )
    return WalletBinding(
        signature_type=sig_type, funder=explicit_l, wallet_mode=mode, derived=False
    )

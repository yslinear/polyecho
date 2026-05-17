"""
polyecho.shared.polymarket.redeem
=================================
Redeem / claim-winnings client **interface** (brief §9 — Phase-4 binding).

Phase-4 Wave-1 freezes the *contract*; Wave-2 implements the live path.
Trigger / call / execution semantics (brief §9):

* **Trigger** — poll the Polymarket data-api ``redeemable`` flag (do NOT
  compute UMA/on-chain resolution; adopt the pragmatic API-trust).  Gate
  on ``auto_redeem`` + relayer/key configured + quota-cooldown.
* **Call** — ``redeemPositions(collateral, PARENT_COLLECTION_ID,
  conditionId, [2**outcomeIndex…])`` on the CTF contract.  Collateral is
  **pUSD for normal markets vs WrappedCollateral for NegRisk** (the brief's
  highest-risk detail).  NegRisk redeem pays WCOL → must then
  ``unwrap(to, amount)``.
* **Execution per mode** — MAGIC → Builder-Relayer PROXY; SAFE →
  Builder-Relayer SafeTx / manual ``execTransaction``; EOA → direct
  ``web3.py``.  Use the official ``py-builder-relayer-client`` (PRD §11.2)
  for gasless; ``web3.py`` for EOA/manual.  Do NOT hand-roll the
  relayer/HMAC (brief §9).

This module is interface-only: no network, no web3, no relayer import.
The concrete client lands in Wave-2 (core).  Every method is wired with
the correct signature + the live-lock guard so the chokepoint is binding.

Dependency note (Phase-4 W1)
----------------------------
* ``web3`` (EOA direct tx + Safe ``computeProxyAddress`` RPC + CTF
  ``redeemPositions`` encoding) is **already declared** in
  ``packages/core/pyproject.toml`` (``web3>=7.0``) — core owns redeem
  execution; shared stays web3-free (the SAFE proxy RPC is *injected* into
  :func:`polyecho.shared.polymarket.wallet.resolve_wallet`).
* ``py-builder-relayer-client`` (PRD §11.2, official gasless relayer for
  MAGIC/SAFE redeem): **# TODO(verify)** — PyPI availability could not be
  confirmed from this sandboxed environment (curl/WebFetch blocked,
  2026-05-18).  Per the brief this MUST NOT block: the gasless path is
  expressed here only as the abstract :class:`RedeemClient` Protocol /
  :class:`BaseRedeemClient` (no import of the relayer package), so Wave-2
  adds the concrete dependency to ``packages/core/pyproject.toml`` once
  the exact PyPI name/version is verified, with zero change to this
  frozen contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from polyecho.shared.constants import (
    CTF_ADDRESS,
    PARENT_COLLECTION_ID,
    PUSD_ADDRESS,
    WRAPPED_COLLATERAL_ADDRESS,
)
from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemResult,
    WalletBinding,
    assert_live_allowed,
)

__all__ = ["collateral_for", "index_set_for", "RedeemClient", "BaseRedeemClient"]


def collateral_for(*, is_neg_risk: bool) -> str:
    """Return the correct collateral address for a redeem (brief §9).

    NegRisk markets redeem against **WrappedCollateral** (then ``unwrap``);
    normal markets redeem against **pUSD**.  This is the brief's
    "highest-risk detail to get right".
    """
    return WRAPPED_COLLATERAL_ADDRESS if is_neg_risk else PUSD_ADDRESS


def index_set_for(outcome_index: int) -> int:
    """``2**outcome_index`` bitmask for ``redeemPositions`` (brief §9)."""
    if outcome_index < 0:
        raise ValueError(f"outcome_index must be >= 0, got {outcome_index}")
    return 1 << outcome_index


@runtime_checkable
class RedeemClient(Protocol):
    """The redeem client contract Wave-2 implements (brief §9).

    Idempotency / durability is the ``redeem_jobs`` table's job (brief §9 —
    survives restart, no double-redeem); this client only performs the
    on-chain / relayer call for one resolved job.
    """

    async def redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult: ...

    async def unwrap_collateral(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult: ...


class BaseRedeemClient:
    """Abstract base wiring the live-lock chokepoint (brief §9).

    Subclasses (Wave-2) override :meth:`_do_redeem` / :meth:`_do_unwrap`
    with the real Builder-Relayer / web3 calls.  The public methods call
    :func:`assert_live_allowed` FIRST so a misconfigured deployment fails
    closed before any irreversible call.
    """

    #: Brief §9 constants surfaced for subclasses (no inline hardcoding).
    ctf_address: str = CTF_ADDRESS
    parent_collection_id: str = PARENT_COLLECTION_ID

    async def redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        """Redeem one resolved position (live-lock guarded; brief §9)."""
        assert_live_allowed("redeem")
        return await self._do_redeem(spec, binding=binding)

    async def unwrap_collateral(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        """Unwrap WCOL → pUSD after a NegRisk redeem (live-lock guarded)."""
        assert_live_allowed("unwrap_collateral")
        return await self._do_unwrap(binding=binding, amount=amount)

    async def _do_redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        raise NotImplementedError(
            "Phase-4 W2: redeemPositions via py-builder-relayer-client "
            "(MAGIC/SAFE gasless) or web3.py (EOA) — see brief §9"
        )

    async def _do_unwrap(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        raise NotImplementedError(
            "Phase-4 W2: WrappedCollateral.unwrap(to, amount) after a "
            "NegRisk redeem — see brief §9"
        )

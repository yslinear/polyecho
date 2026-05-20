"""
polyecho.shared.polymarket.redeem
=================================
Redeem / claim-winnings client (brief §9 — Phase-4 binding; Wave C-redeem
concrete implementation).

Trigger / call / execution semantics (brief §9 / polyhermes-analysis §9):

* **Trigger** — poll the Polymarket data-api ``redeemable`` flag (do NOT
  compute UMA/on-chain resolution; adopt the pragmatic API-trust).  Gate
  on ``auto_redeem`` + relayer/key configured + quota-cooldown.  Owned by
  :mod:`polyecho.core.redeem` (this module only performs one resolved call).
* **Call** — ``redeemPositions(collateral, PARENT_COLLECTION_ID,
  conditionId, [2**outcomeIndex…])`` on the CTF contract.  Collateral is
  **pUSD for normal markets vs WrappedCollateral for NegRisk** (the brief's
  highest-risk detail).  NegRisk redeem pays WCOL → must then
  ``unwrap(to, amount)`` on WCOL (PolyHermes polls ~20s for the balance).
* **Execution per mode** —
  * **EOA (sig 0)** — CONCRETE: ``web3.py`` v7 build → ``eth_account``
    sign → send raw → wait receipt.  This is the only mode made fully
    real (verified against web3.py v7 docs + Polymarket CTF examples).
  * **MAGIC (sig 1)** — Builder-Relayer PROXY (eth_sign / EIP-191):
    CONCRETE via :mod:`polyecho.shared.polymarket.relayer_extended`
    (hand-rolled per the brief; verified against
    ``Polymarket/builder-relayer-client`` TS reference, 2026-05-20).
    The dispatch wraps ``CTF.redeemPositions`` inside
    ``ProxyFactory.proxy([(to, type=1 Call, data, value=0)])``, computes
    the ``rlx:`` struct hash, ``eth_sign``-s it, and POSTs
    ``type:"PROXY"`` to ``/submit``.  Without injected
    ``extended_relayer_creds`` the path still fails closed.
  * **SAFE (sig 2)** — Builder-Relayer SafeTx (EIP-712): CONCRETE via
    :mod:`polyecho.shared.polymarket.relayer.PolymarketRelayerClient`
    (Phase-4 P3 wave); inject a configured relayer into
    :class:`Web3RedeemClient` and ``_relay_redeem`` / ``_relay_unwrap``
    delegate to it.  Without an injected relayer (or without builder
    API credentials), sig 2 still fails closed.
  * **POLY_1271 (sig 3)** — Deposit-Wallet EIP-712 ``Batch``: CONCRETE
    via :mod:`polyecho.shared.polymarket.relayer_extended` (verified
    against ``builder-relayer-client/src/builder/deposit-wallet.ts``,
    2026-05-20).  The dispatch deploys the deposit-wallet clone via
    ``type:"WALLET-CREATE"`` if needed, EIP-712 signs the ``Batch`` typed
    data (domain ``DepositWallet/1/137/<walletAddress>``), and POSTs
    ``type:"WALLET"`` with ``depositWalletParams``.  Without injected
    ``extended_relayer_creds`` the path still fails closed.

The live-lock chokepoint is :func:`assert_live_allowed` called FIRST in
the public :meth:`BaseRedeemClient.redeem` / :meth:`unwrap_collateral`, so
a misconfigured deployment fails closed before any irreversible call.

Dependency note (Wave C-redeem)
-------------------------------
* ``web3`` (EOA direct tx + ABI encoding) is declared in
  ``packages/core/pyproject.toml`` (``web3>=7.0``).  This shared module
  stays import-side-effect-free and **web3-free at import time**: the EOA
  path takes an injected :class:`Web3Backend` (a tiny structural Protocol)
  so the concrete client is unit-testable with a fake and core owns the
  real ``AsyncWeb3`` wiring.
* ``py-builder-relayer-client`` (PRD §11.2, official gasless relayer for
  SAFE redeem): **verified** at v0.0.1 on PyPI (homepage
  ``github.com/Polymarket/py-builder-relayer-client``, source inspected
  2026-05-20).  It is declared as an **optional** runtime dependency
  (``packages/shared`` ``[relayer]`` extra) and imported lazily by
  :mod:`polyecho.shared.polymarket.relayer` so this module stays
  import-side-effect-free.  Hosts without the SDK see a clear
  :class:`RelayerNotAvailableError` on first use; sig 2 then fails
  closed at the live-lock chokepoint instead of submitting a guessed
  payload.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from polyecho.shared.constants import (
    CTF_ADDRESS,
    MAX_UINT256,
    PARENT_COLLECTION_ID,
    POLYGON_CHAIN_ID,
    PUSD_ADDRESS,
    WRAPPED_COLLATERAL_ADDRESS,
)
from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    WalletBinding,
    assert_live_allowed,
)

__all__ = [
    "collateral_for",
    "index_set_for",
    "CTF_REDEEM_ABI",
    "WRAPPED_COLLATERAL_ABI",
    "ERC20_APPROVE_ABI",
    "RelayerNotConfiguredError",
    "Web3Backend",
    "RedeemClient",
    "BaseRedeemClient",
    "Web3RedeemClient",
]

# ---------------------------------------------------------------------------
# Minimal ABIs (brief §9 — redeemPositions / unwrap / approve).  Verified
# against the Polymarket CTF examples (context7
# /polymarket/conditional-token-examples-py): the CTF on Polygon at
# 0x4D97…6045 exposes split/merge/redeem with the SAME 4-arg conditional-
# tokens shape; ``redeemPositions`` takes (collateral, parentCollectionId,
# conditionId, indexSets) — Polymarket passes ``HASH_ZERO`` for
# parentCollectionId on all top-level markets.
# ---------------------------------------------------------------------------
#: ``CTF.redeemPositions(collateral, parentCollectionId, conditionId,
#: indexSets)`` — Gnosis Conditional Tokens redeem (brief §9).
CTF_REDEEM_ABI: tuple[dict[str, Any], ...] = (
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
)

#: ``WrappedCollateral.unwrap(to, amount)`` — NegRisk post-redeem unwrap
#: of WCOL back to pUSD (brief §9 — "must then unwrap(to,amount)").
WRAPPED_COLLATERAL_ABI: tuple[dict[str, Any], ...] = (
    {
        "constant": False,
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "unwrap",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
)

#: ERC-20 ``approve(spender, amount)`` + ``allowance`` — one-time max
#: approve at account setup so redeem never needs a per-call approval
#: (brief §9 — ``approve(spender, 2**256-1)`` once).
ERC20_APPROVE_ABI: tuple[dict[str, Any], ...] = (
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
)


class RelayerNotConfiguredError(RuntimeError):
    """Raised when a gasless redeem is requested but **no** relayer
    credentials / clients are wired into :class:`Web3RedeemClient`.

    All four signature types are now supported in code:

    * sig 0 (EOA) — concrete on-chain path; never raises this.
    * sig 1 (MAGIC / POLY_PROXY) — concrete via
      :mod:`polyecho.shared.polymarket.relayer_extended`; raises only when
      no :class:`ExtendedRelayerCredentials` are supplied.
    * sig 2 (SAFE) — concrete via
      :mod:`polyecho.shared.polymarket.relayer`; raises only when no
      :class:`PolymarketRelayerClient` is injected.
    * sig 3 (POLY_1271 / Deposit-Wallet) — concrete via
      :mod:`polyecho.shared.polymarket.relayer_extended`; raises only when
      no :class:`ExtendedRelayerCredentials` are supplied.

    This is a *correctness* guard: it fails closed with a clear message
    rather than submitting a wrong transaction when the operator forgot
    to configure builder credentials.
    """


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


# ---------------------------------------------------------------------------
# Injected web3 seam (keeps this module web3-free at import time)
# ---------------------------------------------------------------------------
@runtime_checkable
class Web3Backend(Protocol):
    """The tiny async web3 surface the EOA path needs (brief §9).

    Structurally satisfied by a thin adapter around web3.py v7 ``AsyncWeb3``
    (core owns the concrete adapter / RPC URL); tests pass a fake.  Method
    shapes mirror web3.py v7 verbatim (context7 /ethereum/web3.py v7.12.0):

    * ``send_signed`` — build a contract-function tx dict, sign it with the
      account's private key via ``eth_account`` (sync, no I/O — the only
      place the decrypted key is used) and broadcast the raw bytes,
      returning the ``0x…`` tx hash.  Encapsulating build+sign+send keeps
      the decrypted key out of this module entirely (defence-in-depth).
    * ``wait_receipt`` — ``w3.eth.wait_for_transaction_receipt``; returns
      ``status`` (1 = success) and ``blockNumber``.
    * ``call`` — a read-only ``view`` contract call (e.g. WCOL
      ``balanceOf``) used to poll the post-NegRisk-redeem WCOL balance.
    """

    async def send_signed(
        self,
        *,
        contract_address: str,
        abi: tuple[dict[str, Any], ...],
        fn_name: str,
        args: tuple[Any, ...],
        signer_private_key: str,
        from_address: str,
        chain_id: int,
    ) -> str: ...

    async def wait_receipt(self, tx_hash: str) -> dict[str, Any]: ...

    async def call(
        self,
        *,
        contract_address: str,
        abi: tuple[dict[str, Any], ...],
        fn_name: str,
        args: tuple[Any, ...],
    ) -> Any: ...


#: ``async (account_funder_or_eoa) -> decrypted 0x… private key`` — injected
#: so the decrypted key never persists in this module and the EOA path is
#: testable with a fake.  Core wires this to the crypto-config decrypt path.
PrivateKeyResolver = Callable[[WalletBinding], Awaitable[str]]


@runtime_checkable
class RedeemClient(Protocol):
    """The redeem client contract core's worker drives (brief §9).

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

    The public methods call :func:`assert_live_allowed` FIRST so a
    misconfigured deployment fails closed before any irreversible call.
    :class:`Web3RedeemClient` overrides the ``_do_*`` hooks with the real
    EOA web3 path *and* the relayer seam for sig 2 SAFE (Phase-4 P3); on
    this base class the gasless seam stays abstract and fails closed
    with :class:`RelayerNotConfiguredError` so a misconfigured deployment
    can never silently fall through.
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
            "Wave C-redeem: use Web3RedeemClient for the concrete EOA path "
            "(brief §9)"
        )

    async def _do_unwrap(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        raise NotImplementedError(
            "Wave C-redeem: use Web3RedeemClient for the concrete EOA path "
            "(brief §9)"
        )

    # -- Gasless seam (MAGIC/SAFE/1271) — concrete via relayer.py --------
    # Subclasses (see :class:`Web3RedeemClient`) override this seam to
    # dispatch SAFE (sig 2) to PolymarketRelayerClient.  MAGIC (sig 1)
    # and POLY_1271 (sig 3) stay fail-closed everywhere — verified
    # 2026-05-20: py-builder-relayer-client v0.0.1 only exposes
    # SafeTransaction submit (TransactionType.SAFE), not PROXY/EIP-191
    # or 1271; we refuse to invent the wire format.
    async def _relay_redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        """Gasless redeem via the official Builder-Relayer (brief §9).

        Default (no relayer injected) ⇒ fail closed.  Concrete dispatch
        for sig 2 lives in :meth:`Web3RedeemClient._relay_redeem`.
        """
        raise RelayerNotConfiguredError(
            f"Gasless redeem for wallet_mode={binding.wallet_mode!r} "
            f"(signature_type={binding.signature_type}) requires a "
            f"configured Builder-Relayer client (sig 2 SAFE) — only "
            f"signature_type=0 (EOA) and signature_type=2 (SAFE, with a "
            f"PolymarketRelayerClient injected) are concrete."
        )

    async def _relay_unwrap(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        """Gasless WCOL unwrap via the Builder-Relayer (brief §9).

        Default (no relayer injected) ⇒ fail closed.  Concrete dispatch
        for sig 2 lives in :meth:`Web3RedeemClient._relay_unwrap`.
        """
        raise RelayerNotConfiguredError(
            f"Gasless unwrap for wallet_mode={binding.wallet_mode!r} "
            f"(signature_type={binding.signature_type}) requires a "
            f"configured Builder-Relayer client (sig 2 SAFE)."
        )


def _eoa_from_key(private_key: str) -> str:
    """Recover the EOA address that owns *private_key*.

    Used by sig 1 / sig 3 relayer paths where the wire format requires the
    signer EOA (NOT the proxy / deposit-wallet funder).  Imported lazily
    so this module stays eth_account-free at import time (eth_account is
    a transitive of web3.py already pinned in core/shared deps).
    """
    from eth_account import Account

    addr: str = Account.from_key(private_key).address
    return addr.lower()


# Wallet signature_type that has a concrete on-chain path (brief §9).
_SIG_EOA = 0
#: MAGIC / POLY_PROXY (sig 1) — concrete via relayer_extended.py
#: (hand-rolled per ``docs/reference/polymarket-sig1-sig3-redeem-wire.md``,
#: verified against TS ``Polymarket/builder-relayer-client``; 2026-05-20).
_SIG_MAGIC = 1
#: SAFE (sig 2) — only non-EOA sig with a concrete relayer SDK surface
#: (py-builder-relayer-client.RelayClient.execute consumes
#: ``list[SafeTransaction]``, TransactionType.SAFE).  Verified 2026-05-20.
_SIG_SAFE = 2
#: POLY_1271 / Deposit-Wallet (sig 3) — concrete via relayer_extended.py
#: (Solady ERC-1967 CREATE2 + EIP-712 ``Batch`` typed-data; verified
#: against TS ``builder-relayer-client/src/builder/deposit-wallet.ts``).
_SIG_POLY_1271 = 3


class Web3RedeemClient(BaseRedeemClient):
    """Concrete redeem client (brief §9 — Wave C-redeem + Phase-4 P3).

    * **EOA (signature_type 0)** — CONCRETE: builds the CTF
      ``redeemPositions`` (and WCOL ``unwrap`` for NegRisk) tx via the
      injected :class:`Web3Backend`, signs with the decrypted key from the
      injected :data:`PrivateKeyResolver`, sends raw and waits for the
      receipt.  Verified against web3.py v7 (``sign_transaction`` →
      ``raw_transaction`` → ``send_raw_transaction`` →
      ``wait_for_transaction_receipt``) + the Polymarket CTF examples.
    * **SAFE (signature_type 2)** — CONCRETE when a
      :class:`~polyecho.shared.polymarket.relayer.PolymarketRelayerClient`
      is injected: ``_relay_redeem`` / ``_relay_unwrap`` build the
      SafeTransaction calldata and call
      ``relayer.submit_safe_transaction()`` (which signs the SafeTx
      EIP-712 hash with the operator's signing key and POSTs to
      ``relayer-v2.polymarket.com/submit`` with builder-program HMAC
      headers).  Without an injected relayer, sig 2 fails closed
      (:class:`RelayerNotConfiguredError`) at the live-lock chokepoint.
    * **MAGIC (sig 1) / POLY_1271 (sig 3)** — CONCRETE via
      :mod:`polyecho.shared.polymarket.relayer_extended` (hand-rolled per
      ``docs/reference/polymarket-sig1-sig3-redeem-wire.md``; verified
      against the Polymarket TS ``builder-relayer-client`` reference,
      2026-05-20).  When ``extended_relayer_creds`` is injected the
      dispatch posts the appropriate ``type:"PROXY"`` / ``type:"WALLET"``
      submit body; without those credentials sig 1 / sig 3 still fail
      closed at the chokepoint (:class:`RelayerNotConfiguredError`).

    The collateral branch is the brief's highest-risk detail and is
    derived ONLY from :attr:`RedeemJobSpec.is_neg_risk` via
    :func:`collateral_for` (normal → pUSD, NegRisk → WrappedCollateral).
    NegRisk redeem pays WCOL → the worker (core) then calls
    :meth:`unwrap_collateral`; this client exposes
    :meth:`poll_wcol_balance` so the worker can poll ~20s for the WCOL
    balance before unwrapping (brief §9 — "PolyHermes polls 20s").

    Args:
        web3: A :class:`Web3Backend` (core wires the real ``AsyncWeb3``).
        private_key_resolver: ``async (binding) -> 0x… key``; called ONLY
            on the EOA live path (the lock + worker short-circuit keep it
            uncalled by default).
        chain_id: EVM chain id for the signed tx (default Polygon 137).
        relayer: Optional :class:`PolymarketRelayerClient` enabling the
            concrete SAFE (sig 2) path.  Without it, SAFE wallets follow
            the abstract seam and raise :class:`RelayerNotConfiguredError`.
    """

    def __init__(
        self,
        *,
        web3: Web3Backend,
        private_key_resolver: PrivateKeyResolver,
        chain_id: int = POLYGON_CHAIN_ID,
        relayer: Any | None = None,
        extended_relayer_creds: Any | None = None,
        extended_relayer_base_url: str | None = None,
    ) -> None:
        self._web3 = web3
        self._resolve_key = private_key_resolver
        self._chain_id = chain_id
        self._relayer = relayer
        # ``extended_relayer_creds`` — optional
        # :class:`relayer_extended.ExtendedRelayerCredentials` enabling the
        # concrete sig 1 (MAGIC) + sig 3 (POLY_1271) paths.  Without it,
        # sig 1 / sig 3 still fail-closed at the chokepoint (just like sig
        # 2 without the SAFE relayer).
        self._ext_creds = extended_relayer_creds
        self._ext_base_url = extended_relayer_base_url

    # -- Relayer concrete path ------------------------------------------
    # verified 2026-05-20: concrete via relayer_extended.py for sig 1 / 3
    # (per docs/reference/polymarket-sig1-sig3-redeem-wire.md) and via
    # relayer.py for sig 2 (P3 wave SDK delegation).
    async def _relay_redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        """Dispatch sig 1 / 2 / 3 to the correct relayer surface.

        Falls back to the base-class fail-closed seam only when the
        relevant credentials/relayer have not been injected.
        """
        if binding.signature_type == _SIG_SAFE and self._relayer is not None:
            from polyecho.shared.polymarket.relayer import relay_redeem

            return await relay_redeem(self._relayer, spec, binding=binding)

        if (
            binding.signature_type == _SIG_MAGIC
            and self._ext_creds is not None
        ):
            return await self._relay_redeem_sig1(spec, binding=binding)

        if (
            binding.signature_type == _SIG_POLY_1271
            and self._ext_creds is not None
        ):
            return await self._relay_redeem_sig3(spec, binding=binding)

        return await super()._relay_redeem(spec, binding=binding)

    async def _relay_unwrap(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        """Dispatch sig 1 / 2 / 3 WCOL unwrap to the correct relayer."""
        if binding.signature_type == _SIG_SAFE and self._relayer is not None:
            from polyecho.shared.polymarket.relayer import relay_unwrap

            return await relay_unwrap(
                self._relayer, binding=binding, amount=amount
            )

        if (
            binding.signature_type == _SIG_MAGIC
            and self._ext_creds is not None
        ):
            return await self._relay_unwrap_sig1(
                binding=binding, amount=amount
            )

        if (
            binding.signature_type == _SIG_POLY_1271
            and self._ext_creds is not None
        ):
            return await self._relay_unwrap_sig3(
                binding=binding, amount=amount
            )

        return await super()._relay_unwrap(binding=binding, amount=amount)

    # -- sig 1 (MAGIC) helpers ------------------------------------------
    async def _relay_redeem_sig1(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        from polyecho.shared.polymarket.relayer_extended import (
            ExtendedRelayerCredentials,
            RelayerSubmissionError,
            redeem_proxy,
        )

        assert isinstance(self._ext_creds, ExtendedRelayerCredentials)
        key = await self._resolve_key(binding)
        try:
            tx_hash = await redeem_proxy(
                eoa=_eoa_from_key(key),
                private_key=key,
                proxy_funder=binding.funder,
                condition_id=spec.condition_id,
                is_neg_risk=spec.is_neg_risk,
                index_sets=[spec.index_set],
                creds=self._ext_creds,
                base_url=self._ext_base_url,
            )
        except RelayerSubmissionError as exc:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                detail=f"sig 1 redeem via relayer_extended failed: {exc!r}",
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=(
                f"redeemPositions via Builder-Relayer (sig 1 PROXY; "
                f"collateral={'WCOL' if spec.is_neg_risk else 'pUSD'})"
            ),
        )

    async def _relay_unwrap_sig1(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        from polyecho.shared.polymarket.relayer_extended import (
            ExtendedRelayerCredentials,
            RelayerSubmissionError,
            relay_unwrap_proxy,
        )

        assert isinstance(self._ext_creds, ExtendedRelayerCredentials)
        if amount <= 0:
            return RedeemResult(
                status=RedeemJobStatus.CONFIRMED,
                detail="sig 1 unwrap skipped: WCOL balance is 0",
            )
        key = await self._resolve_key(binding)
        try:
            tx_hash = await relay_unwrap_proxy(
                eoa=_eoa_from_key(key),
                private_key=key,
                proxy_funder=binding.funder,
                amount=amount,
                creds=self._ext_creds,
                base_url=self._ext_base_url,
            )
        except RelayerSubmissionError as exc:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                detail=f"sig 1 unwrap via relayer_extended failed: {exc!r}",
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=f"WCOL.unwrap via Builder-Relayer (sig 1 PROXY, amount={amount})",
        )

    # -- sig 3 (POLY_1271 / Deposit-Wallet) helpers ---------------------
    async def _relay_redeem_sig3(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        from polyecho.shared.polymarket.relayer_extended import (
            ExtendedRelayerCredentials,
            RelayerSubmissionError,
            redeem_deposit_wallet,
        )

        assert isinstance(self._ext_creds, ExtendedRelayerCredentials)
        key = await self._resolve_key(binding)
        try:
            tx_hash = await redeem_deposit_wallet(
                eoa=_eoa_from_key(key),
                private_key=key,
                deposit_funder=binding.funder,
                condition_id=spec.condition_id,
                is_neg_risk=spec.is_neg_risk,
                index_sets=[spec.index_set],
                creds=self._ext_creds,
                base_url=self._ext_base_url,
            )
        except RelayerSubmissionError as exc:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                detail=f"sig 3 redeem via relayer_extended failed: {exc!r}",
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=(
                f"redeemPositions via Builder-Relayer (sig 3 Deposit-Wallet; "
                f"collateral={'WCOL' if spec.is_neg_risk else 'pUSD'})"
            ),
        )

    async def _relay_unwrap_sig3(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        from polyecho.shared.polymarket.relayer_extended import (
            ExtendedRelayerCredentials,
            RelayerSubmissionError,
            relay_unwrap_deposit_wallet,
        )

        assert isinstance(self._ext_creds, ExtendedRelayerCredentials)
        if amount <= 0:
            return RedeemResult(
                status=RedeemJobStatus.CONFIRMED,
                detail="sig 3 unwrap skipped: WCOL balance is 0",
            )
        key = await self._resolve_key(binding)
        try:
            tx_hash = await relay_unwrap_deposit_wallet(
                eoa=_eoa_from_key(key),
                private_key=key,
                deposit_funder=binding.funder,
                amount=amount,
                creds=self._ext_creds,
                base_url=self._ext_base_url,
            )
        except RelayerSubmissionError as exc:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                detail=f"sig 3 unwrap via relayer_extended failed: {exc!r}",
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=f"WCOL.unwrap via Builder-Relayer (sig 3 Deposit-Wallet, amount={amount})",
        )

    # -- EOA concrete path ----------------------------------------------
    async def _do_redeem(
        self, spec: RedeemJobSpec, *, binding: WalletBinding
    ) -> RedeemResult:
        if binding.signature_type != _SIG_EOA:
            # MAGIC(1)/SAFE(2)/POLY_1271(3): abstract gasless seam.
            return await self._relay_redeem(spec, binding=binding)

        collateral = collateral_for(is_neg_risk=spec.is_neg_risk)
        key = await self._resolve_key(binding)
        tx_hash = await self._web3.send_signed(
            contract_address=self.ctf_address,
            abi=CTF_REDEEM_ABI,
            fn_name="redeemPositions",
            args=(
                collateral,
                self.parent_collection_id,
                spec.condition_id,
                [spec.index_set],
            ),
            signer_private_key=key,
            from_address=binding.funder,
            chain_id=self._chain_id,
        )
        receipt = await self._web3.wait_receipt(tx_hash)
        if int(receipt.get("status", 0)) != 1:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                tx_hash=tx_hash,
                detail=(
                    f"redeemPositions reverted (status="
                    f"{receipt.get('status')!r}, collateral="
                    f"{'WCOL' if spec.is_neg_risk else 'pUSD'})"
                ),
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=(
                f"redeemPositions confirmed "
                f"(collateral={'WCOL' if spec.is_neg_risk else 'pUSD'}, "
                f"neg_risk={spec.is_neg_risk}); NegRisk ⇒ unwrap next"
                if spec.is_neg_risk
                else "redeemPositions confirmed (collateral=pUSD)"
            ),
        )

    async def _do_unwrap(
        self, *, binding: WalletBinding, amount: int
    ) -> RedeemResult:
        if binding.signature_type != _SIG_EOA:
            return await self._relay_unwrap(binding=binding, amount=amount)
        if amount <= 0:
            return RedeemResult(
                status=RedeemJobStatus.CONFIRMED,
                detail="unwrap skipped: WCOL balance is 0 (nothing to unwrap)",
            )
        key = await self._resolve_key(binding)
        tx_hash = await self._web3.send_signed(
            contract_address=WRAPPED_COLLATERAL_ADDRESS,
            abi=WRAPPED_COLLATERAL_ABI,
            fn_name="unwrap",
            args=(binding.funder, amount),
            signer_private_key=key,
            from_address=binding.funder,
            chain_id=self._chain_id,
        )
        receipt = await self._web3.wait_receipt(tx_hash)
        if int(receipt.get("status", 0)) != 1:
            return RedeemResult(
                status=RedeemJobStatus.FAILED,
                tx_hash=tx_hash,
                detail=f"WCOL.unwrap reverted (amount={amount})",
            )
        return RedeemResult(
            status=RedeemJobStatus.CONFIRMED,
            tx_hash=tx_hash,
            detail=f"WCOL.unwrap confirmed (amount={amount})",
        )

    async def poll_wcol_balance(self, *, binding: WalletBinding) -> int:
        """Read the funder's WrappedCollateral balance (brief §9).

        Used by the core worker after a NegRisk redeem so it can pass the
        actual WCOL amount to :meth:`unwrap_collateral` (PolyHermes polls
        ~20s; the poll loop / cooldown lives in the worker).
        """
        raw = await self._web3.call(
            contract_address=WRAPPED_COLLATERAL_ADDRESS,
            abi=WRAPPED_COLLATERAL_ABI,
            fn_name="balanceOf",
            args=(binding.funder,),
        )
        return int(raw)

    async def ensure_collateral_approval(
        self, *, binding: WalletBinding, collateral: str, spender: str
    ) -> str | None:
        """One-time idempotent ERC-20 ``approve(spender, 2**256-1)``.

        Brief §9: USDC/collateral ``approve`` once at account setup so
        redeem never needs a per-call approval.  Idempotent — checks the
        current ``allowance`` first and returns ``None`` (no tx) if already
        at/above the max.  Live-lock guarded by the caller (account-setup
        path) which MUST call :func:`assert_live_allowed` before this; the
        guard is asserted here too (defence-in-depth) so a stray call can
        never approve when the lock is off.

        EOA only — gasless approval is part of the relayer seam (TODO).
        """
        assert_live_allowed("approve_collateral")
        if binding.signature_type != _SIG_EOA:
            raise RelayerNotConfiguredError(
                "Collateral approval for non-EOA wallets goes through the "
                "Builder-Relayer seam. # TODO(verify) py-builder-relayer-"
                "client (PRD §11.2)."
            )
        current = int(
            await self._web3.call(
                contract_address=collateral,
                abi=ERC20_APPROVE_ABI,
                fn_name="allowance",
                args=(binding.funder, spender),
            )
        )
        if current >= MAX_UINT256:
            return None  # already approved — idempotent no-op
        key = await self._resolve_key(binding)
        return await self._web3.send_signed(
            contract_address=collateral,
            abi=ERC20_APPROVE_ABI,
            fn_name="approve",
            args=(spender, MAX_UINT256),
            signer_private_key=key,
            from_address=binding.funder,
            chain_id=self._chain_id,
        )

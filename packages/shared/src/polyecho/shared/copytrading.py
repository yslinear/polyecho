"""
polyecho.shared.copytrading
============================
**FROZEN Phase-4 Wave-1 contract module.**

Wave-2 (core / cli / web) imports the dataclasses, enums and protocols
defined here *verbatim*.  The signatures in this file are **binding**: the
copy-trade engine, risk gate, redeem worker and the live-money chokepoint
all build on exactly these shapes.  Treat every public symbol as an API
surface — additive evolution only.

What lives here (and only here)
-------------------------------
* :class:`SkipReason`        — queryable skip/error taxonomy (brief §7; the
                               ``copy_skips.skip_reason`` value set).
* :class:`DecisionOutcome`   — terminal verdict of one evaluation.
* :class:`Decision`          — what the engine decided to do for one trade.
* :class:`RiskResult`        — the risk gate's pass/reject verdict.
* :class:`EvaluatedCopy`     — the full evaluation bundle Wave-2 acts on.
* :class:`WalletBinding`     — per-account ``(signature_type, funder)``.
* :class:`RedeemJobSpec`     — durable redeem-job intent (mirrors the
                               ``redeem_jobs`` row, brief §9).
* :class:`RedeemResult`      — outcome of a redeem attempt.
* :class:`CopyEvaluator`     — the ``evaluate_copy``-shaped Protocol.
* :class:`RiskGate`          — the pre-trade gate Protocol (brief §4).

No network / DB / SDK I/O on import.  Pure types only.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SkipReason",
    "DecisionOutcome",
    "TradeSide",
    "Decision",
    "RiskResult",
    "EvaluatedCopy",
    "WalletBinding",
    "RedeemJobStatus",
    "RedeemJobSpec",
    "RedeemResult",
    "CopyEvaluator",
    "RiskGate",
]


# ---------------------------------------------------------------------------
# Skip / error taxonomy (brief §7 — "a queryable skip/error log with a
# skip-reason taxonomy").  These are the persisted ``copy_skips.skip_reason``
# values; the StrEnum value IS the stored string (stable, queryable).
# ---------------------------------------------------------------------------
class SkipReason(StrEnum):
    """Why a candidate copy-trade was skipped or rejected (brief §7).

    Ordered cheap→expensive to mirror the brief §4 fail-closed gate order.
    """

    #: Idempotency: txHash / (leader,tradeId) already processed (brief §3).
    DUP = "dup"
    #: No active follow_relation for this (account, leader) (brief §4).
    NOT_FOLLOWING = "not_following"
    #: Keyword / category filter excluded the market (brief §4).
    FILTER_KEYWORD = "filter_keyword"
    #: Price outside the template min/max price band (brief §4).
    FILTER_PRICE_BAND = "filter_price_band"
    #: Market end-date too close / already ended (brief §4).
    FILTER_MARKET_END = "filter_market_end"
    #: Orderbook spread wider than the template max spread (brief §4).
    ORDERBOOK_SPREAD = "orderbook_spread"
    #: Orderbook depth below the template min depth (brief §4).
    ORDERBOOK_DEPTH = "orderbook_depth"
    #: Position-value / concurrency cap hit (brief §4/§7 — KEEP this cap).
    POSITION_CAP = "position_cap"
    #: Computed size below the venue / template minimum (brief §3).
    BELOW_MIN_SIZE = "below_min_size"
    #: Per-day order count cap hit (brief §4).
    DAILY_ORDER_CAP = "daily_order_cap"
    #: Per-day realised+open loss cap / circuit breaker hit (brief §4/§7).
    DAILY_LOSS_CAP = "daily_loss_cap"
    #: Cross-leader / cross-account portfolio exposure cap (brief §7).
    PORTFOLIO_CAP = "portfolio_cap"
    #: Slippage vs best-ask exceeded tolerance (brief §3/§4).
    SLIPPAGE_REJECT = "slippage_reject"
    #: Leader strategy drifted away from the followed profile (brief §7).
    STRATEGY_DRIFT = "strategy_drift"
    #: Fail-closed: a required position/orderbook/market fetch failed
    #: (brief §4 — "fail-closed: a fetch failure ⇒ reject").
    DATA_UNAVAILABLE = "data_unavailable"
    #: The operator-controlled ``auto_redeem`` toggle is OFF — the redeem
    #: was upserted for audit but no on-chain call was made.
    LIVE_LOCKED = "live_locked"


class DecisionOutcome(StrEnum):
    """Terminal verdict of one copy evaluation."""

    #: Place the order described by :class:`Decision`.
    COPY = "copy"
    #: Do nothing; see :attr:`Decision.skip_reason`.
    SKIP = "skip"


class TradeSide(StrEnum):
    """Order side (mirrors py-clob-client-v2 BUY/SELL)."""

    BUY = "BUY"
    SELL = "SELL"


# ---------------------------------------------------------------------------
# Wallet binding (brief §9 per-wallet-mode resolver output)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WalletBinding:
    """Resolved ``(signature_type, funder)`` for one account (brief §9).

    Order signing is mode-AGNOSTIC: wallet mode only sets the order's
    ``signatureType`` int and the maker/``funder`` address — everything
    else is identical and owned by py-clob-client-v2.  This is the entire
    surface Wave-2's order placement needs.

    Attributes:
        signature_type: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE,
                        3=POLY_1271 (brief §6/§9).
        funder:         Maker / funder address (lowercased ``0x…``).
        wallet_mode:    Human label: ``eoa`` | ``magic`` | ``safe`` |
                        ``poly_1271``.
        derived:        ``True`` if ``funder`` came from address derivation
                        (CREATE2), ``False`` if operator-supplied.  Brief
                        §6/§9: prefer operator-confirmed; derivation is a
                        convenience to verify, never to trust blindly.
    """

    signature_type: int
    funder: str
    wallet_mode: str
    derived: bool


# ---------------------------------------------------------------------------
# Decision / risk / evaluation bundle
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Decision:
    """What the engine decided to do for one detected leader trade.

    A ``COPY`` decision carries the fully-computed order parameters; a
    ``SKIP`` decision carries the :class:`SkipReason`.

    Attributes:
        outcome:       COPY or SKIP.
        skip_reason:   Set iff ``outcome is SKIP``.
        side:          Order side for a COPY decision.
        token_id:      Polymarket outcome token id (COPY).
        price:         Limit price per share (COPY) — ``Decimal`` to avoid
                       float rounding on money.
        size:          Share quantity (COPY).
        order_type:    ``"FOK"`` | ``"FAK"`` | ``"GTC"`` (brief §2 default
                       FAK/FOK taker).
        detail:        Free-text context (logged into ``copy_skips.detail``
                       on SKIP).
    """

    outcome: DecisionOutcome
    skip_reason: SkipReason | None = None
    side: TradeSide | None = None
    token_id: str | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    order_type: str = "FOK"
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RiskResult:
    """The pre-trade risk gate's verdict (brief §4).

    Attributes:
        passed:       ``True`` ⇒ proceed to sign/submit.
        skip_reason:  The first failing gate's reason iff ``not passed``.
        adjusted_size: Size after normalize/clamp (brief §3/§4 "adjust,
                       skip only if infeasible"); ``None`` ⇒ unchanged.
        detail:       Free-text explanation for the audit log.
    """

    passed: bool
    skip_reason: SkipReason | None = None
    adjusted_size: Decimal | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluatedCopy:
    """The full evaluation bundle Wave-2 acts on (brief §3/§5 pipeline).

    Produced by a :class:`CopyEvaluator`; carries the dedup key, the
    resolved binding, the engine decision and the risk verdict so the
    executor and the ``copy_skips`` / ``my_trades`` writers need no further
    lookups.

    Attributes:
        leader_address: Leader whose trade triggered this.
        tx_hash:        Cross-source dedup key (brief §3 — also the
                        ``leader_trades(leader_address, tx_hash)`` UNIQUE).
        account_id:     The following account.
        condition_id:   Market condition id.
        binding:        Resolved wallet binding for the account.
        decision:       The engine decision.
        risk:           The risk verdict (``None`` until the gate runs;
                        a SKIP decision may short-circuit before risk).
        raw:            Original detected-trade payload (defensive / audit).
    """

    leader_address: str
    tx_hash: str
    account_id: uuid.UUID
    condition_id: str
    binding: WalletBinding
    decision: Decision
    risk: RiskResult | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Redeem (brief §9 — durable redeem_jobs, idempotent, survives restart)
# ---------------------------------------------------------------------------
class RedeemJobStatus(StrEnum):
    """``redeem_jobs.status`` lifecycle (brief §9)."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RedeemJobSpec:
    """Intent to redeem one resolved position (mirrors a ``redeem_jobs`` row).

    Idempotent on ``(account_id, condition_id, outcome_index)`` (brief §9).
    The collateral choice (pUSD vs WrappedCollateral) is derived from
    ``is_neg_risk`` — the brief's "highest-risk detail to get right".

    Attributes:
        account_id:    Owning account.
        condition_id:  CTF condition id.
        outcome_index: Winning outcome index.
        index_set:     ``2**outcome_index`` bitmask passed to
                       ``redeemPositions`` (brief §9).
        is_neg_risk:   NegRisk market ⇒ WrappedCollateral + post-unwrap.
        detected_at:   When the redeemable flag was first observed.
    """

    account_id: uuid.UUID
    condition_id: str
    outcome_index: int
    index_set: int
    is_neg_risk: bool
    detected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RedeemResult:
    """Outcome of a redeem attempt (brief §9).

    Attributes:
        status:      Resulting :class:`RedeemJobStatus`.
        tx_hash:     On-chain / relayer tx hash if submitted.
        confirmed_at: Block-confirmation time if confirmed.
        skip_reason: Set iff the redeem was gated/rejected
                     (e.g. LIVE_LOCKED, DATA_UNAVAILABLE).
        detail:      Free-text context for the audit log.
    """

    status: RedeemJobStatus
    tx_hash: str | None = None
    confirmed_at: datetime | None = None
    skip_reason: SkipReason | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Protocols Wave-2 implements (frozen shapes)
# ---------------------------------------------------------------------------
@runtime_checkable
class CopyEvaluator(Protocol):
    """The ``evaluate_copy``-shaped contract (brief §3/§5 pipeline).

    Wave-2's engine implements this.  Given a detected leader trade plus
    the resolved wallet binding, it runs dedup → branch → size → filters
    and returns a fully-decided :class:`EvaluatedCopy`.  It does NOT place
    orders and does NOT run the risk gate (that is :class:`RiskGate`); it
    is pure decision logic so it is unit-testable without network.
    """

    async def evaluate_copy(
        self,
        *,
        leader_address: str,
        tx_hash: str,
        account_id: uuid.UUID,
        condition_id: str,
        binding: WalletBinding,
        trade: Mapping[str, Any],
    ) -> EvaluatedCopy: ...


@runtime_checkable
class RiskGate(Protocol):
    """The pre-trade risk gate (brief §4 — cheap→expensive, fail-closed).

    Implementations MUST be fail-closed: any required position / orderbook
    / market fetch failure ⇒ reject with
    :attr:`SkipReason.DATA_UNAVAILABLE` (brief §4).  ``evaluated`` carries
    the COPY decision to vet; the returned :class:`RiskResult` may carry an
    ``adjusted_size`` (normalize/clamp before reject).
    """

    async def check(self, evaluated: EvaluatedCopy) -> RiskResult: ...


# Importable type aliases used by Sequence-typed callers (kept for Wave-2
# convenience; not a behavioural contract).
SkipReasonList = Sequence[SkipReason]

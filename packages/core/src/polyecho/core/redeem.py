"""
polyecho.core.redeem
====================
Durable redeem worker (PolyHermes brief §9 — Phase-4 W2).

Flow (brief §9):

1. Poll the datasource ``redeemable`` flag (do NOT compute UMA/on-chain
   resolution — adopt the pragmatic API-trust).
2. Upsert a :class:`~polyecho.shared.copytrading.RedeemJobSpec` —
   **idempotent on ``(account_id, condition_id, outcome_index)``** so a
   restart never double-redeems (the durable improvement over
   PolyHermes' in-memory-only dedup).
3. In **live** mode call
   :class:`~polyecho.shared.polymarket.redeem.BaseRedeemClient.redeem`
   (NegRisk ⇒ WrappedCollateral + post-``unwrap``; normal ⇒ pUSD — the
   collateral choice is the brief's "highest-risk detail", derived from
   ``is_neg_risk``).  Gated by ``auto_redeem`` AND the live lock.
4. Default dry-run: log the redeem intent with
   :attr:`SkipReason.LIVE_LOCKED`; call **nothing**.

The live-lock chokepoint is ``BaseRedeemClient.redeem`` itself (it calls
``assert_live_allowed``); this worker additionally short-circuits on
``is_live_enabled()`` so the default path performs zero redeem work.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    SkipReason,
    WalletBinding,
    is_live_enabled,
)
from polyecho.shared.polymarket.redeem import BaseRedeemClient, index_set_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedeemCandidate:
    """One ``redeemable`` position discovered from the datasource."""

    account_id: uuid.UUID
    condition_id: str
    outcome_index: int
    is_neg_risk: bool


#: ``async () -> Sequence[RedeemCandidate]`` — the datasource ``redeemable`` poll.
CandidateSource = Callable[[], Awaitable[Sequence[RedeemCandidate]]]
#: ``async (RedeemJobSpec) -> bool`` — upsert; ``True`` ⇒ newly created (not dup).
JobUpserter = Callable[[RedeemJobSpec], Awaitable[bool]]


@dataclass(frozen=True)
class RedeemOutcome:
    """What the worker did for one candidate (dry-run or live)."""

    spec: RedeemJobSpec
    created: bool
    result: RedeemResult


class RedeemWorker:
    """Polls ``redeemable`` → idempotent upsert → live-locked redeem.

    Args:
        candidate_source: ``async () -> [RedeemCandidate]``.
        job_upserter:     ``async (spec) -> created?`` — enforces the
                          ``(account, condition, outcome)`` idempotency.
        binding_for:      ``async (account_id) -> WalletBinding`` — only
                          called on the live path (default never builds it).
        redeem_client:    A :class:`BaseRedeemClient` (its ``redeem`` is the
                          live-lock chokepoint); ``None`` ⇒ dry-run only.
        auto_redeem:      Mirrors ``settings.auto_redeem`` — even in live
                          mode redeem is skipped unless this is True.
    """

    def __init__(
        self,
        *,
        candidate_source: CandidateSource,
        job_upserter: JobUpserter,
        binding_for: Callable[[uuid.UUID], Awaitable[WalletBinding]] | None = None,
        redeem_client: BaseRedeemClient | None = None,
        auto_redeem: bool = False,
    ) -> None:
        self._candidate_source = candidate_source
        self._job_upserter = job_upserter
        self._binding_for = binding_for
        self._redeem_client = redeem_client
        self._auto_redeem = auto_redeem

    async def poll_once(self) -> list[RedeemOutcome]:
        """One redeem pass; idempotent, live-locked, restart-safe."""
        candidates = await self._candidate_source()
        outcomes: list[RedeemOutcome] = []
        for cand in candidates:
            spec = RedeemJobSpec(
                account_id=cand.account_id,
                condition_id=cand.condition_id,
                outcome_index=cand.outcome_index,
                index_set=index_set_for(cand.outcome_index),
                is_neg_risk=cand.is_neg_risk,
                detected_at=datetime.now(tz=UTC),
            )
            created = await self._job_upserter(spec)

            if not is_live_enabled() or not self._auto_redeem:
                logger.info(
                    "redeem_intent_dry_run",
                    extra={
                        "account_id": str(cand.account_id),
                        "condition_id": cand.condition_id,
                        "outcome_index": cand.outcome_index,
                        "is_neg_risk": cand.is_neg_risk,
                        "skip_reason": SkipReason.LIVE_LOCKED.value,
                        "auto_redeem": self._auto_redeem,
                    },
                )
                outcomes.append(
                    RedeemOutcome(
                        spec=spec,
                        created=created,
                        result=RedeemResult(
                            status=RedeemJobStatus.PENDING,
                            skip_reason=SkipReason.LIVE_LOCKED,
                            detail=(
                                "live/auto_redeem off — redeem intent logged, "
                                "NOT submitted"
                            ),
                        ),
                    )
                )
                continue

            # --- LIVE PATH (live_trading AND auto_redeem) ---------------
            if (  # pragma: no cover - live-only
                self._redeem_client is None or self._binding_for is None
            ):
                raise RuntimeError(
                    "live auto_redeem enabled but redeem_client/binding_for "
                    "not configured"
                )
            binding = await self._binding_for(cand.account_id)  # pragma: no cover
            result = await self._redeem_client.redeem(  # pragma: no cover - live-only
                spec, binding=binding
            )
            if (  # pragma: no cover - live-only
                cand.is_neg_risk
                and result.status is RedeemJobStatus.CONFIRMED
            ):
                # NegRisk pays WCOL → must unwrap (brief §9).
                await self._redeem_client.unwrap_collateral(binding=binding, amount=0)
            outcomes.append(  # pragma: no cover - live-only
                RedeemOutcome(spec=spec, created=created, result=result)
            )
        return outcomes

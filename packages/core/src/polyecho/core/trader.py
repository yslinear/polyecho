"""
polyecho.core.trader
====================
Trader-core copy-trade engine — the Phase-4 Wave-2 live money path
(PRD §4.2/§5.5, PolyHermes brief §3/§4/§7/§9).

Security contract (PRD §4.2, §7.2):

* This process is the ONLY one that holds private keys.
* It opens NO network ports; control flows through Postgres.
* Logs never contain private keys / API secrets / signatures.

HARD invariant: the engine NEVER places a real order or redeem unless
``is_live_enabled()`` is True (default OFF).  The dry-run path computes
the full :class:`~polyecho.shared.copytrading.Decision`, records the
intent as a ``CopySkip`` + structured log with
:attr:`SkipReason.LIVE_LOCKED`, and calls **no** SDK.  All real
placement/redeem goes through ``PolymarketClient`` / ``BaseRedeemClient``
(which call ``assert_live_allowed`` — that chokepoint is never bypassed).

``Decision`` is re-exported **verbatim** from the frozen Wave-1 contract
(``polyecho.shared.copytrading``) so the engine builds on exactly that
shape; ``DecisionOutcome`` is likewise the frozen enum.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from polyecho.core.detection import (
    ChainCrossCheckListener,
    DetectedTrade,
    TradeDeduplicator,
    UserChannelListener,
)
from polyecho.core.evaluator import TemplateConfig, TraderCopyEvaluator
from polyecho.core.execution import BuyLot, FifoLedger, OrderExecutor
from polyecho.core.reconcile import Reconciler
from polyecho.core.redeem import RedeemCandidate, RedeemWorker
from polyecho.core.risk import LayeredRiskGate, RiskContext
from polyecho.shared.config import get_settings
from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    RedeemJobSpec,
    SkipReason,
    TradeSide,
    WalletBinding,
    is_live_enabled,
)
from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource

logger = logging.getLogger(__name__)

__all__ = ["Decision", "DecisionOutcome", "TraderCore", "FollowSpec"]


@dataclass(frozen=True)
class FollowSpec:
    """A resolved ``(account, leader, template, binding)`` follow relation.

    The brief (§4) snapshots the copy template into the follow relation on
    apply (decoupled, not FK-bound); this is that immutable in-memory view
    the engine carries — built from the DB for live, hand-built for tests.
    """

    account_id: uuid.UUID
    leader_address: str
    template: TemplateConfig
    binding: WalletBinding
    last_seen_ts: datetime | None = None


# Hook signatures the engine calls so it stays DB/network-free in tests.
SkipRecorder = Callable[[EvaluatedCopy, SkipReason, str], Awaitable[None]]
TradeRecorder = Callable[[EvaluatedCopy, dict[str, Any]], Awaitable[None]]
EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class _Stats:
    """Per-run counters surfaced to ``--once`` and tests."""

    detected: int = 0
    deduped: int = 0
    copied: int = 0
    skipped: int = 0
    placed: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)


class TraderCore:
    """Main copy-trading execution service (PRD §4.2/§5.5).

    Wires detection → dedup → evaluate_copy → fail-closed risk gate →
    execution (live-locked) → reconcile → redeem.  Every collaborator is
    injectable so the whole pipeline runs against the bundled fixture
    datasource with no network and no DB.

    Default construction is fixture-backed and live-locked: a
    ``TraderCore()`` followed by ``run_once()`` performs a single dry-run
    pass and places nothing.
    """

    def __init__(
        self,
        *,
        follows: list[FollowSpec] | None = None,
        datasource: DataSource | None = None,
        confirmations: int = 1,
        skip_recorder: SkipRecorder | None = None,
        trade_recorder: TradeRecorder | None = None,
        event_emitter: EventEmitter | None = None,
        risk_context_for: Callable[[EvaluatedCopy], Awaitable[RiskContext]] | None = None,
        orderbook_for: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        market_end_for: Callable[[str], datetime | None] | None = None,
        redeem_candidate_source: Callable[[], Awaitable[list[RedeemCandidate]]] | None = None,
    ) -> None:
        self._settings = get_settings()
        self._ds: DataSource = datasource if datasource is not None else get_datasource()
        self._follows: list[FollowSpec] = follows if follows is not None else []
        self._confirmations = max(1, confirmations)

        self._queue: asyncio.Queue[DetectedTrade] = asyncio.Queue()
        self._dedup = TradeDeduplicator()
        self._ledger = FifoLedger()

        # In-proc skip / trade / event sinks (the durable writers are the
        # injected hooks; defaults keep an inspectable in-memory log so
        # tests can assert the SkipReason taxonomy — brief §7 #6).
        self.skips: list[tuple[str, str]] = []  # (skip_reason, detail)
        self.trades: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.stats = _Stats()

        self._skip_recorder = skip_recorder
        self._trade_recorder = trade_recorder
        self._event_emitter = event_emitter

        self._follow_index = {
            (str(f.account_id), f.leader_address.lower()): f for f in self._follows
        }

        self._evaluator = TraderCopyEvaluator(
            template_for=self._template_for,
            market_end_for=market_end_for,
            is_following=self._is_following,
        )
        self._risk = LayeredRiskGate(
            context_for=risk_context_for or self._default_risk_context,
            orderbook_for=orderbook_for,
        )
        self._executor = OrderExecutor(ledger=self._ledger)
        self._redeem_candidate_source = redeem_candidate_source

    # ------------------------------------------------------------------
    # Evaluator / risk hooks
    # ------------------------------------------------------------------

    def _template_for(
        self, account_id: uuid.UUID, leader: str
    ) -> TemplateConfig | None:
        spec = self._follow_index.get((str(account_id), leader.lower()))
        return spec.template if spec is not None else None

    def _is_following(self, account_id: uuid.UUID, leader: str) -> bool:
        return (str(account_id), leader.lower()) in self._follow_index

    async def _default_risk_context(self, evaluated: EvaluatedCopy) -> RiskContext:
        tpl = self._template_for(evaluated.account_id, evaluated.leader_address)
        if tpl is None:
            # Fail-closed: a missing template is treated as data-unavailable
            # by raising, which the gate converts to DATA_UNAVAILABLE.
            raise RuntimeError("no template snapshot for risk context")
        # The tx is already claimed in process_detected before the gate
        # runs, so the dup gate here is a no-op (False) on the first pass;
        # the durable dup defence is the leader_trades UNIQUE + LRU.
        return RiskContext(
            template=tpl,
            already_processed=False,
            is_following=self._is_following(
                evaluated.account_id, evaluated.leader_address
            ),
            open_position_count=len(
                self._ledger.open_lots(
                    evaluated.account_id,
                    evaluated.condition_id,
                    str(evaluated.raw.get("outcome") or ""),
                )
            ),
        )

    # ------------------------------------------------------------------
    # Recording helpers (default in-memory; durable via injected hooks)
    # ------------------------------------------------------------------

    async def _record_skip(
        self, evaluated: EvaluatedCopy, reason: SkipReason, detail: str
    ) -> None:
        self.skips.append((reason.value, detail))
        self.stats.skipped += 1
        self.stats.skip_reasons[reason.value] = (
            self.stats.skip_reasons.get(reason.value, 0) + 1
        )
        logger.info(
            "copy_skip",
            extra={
                "leader": evaluated.leader_address,
                "tx_hash": evaluated.tx_hash,
                "condition_id": evaluated.condition_id,
                "skip_reason": reason.value,
                "detail": detail,
            },
        )
        if self._skip_recorder is not None:
            await self._skip_recorder(evaluated, reason, detail)

    async def _record_trade(
        self, evaluated: EvaluatedCopy, info: dict[str, Any]
    ) -> None:
        self.trades.append(info)
        if self._trade_recorder is not None:
            await self._trade_recorder(evaluated, info)

    async def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))
        logger.info("event", extra={"event_type": event_type, **payload})
        if self._event_emitter is not None:
            await self._event_emitter(event_type, payload)

    # ------------------------------------------------------------------
    # Pipeline: one detected trade → fully processed
    # ------------------------------------------------------------------

    async def process_detected(self, trade: DetectedTrade) -> None:
        """Run one detected trade through the full pipeline (brief §3/§4)."""
        self.stats.detected += 1
        leader = str(trade["leader_address"]).lower()
        tx_hash = str(trade["tx_hash"])

        # Idempotency layers 1 & 2 (brief §3): per-tx lock + LRU.
        lock = self._dedup.lock_for(leader, tx_hash)
        async with lock:
            if not self._dedup.claim(leader, tx_hash):
                self.stats.deduped += 1
                logger.info(
                    "trade_deduped",
                    extra={"leader": leader, "tx_hash": tx_hash},
                )
                return

            condition_id = str(trade.get("condition_id") or "")
            for spec in self._follows:
                if spec.leader_address.lower() != leader:
                    continue
                await self._process_for_follow(spec, trade, condition_id, tx_hash)

    async def _process_for_follow(
        self,
        spec: FollowSpec,
        trade: DetectedTrade,
        condition_id: str,
        tx_hash: str,
    ) -> None:
        evaluated = await self._evaluator.evaluate_copy(
            leader_address=spec.leader_address,
            tx_hash=tx_hash,
            account_id=spec.account_id,
            condition_id=condition_id,
            binding=spec.binding,
            trade=trade,
        )
        decision = evaluated.decision

        if decision.outcome is DecisionOutcome.SKIP:
            await self._record_skip(
                evaluated,
                decision.skip_reason or SkipReason.DATA_UNAVAILABLE,
                decision.detail or "evaluator skip",
            )
            return

        # Fail-closed layered risk gate (brief §4).
        risk = await self._risk.check(evaluated)
        evaluated = EvaluatedCopy(
            leader_address=evaluated.leader_address,
            tx_hash=evaluated.tx_hash,
            account_id=evaluated.account_id,
            condition_id=evaluated.condition_id,
            binding=evaluated.binding,
            decision=decision,
            risk=risk,
            raw=evaluated.raw,
        )
        if not risk.passed:
            reason = risk.skip_reason or SkipReason.DATA_UNAVAILABLE
            await self._record_skip(evaluated, reason, risk.detail or "risk reject")
            await self._emit_event(
                "risk_triggered",
                {
                    "leader": spec.leader_address,
                    "tx_hash": tx_hash,
                    "skip_reason": reason.value,
                },
            )
            return

        self.stats.copied += 1

        # Execution — live-locked by default (places nothing unless live).
        result = await self._executor.execute(evaluated)

        side = decision.side
        size = risk.adjusted_size if risk.adjusted_size is not None else decision.size
        price = decision.price or Decimal(0)
        outcome_name = str(trade.get("outcome") or decision.token_id or condition_id)

        if result.placed:  # pragma: no cover - live-only
            self.stats.placed += 1
            if side is TradeSide.BUY:
                self._ledger.add_buy(
                    BuyLot(
                        trade_id=tx_hash,
                        account_id=spec.account_id,
                        condition_id=condition_id,
                        outcome=outcome_name,
                        size=size or Decimal(0),
                        price=price,
                    )
                )
            await self._record_trade(
                evaluated,
                {
                    "tx_hash": tx_hash,
                    "side": side.value if side else None,
                    "size": str(size),
                    "price": str(price),
                    "fill_status": "filled",
                },
            )
            await self._emit_event(
                "trade_opened" if side is TradeSide.BUY else "trade_closed",
                {"leader": spec.leader_address, "tx_hash": tx_hash},
            )
        else:
            # Default dry-run: still record the FIFO BUY lot so PnL/limits
            # accounting is correct, log the LIVE_LOCKED intent as a skip.
            if side is TradeSide.BUY:
                self._ledger.add_buy(
                    BuyLot(
                        trade_id=tx_hash,
                        account_id=spec.account_id,
                        condition_id=condition_id,
                        outcome=outcome_name,
                        size=size or Decimal(0),
                        price=price,
                    )
                )
            await self._record_skip(
                evaluated,
                result.skip_reason or SkipReason.LIVE_LOCKED,
                result.detail or "dry-run: not placed",
            )

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run_once(self) -> _Stats:
        """Single dry-run fixture pass (the ``--once`` / CI-smoke path).

        Runs both detection producers once into the queue, drains it
        through the full pipeline, then a single reconcile + redeem pass.
        Returns the per-run :class:`_Stats`.  Places nothing (live lock).
        """
        leaders = sorted({f.leader_address.lower() for f in self._follows})
        ws = UserChannelListener(leaders, self._queue, datasource=self._ds, live=False)
        chain = ChainCrossCheckListener(
            leaders,
            self._queue,
            datasource=self._ds,
            confirmations=self._confirmations,
            live=False,
        )
        await ws.run_once()
        await chain.run_once()

        while not self._queue.empty():
            trade = self._queue.get_nowait()
            try:
                await self.process_detected(trade)
            finally:
                self._queue.task_done()

        await self._reconcile_once()
        await self._redeem_once()
        logger.info(
            "run_once_complete",
            extra={
                "detected": self.stats.detected,
                "deduped": self.stats.deduped,
                "copied": self.stats.copied,
                "skipped": self.stats.skipped,
                "placed": self.stats.placed,
                "live_enabled": is_live_enabled(),
            },
        )
        return self.stats

    async def _reconcile_once(self) -> int:
        async def _read() -> dict[str, datetime | None]:
            return {f.leader_address.lower(): f.last_seen_ts for f in self._follows}

        async def _write(_leader: str, _ts: datetime) -> None:
            return None

        async def _enqueue(t: DetectedTrade) -> None:
            await self._queue.put(t)

        rec = Reconciler(
            cursor_reader=_read,
            cursor_writer=_write,
            enqueue=_enqueue,
            datasource=self._ds,
        )
        n = await rec.reconcile_once()
        # Drain anything reconcile re-enqueued (dedup makes dups no-ops).
        while not self._queue.empty():
            trade = self._queue.get_nowait()
            try:
                await self.process_detected(trade)
            finally:
                self._queue.task_done()
        return n

    async def _redeem_once(self) -> int:
        if self._redeem_candidate_source is None:
            return 0

        async def _upsert(_spec: RedeemJobSpec) -> bool:
            return True

        worker = RedeemWorker(
            candidate_source=self._redeem_candidate_source,
            job_upserter=_upsert,
            auto_redeem=bool(self._settings.auto_redeem),
        )
        outcomes = await worker.poll_once()
        for o in outcomes:
            self.events.append(("redeem_intent", {"condition_id": o.spec.condition_id}))
        return len(outcomes)

    async def run(self) -> None:
        """Supervised loop: detection / reconcile / redeem TaskGroup.

        Fixture mode performs one bounded ``run_once`` pass then idles
        (restart-safe; reconcile is the durable backfill).  The live WS /
        RPC streams are the Phase-4 W2 deliverable behind the same
        interface and only ever exercised with ``POLYECHO_LIVE_TRADING``.
        """
        await self.run_once()
        # Idle until cancelled by the supervising TaskGroup / signal — an
        # Event that is never set is the lint-clean "park forever" idiom
        # (no busy sleep loop).  Live WS/RPC stream tasks attach here in
        # the Phase-4 W2 live build behind the same interface.
        async with asyncio.TaskGroup() as tg:  # pragma: no cover - supervised loop
            tg.create_task(self._park())

    async def _park(self) -> None:  # pragma: no cover - supervised loop
        await asyncio.Event().wait()

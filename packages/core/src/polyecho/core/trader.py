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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polyecho.core._perf import (
    COPY_LATENCY_TARGET_MS,
    detect_lag_ms,
    perf_timer,
)
from polyecho.core.detection import (
    ChainCrossCheckListener,
    DetectedTrade,
    TradeDeduplicator,
    UserChannelListener,
)
from polyecho.core.evaluator import TemplateConfig, TraderCopyEvaluator
from polyecho.core.events import EventType, PushPayload, Severity, safe_emit
from polyecho.core.execution import BuyLot, FifoLedger, OrderExecutor
from polyecho.core.pending import PendingActionExecutor
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
from polyecho.shared.risk_mode import RiskMode, apply_conservative, get_risk_mode

logger = logging.getLogger(__name__)

#: Funder USDC balance (in USD) at/under which SYSTEM_LOW_BALANCE fires.
#:
#: Rationale: Polymarket's CLOB enforces a $1 minimum order notional, and a
#: copy-trade leg also has to clear the configured ``min_trade_size_usd``
#: (the bundled templates default this to $1) PLUS leave headroom for the
#: maker/taker fee + price slippage on the next fill.  A funder sitting at
#: only a couple of dollars can therefore no longer place even the smallest
#: copy order, so $5 is a defensible "you will start missing fills now"
#: early-warning floor — high enough to warn *before* the account goes
#: fully dead, low enough never to false-alarm a normally funded account.
#: It is a documented constant (not env-tunable) so the alert threshold is
#: part of the reviewed contract; tune here if the min-order economics move.
LOW_BALANCE_THRESHOLD_USD: float = 5.0

#: Engine-injected, optional async balance probe:
#: ``async (account_id) -> funder USDC balance in USD | None``.  Defaults to
#: ``None`` (no probe) so the fixture / ``--once`` / unit paths stay
#: network-free and unchanged; the live wiring injects a real
#: funder-balance reader.  A ``None`` return == "balance unknown" ⇒ never
#: alarms (best-effort, fail-open for the *alert* — the money lock is
#: unaffected either way).
BalanceFetcher = Callable[[uuid.UUID], Awaitable[float | None]]

__all__ = [
    "Decision",
    "DecisionOutcome",
    "LOW_BALANCE_THRESHOLD_USD",
    "TraderCore",
    "FollowSpec",
    "PendingActionExecutor",
]


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
#: P1-4 ``async (leader_address, last_seen_ts) -> None`` — persist the
#: per-leader reconcile cursor (the live wiring writes
#: ``follow_relations.last_seen_ts``; default keeps an in-proc store so
#: ``--once`` / tests stay DB-free yet still observe cursor advancement).
CursorWriter = Callable[[str, datetime], Awaitable[None]]
#: P1-5 ``async (info: dict) -> None`` — write the ``trader_heartbeat``
#: settings row (timestamp + last-poll info).  Best-effort; the live
#: wiring upserts the ``settings`` table, default is an in-proc mirror so
#: ``polyecho trader status`` liveness is measurable without a DB in tests.
HeartbeatWriter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _Stats:
    """Per-run counters surfaced to ``--once`` and tests."""

    detected: int = 0
    deduped: int = 0
    copied: int = 0
    skipped: int = 0
    placed: int = 0
    errors: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    #: Observability only (PRD §7.1) — best-effort copy-trade latency
    #: accumulators surfaced on ``run_once_complete``.  These are NOT
    #: counters consumed by the exit-code contract; ``degenerate_once_pass``
    #: never reads them, so a timing failure cannot change ``--once``.
    copy_latency_ms_total: float = 0.0
    copy_latency_samples: int = 0
    copy_latency_ms_max: float = 0.0


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
        pending_action_executor: PendingActionExecutor | None = None,
        balance_for: BalanceFetcher | None = None,
        cursor_writer: CursorWriter | None = None,
        heartbeat_writer: HeartbeatWriter | None = None,
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

        # P1-4: per-leader reconcile cursor.  Seeded from the FollowSpec
        # snapshots so a restart resumes from the persisted ``last_seen_ts``
        # (restart-safe).  The injected ``cursor_writer`` persists each
        # advance to ``follow_relations.last_seen_ts`` in the live wiring;
        # the default keeps this in-proc store so ``--once`` / tests stay
        # DB-free yet still observe and verify cursor advancement.
        self._cursor_writer = cursor_writer
        self._cursor_store: dict[str, datetime | None] = {}
        for f in self._follows:
            lk = f.leader_address.lower()
            if f.last_seen_ts is not None and (
                lk not in self._cursor_store
                or self._cursor_store[lk] is None
                or f.last_seen_ts > self._cursor_store[lk]  # type: ignore[operator]
            ):
                self._cursor_store[lk] = f.last_seen_ts
            else:
                self._cursor_store.setdefault(lk, f.last_seen_ts)

        # P1-5: trader liveness.  The supervised loop + startup write a
        # ``trader_heartbeat`` row (timestamp + last-poll info) so
        # ``polyecho trader status`` / the Dashboard can show UP/DOWN.
        # Best-effort: a write failure is logged, never crashes the loop.
        self._heartbeat_writer = heartbeat_writer
        self.last_heartbeat: dict[str, Any] | None = None

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
        # Confirmed-pending-action executor (web confirms; core executes
        # through the same chokepoint).  Optional / injected so the engine
        # stays DB-free in tests.
        self._pending_executor = pending_action_executor
        # SYSTEM_BOT_RESTART fires exactly once per process start.
        self._bot_restart_emitted = False

        # --- PRD §5.8 system-health debounce state ----------------------
        # SYSTEM_API_FAILURE coalescing: a PolymarketClient call can fail
        # on every tick of a hot loop, so we emit ONLY on the transition
        # into a failing state, keyed per call-site (``"orderbook"`` /
        # ``"risk_context"`` / ``"order_place"``).  A call-site that is in
        # ``_api_failing`` has already pushed; it is cleared on the next
        # success so a *fresh* failure transition re-alerts (a flapping
        # API alerts once per outage, not once per tick).
        self._balance_for = balance_for
        self._api_failing: set[str] = set()
        # SYSTEM_LOW_BALANCE edge-trigger: per-account "we are currently
        # below threshold and have already pushed" set — emit once on the
        # downward crossing, re-arm only after recovery ≥ threshold.
        self._low_balance_alerted: set[str] = set()

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

    async def _resolve_template(self, tpl: TemplateConfig) -> TemplateConfig:
        """風控收斂: tighten the template when the dashboard is CONSERVATIVE.

        Reads the FROZEN :func:`polyecho.shared.risk_mode.get_risk_mode`
        (fail-safe NORMAL).  When ``CONSERVATIVE`` the snapshot is run
        through the PURE :func:`apply_conservative` (tighten-only, never
        loosen) BEFORE the LayeredRiskGate evaluates it, so a trade that
        passes under NORMAL can be clamped / rejected under CONSERVATIVE.

        ``TemplateConfig`` structurally satisfies the contract's
        ``ConservativeTemplate`` :class:`typing.Protocol` (all nine clamped
        field names + types match exactly: ``copy_ratio``,
        ``max_position_usd``, ``daily_loss_limit_usd``, ``daily_order_cap``,
        ``min_price``, ``max_price``, ``min_depth``, ``max_spread``,
        ``price_tolerance_pct``), and ``apply_conservative`` uses
        ``dataclasses.replace`` so the extra core-only fields
        (``fixed_size_usd`` etc.) are carried through untouched — no
        adapter is needed and ``shared`` is never edited.
        """
        try:
            mode = await get_risk_mode()
        except Exception:  # noqa: BLE001 - fail-safe (get_risk_mode also is)
            mode = RiskMode.NORMAL
        if mode is RiskMode.CONSERVATIVE:
            tightened = apply_conservative(tpl)
            logger.info(
                "risk_mode_conservative_applied",
                extra={
                    "max_position_usd": str(tightened.max_position_usd),
                    "daily_loss_limit_usd": str(tightened.daily_loss_limit_usd),
                    "daily_order_cap": tightened.daily_order_cap,
                    "min_price": str(tightened.min_price),
                    "max_price": str(tightened.max_price),
                    "min_depth": str(tightened.min_depth),
                    "max_spread": str(tightened.max_spread),
                    "price_tolerance_pct": str(tightened.price_tolerance_pct),
                },
            )
            return tightened
        return tpl

    async def _default_risk_context(self, evaluated: EvaluatedCopy) -> RiskContext:
        tpl = self._template_for(evaluated.account_id, evaluated.leader_address)
        if tpl is None:
            # Fail-closed: a missing template is treated as data-unavailable
            # by raising, which the gate converts to DATA_UNAVAILABLE.
            raise RuntimeError("no template snapshot for risk context")
        # 風控收斂: resolve the EFFECTIVE template (Conservative tighten-only)
        # right where the gate consumes it — before any gate runs.
        tpl = await self._resolve_template(tpl)
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

    async def _emit_push_event(
        self,
        *,
        event_type: EventType,
        severity: Severity = Severity.INFO,
        payload: dict[str, Any],
        push: PushPayload | None = None,
    ) -> None:
        """Emit a PRD §5.8 push event (best-effort) + keep the in-mem log.

        Routes through :func:`polyecho.core.events.safe_emit` so a NOTIFY /
        DB hiccup can never crash or stall the trading loop.  The in-memory
        ``self.events`` mirror + injected ``event_emitter`` hook are kept
        so existing tests / observers still see every event.
        """
        self.events.append((str(event_type), payload))
        logger.info(
            "push_event",
            extra={"event_type": str(event_type), "severity": str(severity)},
        )
        if self._event_emitter is not None:
            await self._event_emitter(str(event_type), payload)
        await safe_emit(
            event_type=event_type,
            severity=severity,
            payload=payload,
            push=push,
        )

    # ------------------------------------------------------------------
    # PRD §5.8 push-event builders (zh-TW copy, deep-links, dedupe tags)
    # ------------------------------------------------------------------

    async def _emit_fill_events(
        self,
        *,
        spec: FollowSpec,
        tx_hash: str,
        side: TradeSide | None,
        size: Decimal,
        price: Decimal,
        outcome: str,
        sell_match: Any | None,
    ) -> None:
        """COPY_FILL on every fill; on a SELL also POSITION_CLOSED /
        STOP_LOSS / TARGET_HIT depending on the realised PnL.
        """
        leader = spec.leader_address
        side_zh = "買入" if side is TradeSide.BUY else "賣出"
        base_payload: dict[str, Any] = {
            "leader": leader,
            "tx_hash": tx_hash,
            "account_id": str(spec.account_id),
            "side": side.value if side else None,
            "size": str(size),
            "price": str(price),
            "market": outcome,
        }
        await self._emit_push_event(
            event_type=EventType.COPY_FILL,
            severity=Severity.INFO,
            payload=base_payload,
            push=PushPayload(
                title=f"已跟單成交 · {side_zh}",
                body=(
                    f"領單者 {leader[:10]}… 市場 {outcome} "
                    f"{side_zh} {size} 股 @ {price}"
                ),
                url="/trades",
                tag=f"copy_fill:{tx_hash}",
                data={"tx_hash": tx_hash},
            ),
        )

        if side is not TradeSide.SELL or sell_match is None:
            return

        realised: Decimal = getattr(sell_match, "realised_pnl_usd", Decimal(0))
        closed_payload: dict[str, Any] = {
            **base_payload,
            "realised_pnl_usd": str(realised),
        }
        if realised < 0:
            await self._emit_push_event(
                event_type=EventType.STOP_LOSS,
                severity=Severity.WARNING,
                payload=closed_payload,
                push=PushPayload(
                    title="停損出場",
                    body=(
                        f"市場 {outcome} 已停損平倉，"
                        f"已實現損益 ${realised}"
                    ),
                    url="/trades",
                    tag=f"position:{spec.account_id}:{outcome}",
                    data={"tx_hash": tx_hash},
                ),
            )
        elif realised > 0:
            await self._emit_push_event(
                event_type=EventType.TARGET_HIT,
                severity=Severity.INFO,
                payload=closed_payload,
                push=PushPayload(
                    title="達標獲利出場",
                    body=(
                        f"市場 {outcome} 已停利平倉，"
                        f"已實現損益 ${realised}"
                    ),
                    url="/trades",
                    tag=f"position:{spec.account_id}:{outcome}",
                    data={"tx_hash": tx_hash},
                ),
            )
        await self._emit_push_event(
            event_type=EventType.POSITION_CLOSED,
            severity=Severity.INFO,
            payload=closed_payload,
            push=PushPayload(
                title="部位已平倉",
                body=(
                    f"市場 {outcome} 部位已平倉，"
                    f"已實現損益 ${realised}"
                ),
                url="/trades",
                tag=f"position:{spec.account_id}:{outcome}",
                data={"tx_hash": tx_hash},
            ),
        )

    async def _emit_risk_event(
        self,
        spec: FollowSpec,
        tx_hash: str,
        reason: SkipReason,
        detail: str,
    ) -> None:
        """Map a risk-gate reject to RISK_DAILY_LOSS / RISK_STRATEGY_DRIFT.

        ``DAILY_LOSS_CAP`` ⇒ RISK_DAILY_LOSS (WARNING; CRITICAL when the
        detail marks the hard MtM breaker).  ``STRATEGY_DRIFT`` ⇒
        RISK_STRATEGY_DRIFT (WARNING — the PRD §5.3 auto-pause behaviour is
        unchanged; this only ADDS the event).  Other reasons are
        non-actionable internal skips → no push event.
        """
        payload = {
            "leader": spec.leader_address,
            "tx_hash": tx_hash,
            "account_id": str(spec.account_id),
            "skip_reason": reason.value,
            "detail": detail,
        }
        if reason is SkipReason.DAILY_LOSS_CAP:
            hard = "MtM breaker" in detail
            await self._emit_push_event(
                event_type=EventType.RISK_DAILY_LOSS,
                severity=Severity.CRITICAL if hard else Severity.WARNING,
                payload=payload,
                push=PushPayload(
                    title="每日虧損熔斷觸發",
                    body=(
                        f"領單者 {spec.leader_address[:10]}… "
                        f"已觸發每日虧損上限，已暫停跟單"
                    ),
                    url="/dashboard",
                    tag=f"risk_daily_loss:{spec.account_id}",
                    data={"skip_reason": reason.value},
                ),
            )
        elif reason is SkipReason.STRATEGY_DRIFT:
            await self._emit_push_event(
                event_type=EventType.RISK_STRATEGY_DRIFT,
                severity=Severity.WARNING,
                payload=payload,
                push=PushPayload(
                    title="策略偏移偵測",
                    body=(
                        f"領單者 {spec.leader_address[:10]}… "
                        f"策略已偏離追蹤輪廓，已暫停跟單"
                    ),
                    url=f"/discovery/{spec.leader_address}",
                    tag=f"strategy_drift:{spec.leader_address}",
                    data={"skip_reason": reason.value},
                ),
            )

    # ------------------------------------------------------------------
    # Pipeline: one detected trade → fully processed
    # ------------------------------------------------------------------

    async def process_detected(self, trade: DetectedTrade) -> None:
        """Run one detected trade through the full pipeline (brief §3/§4).

        Observability (PRD §7.1 copy-trade latency < 5 s; PRD §2.3
        on-chain timestamp lag): the detection→decision/placement-or-skip
        path is wrapped in a best-effort monotonic timer.  The
        ``copy_trade_processed`` log line carries ``copy_latency_ms`` (and
        ``detect_lag_ms`` when the leader trade carries a source ts).  The
        timing is strictly best-effort — it cannot raise into the trade
        path and never changes control flow or the exit-code contract.
        """
        self.stats.detected += 1
        leader = str(trade["leader_address"]).lower()
        tx_hash = str(trade["tx_hash"])

        # Idempotency layers 1 & 2 (brief §3): per-tx lock + LRU.
        # C4a (R6): claim+lock is now ATOMIC — ``acquire_for`` resolves the
        # per-tx Lock and claims the key under one instance-level guard, so
        # a concurrent eviction can no longer swap the lock identity
        # between "get the lock" and "claim" (the old TOCTOU that let two
        # coroutines both think they held the lock for the same tx).
        with perf_timer() as elapsed:
            lock, is_new = await self._dedup.acquire_for(leader, tx_hash)
            try:
                if not is_new:
                    self.stats.deduped += 1
                    logger.info(
                        "trade_deduped",
                        extra={"leader": leader, "tx_hash": tx_hash},
                    )
                    return

                condition_id = str(trade.get("condition_id") or "")
                await self._process_follows(
                    trade, leader, tx_hash, condition_id
                )
            finally:
                lock.release()

        # Best-effort copy-trade latency observability — wrapped so a
        # timing/log failure can NEVER propagate into the trade outcome.
        self._log_copy_latency(trade, leader, tx_hash, elapsed[0])

    async def _process_follows(
        self,
        trade: DetectedTrade,
        leader: str,
        tx_hash: str,
        condition_id: str,
    ) -> None:
        """Fan one detected trade out to every matching follow spec.

        Extracted verbatim from :meth:`process_detected` so the
        copy-latency timer can wrap exactly the detection→decision/
        placement-or-skip path without altering any control flow.
        """
        checked_accounts: set[uuid.UUID] = set()
        for spec in self._follows:
            if spec.leader_address.lower() != leader:
                continue
            # SYSTEM_LOW_BALANCE: probe each funding account at most
            # once per detected trade (edge-triggered inside the
            # helper — emits once on the downward crossing, never
            # once-per-poll).  No-op unless a balance probe is wired.
            if spec.account_id not in checked_accounts:
                checked_accounts.add(spec.account_id)
                await self._check_low_balance(spec.account_id)
            await self._process_for_follow(spec, trade, condition_id, tx_hash)

    def _log_copy_latency(
        self,
        trade: DetectedTrade,
        leader: str,
        tx_hash: str,
        copy_latency_ms: float,
    ) -> None:
        """Emit the per-trade copy-latency structured log line.

        PRD §7.1 (copy-trade latency < 5 s) + PRD §2.3 (compare against
        the on-chain timestamp).  STRICTLY best-effort: the whole body is
        wrapped so a clock / arithmetic / logging failure can never
        propagate into the trade outcome.  No event is emitted (the
        shared ``EventType`` enum is frozen) — this is a LOG field only.
        """
        try:
            if copy_latency_ms < 0:
                # Sentinel from a misbehaving monotonic clock — skip the
                # line entirely rather than log a bogus negative latency.
                return
            lag_ms = detect_lag_ms(trade.get("timestamp"))
            extra: dict[str, Any] = {
                "leader": leader,
                "tx_hash": tx_hash,
                "copy_latency_ms": round(copy_latency_ms, 3),
                "copy_latency_target_ms": COPY_LATENCY_TARGET_MS,
            }
            if lag_ms is not None:
                extra["detect_lag_ms"] = round(lag_ms, 3)
            logger.info("copy_trade_processed", extra=extra)
            # Aggregate accumulators for ``run_once_complete`` (best-effort;
            # never read by the exit-code contract).
            self.stats.copy_latency_ms_total += copy_latency_ms
            self.stats.copy_latency_samples += 1
            if copy_latency_ms > self.stats.copy_latency_ms_max:
                self.stats.copy_latency_ms_max = copy_latency_ms
        except Exception as exc:  # noqa: BLE001 - observability is best-effort
            # A timing/log failure must NEVER affect the trade outcome.
            logger.debug(
                "copy_latency_log_failed_continuing",
                extra={"error": str(exc)},
            )

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
            detail = risk.detail or "risk reject"
            await self._record_skip(evaluated, reason, detail)
            await self._emit_event(
                "risk_triggered",
                {
                    "leader": spec.leader_address,
                    "tx_hash": tx_hash,
                    "skip_reason": reason.value,
                },
            )
            # A DATA_UNAVAILABLE reject whose detail marks a Polymarket
            # fetch error IS an API failure (the risk gate fails closed on
            # the orderbook / context fetch — see risk.py).  Map it to a
            # debounced SYSTEM_API_FAILURE keyed per call-site; any other
            # reject is a normal risk decision (NOT an API outage) and only
            # clears the orderbook flag if the fetch itself succeeded.
            if reason is SkipReason.DATA_UNAVAILABLE and (
                "orderbook fetch failed" in detail
                or "risk context fetch failed" in detail
            ):
                call_site = (
                    "orderbook"
                    if "orderbook fetch failed" in detail
                    else "risk_context"
                )
                await self._note_api_outcome(
                    call_site=call_site,
                    ok=False,
                    error=detail,
                    leader=spec.leader_address,
                    condition_id=condition_id,
                )
            else:
                # Reached the gate body ⇒ the orderbook fetch (if any) did
                # NOT raise; treat the API as healthy so a later real
                # outage re-alerts.
                await self._note_api_outcome(call_site="orderbook", ok=True)
            await self._emit_risk_event(spec, tx_hash, reason, risk.detail or "")
            return

        # A PASSED gate means every required Polymarket fetch succeeded.
        await self._note_api_outcome(call_site="orderbook", ok=True)
        await self._note_api_outcome(call_site="risk_context", ok=True)

        self.stats.copied += 1

        # P1-1: the risk gate may have normalised/clamped the size; the
        # placed order MUST be the gate-approved size, never the original
        # (potentially too-large) decision.size.  We pass adjusted_size
        # through as an explicit override so OrderExecutor places exactly
        # what the gate approved, and reuse result.decision below so the
        # FIFO lot / trade record / push payload all reflect that size.
        result = await self._executor.execute(
            evaluated, size_override=risk.adjusted_size
        )

        decision = result.decision
        side = decision.side
        size = decision.size
        price = decision.price or Decimal(0)
        outcome_name = str(trade.get("outcome") or decision.token_id or condition_id)

        # SYSTEM_API_FAILURE for the live order-placement call: the
        # executor returns DATA_UNAVAILABLE with this detail only after it
        # has exhausted its re-sign retries against PolymarketClient
        # (execution.py).  Debounced per "order_place" call-site; a placed
        # / live-locked / any-other result clears it.
        if (
            not result.placed
            and result.skip_reason is SkipReason.DATA_UNAVAILABLE
            and "order placement failed after retries" in (result.detail or "")
        ):
            await self._note_api_outcome(
                call_site="order_place",
                ok=False,
                error=result.detail or "",
                leader=spec.leader_address,
                condition_id=condition_id,
            )
        else:
            await self._note_api_outcome(call_site="order_place", ok=True)

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
            await self._emit_fill_events(
                spec=spec,
                tx_hash=tx_hash,
                side=side,
                size=size or Decimal(0),
                price=price,
                outcome=outcome_name,
                sell_match=result.sell_match,
            )
        else:
            # P1-3: a dry-run order is NOT a real fill — it must NOT mutate
            # the shared FifoLedger.  Adding a phantom BuyLot here inflated
            # ``open_position_count`` / portfolio-exposure counters (the
            # gate reads ``self._ledger.open_lots``) and risked FIFO-
            # matching a real SELL against a phantom BUY after a live flip.
            # Only the live ``result.placed`` branch above (a real fill)
            # touches the ledger; PnL/limits accounting stays correct for
            # the live path.  We just log the LIVE_LOCKED intent as a skip.
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
        await self._emit_bot_restart_once()
        # P1-5: startup/cycle heartbeat — write at the START of the pass so
        # liveness is observable even if the pass later errors.
        await self._write_heartbeat(phase="cycle_start")
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
            except Exception as exc:  # noqa: BLE001 - count, never abort pass
                self.stats.errors += 1
                logger.warning(
                    "process_detected_failed_continuing",
                    extra={"error": str(exc)},
                )
            finally:
                self._queue.task_done()

        await self._reconcile_once()
        await self._redeem_once()
        await self._pending_actions_once()
        # P1-5: end-of-cycle heartbeat — refreshes the timestamp + final
        # per-pass counters so ``trader status`` reflects the latest poll.
        await self._write_heartbeat(phase="cycle_complete")
        # Aggregate copy-trade latency (PRD §7.1) — best-effort, computed
        # so a divide / attribute slip can never break the pass summary.
        try:
            _samples = self.stats.copy_latency_samples
            _avg_ms = (
                round(self.stats.copy_latency_ms_total / _samples, 3)
                if _samples > 0
                else None
            )
            _max_ms = (
                round(self.stats.copy_latency_ms_max, 3) if _samples > 0 else None
            )
        except Exception:  # noqa: BLE001 - observability is best-effort
            _samples, _avg_ms, _max_ms = 0, None, None
        logger.info(
            "run_once_complete",
            extra={
                "detected": self.stats.detected,
                "deduped": self.stats.deduped,
                "copied": self.stats.copied,
                "skipped": self.stats.skipped,
                "placed": self.stats.placed,
                "live_enabled": is_live_enabled(),
                "copy_latency_samples": _samples,
                "copy_latency_ms_avg": _avg_ms,
                "copy_latency_ms_max": _max_ms,
                "copy_latency_target_ms": COPY_LATENCY_TARGET_MS,
            },
        )
        return self.stats

    def degenerate_once_pass(self) -> str | None:
        """P1-12: classify a completed ``run_once`` for the exit code.

        Exit-code contract (consumed by ``python -m polyecho.core --once``):

        * **No follows configured** ⇒ a legitimately empty/idle pass.
          Return ``None`` (exit 0) — there is nothing to detect, this is
          a valid steady state, NOT a broken pipeline.
        * **Follows exist but nothing was detected** ⇒ degenerate.  A
          configured follow producing zero detections across BOTH
          detection paths + reconcile means the pipeline is wholly
          broken (datasource/fixture/wiring) — return a reason string so
          ``--once`` exits non-zero and CI smoke fails loudly.
        * **Every detected trade errored** (``errors >= detected`` and
          ``detected > 0``) ⇒ degenerate (the pass produced only
          exceptions) — return a reason string (non-zero).

        Any other shape — including a pass that detected trades and only
        produced *skips* (e.g. all LIVE_LOCKED in the default dry-run, or
        legitimate risk rejects) — is a HEALTHY pass: return ``None``
        (exit 0).  Skips are the expected default-off outcome and must
        NOT be treated as failure.
        """
        if not self._follows:
            return None  # nothing configured ⇒ valid idle pass (exit 0)
        if self.stats.detected == 0:
            return (
                f"degenerate: {len(self._follows)} follow(s) configured but "
                f"0 trades detected (pipeline appears broken)"
            )
        if self.stats.errors > 0 and self.stats.errors >= self.stats.detected:
            return (
                f"degenerate: all {self.stats.detected} detected trade(s) "
                f"errored ({self.stats.errors} errors)"
            )
        return None

    async def _reconcile_once(self) -> int:
        async def _read() -> dict[str, datetime | None]:
            # P1-4: resume from the live in-proc cursor store (seeded from
            # the persisted FollowSpec snapshots), NOT a static re-read of
            # the original snapshot — so a second pass advances correctly.
            return {
                f.leader_address.lower(): self._cursor_store.get(
                    f.leader_address.lower(), f.last_seen_ts
                )
                for f in self._follows
            }

        async def _write(leader: str, ts: datetime) -> None:
            # P1-4: a REAL cursor writer (was a no-op ⇒ never restart-safe).
            # Always advance the in-proc store so the next reconcile pass
            # resumes correctly even DB-free, then best-effort-persist to
            # ``follow_relations.last_seen_ts`` via the injected writer.  A
            # persistence failure is logged, never aborts the pass (the
            # in-proc store still moved so we don't reprocess; the next
            # tick re-attempts the durable write).
            self._cursor_store[leader.lower()] = ts
            if self._cursor_writer is not None:
                try:
                    await self._cursor_writer(leader, ts)
                except Exception as exc:  # noqa: BLE001 - best-effort persist
                    logger.warning(
                        "reconcile_cursor_persist_failed_continuing",
                        extra={"leader": leader, "error": str(exc)},
                    )

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
        # C4b (R6): one bad backfilled trade must NOT abort the whole
        # ``run_once`` pass — mirror the detection drain's contract
        # (count the error, log, keep draining) instead of only a bare
        # ``finally: task_done()``.
        while not self._queue.empty():
            trade = self._queue.get_nowait()
            try:
                await self.process_detected(trade)
            except Exception as exc:  # noqa: BLE001 - count, never abort pass
                self.stats.errors += 1
                logger.warning(
                    "reconcile_process_detected_failed_continuing",
                    extra={"error": str(exc)},
                )
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

    async def _write_heartbeat(self, *, phase: str) -> None:
        """P1-5: write the ``trader_heartbeat`` liveness row (best-effort).

        Records a UTC timestamp + last-poll info so ``polyecho trader
        status`` and the Dashboard (PRD §7.4/§2.3) can show UP/DOWN and
        when the engine last polled.  Always updates the in-proc mirror
        (so liveness is measurable DB-free in tests); the injected
        ``heartbeat_writer`` upserts the ``settings`` row in the live
        wiring.  A write failure is logged and swallowed — the heartbeat
        must NEVER crash or stall the trading loop.
        """
        info: dict[str, Any] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "phase": phase,
            "datasource": self._settings.datasource,
            "live_trading": is_live_enabled(),
            "detected": self.stats.detected,
            "copied": self.stats.copied,
            "skipped": self.stats.skipped,
            "placed": self.stats.placed,
            "followed_leaders": sorted(
                {f.leader_address.lower() for f in self._follows}
            ),
        }
        self.last_heartbeat = info
        logger.info("trader_heartbeat", extra=info)
        if self._heartbeat_writer is not None:
            try:
                await self._heartbeat_writer(info)
            except Exception as exc:  # noqa: BLE001 - best-effort liveness
                logger.warning(
                    "trader_heartbeat_write_failed_continuing",
                    extra={"phase": phase, "error": str(exc)},
                )

    async def _emit_bot_restart_once(self) -> None:
        """Emit SYSTEM_BOT_RESTART exactly once per process start (INFO)."""
        if self._bot_restart_emitted:
            return
        self._bot_restart_emitted = True
        await self._emit_push_event(
            event_type=EventType.SYSTEM_BOT_RESTART,
            severity=Severity.INFO,
            payload={
                "datasource": self._settings.datasource,
                "live_trading": is_live_enabled(),
            },
            push=PushPayload(
                title="跟單引擎已啟動",
                body="trader-core 已啟動並開始監控領單者",
                url="/dashboard",
                tag="system_bot_restart",
            ),
        )

    # ------------------------------------------------------------------
    # SYSTEM_API_FAILURE / SYSTEM_LOW_BALANCE (PRD §5.8 health pushes)
    # ------------------------------------------------------------------

    async def _note_api_outcome(
        self,
        *,
        call_site: str,
        ok: bool,
        error: str = "",
        leader: str | None = None,
        condition_id: str | None = None,
    ) -> None:
        """Edge-triggered SYSTEM_API_FAILURE for a PolymarketClient call.

        ``call_site`` is the logical Polymarket call that failed/succeeded
        (e.g. ``"orderbook"``, ``"risk_context"``, ``"order_place"``).  We
        emit WARNING **once on the transition into a failing state** per
        call-site and clear the flag on the next success, so a Polymarket
        API that is flapping inside the hot copy loop alerts once per
        outage instead of once per tick (debounce / coalesce).  Best-effort
        via :func:`safe_emit` — a NOTIFY/DB hiccup never stalls the loop.
        """
        if ok:
            self._api_failing.discard(call_site)
            return
        if call_site in self._api_failing:
            return  # already alerted for this ongoing outage — coalesce
        self._api_failing.add(call_site)
        await self._emit_push_event(
            event_type=EventType.SYSTEM_API_FAILURE,
            severity=Severity.WARNING,
            payload={
                "call_site": call_site,
                "error": error,
                "leader": leader,
                "condition_id": condition_id,
            },
            push=PushPayload(
                title="API 異常",
                body=(
                    f"Polymarket {call_site} 連線異常，已暫停受影響的跟單，"
                    f"系統將自動重試"
                ),
                url="/alerts",
                tag="system_api_failure",
                data={"call_site": call_site},
            ),
        )

    async def _check_low_balance(self, account_id: uuid.UUID) -> None:
        """Edge-triggered SYSTEM_LOW_BALANCE for a funder USDC balance.

        Probes the optional injected :attr:`_balance_for` (no-op when not
        wired, so fixture/``--once`` stay network-free).  Emits WARNING
        **once on the downward crossing** below
        :data:`LOW_BALANCE_THRESHOLD_USD` and re-arms only after the
        balance recovers to ≥ threshold — never once-per-poll.  Best-effort
        (any probe error is swallowed; the alert fails open, the money
        lock is independent).
        """
        if self._balance_for is None:
            return
        acct = str(account_id)
        try:
            balance = await self._balance_for(account_id)
        except Exception as exc:  # noqa: BLE001 - best-effort: alert only
            logger.warning(
                "low_balance_probe_failed_continuing",
                extra={"account_id": acct, "error": str(exc)},
            )
            return
        if balance is None:
            return  # balance unknown ⇒ never alarm
        if balance >= LOW_BALANCE_THRESHOLD_USD:
            self._low_balance_alerted.discard(acct)  # recovered ⇒ re-arm
            return
        if acct in self._low_balance_alerted:
            return  # already alerted on this downward crossing
        self._low_balance_alerted.add(acct)
        await self._emit_push_event(
            event_type=EventType.SYSTEM_LOW_BALANCE,
            severity=Severity.WARNING,
            payload={
                "account_id": acct,
                "balance_usd": balance,
                "threshold": LOW_BALANCE_THRESHOLD_USD,
            },
            push=PushPayload(
                title="餘額不足",
                body=(
                    f"帳戶 USDC 餘額 ${balance:.2f} 已低於 "
                    f"${LOW_BALANCE_THRESHOLD_USD:.2f}，可能無法繼續跟單，"
                    f"請儘速充值"
                ),
                url="/accounts",
                tag=f"low_balance:{acct}",
                data={"account_id": acct},
            ),
        )

    async def pending_event_hook(self, kind: str, payload: dict[str, Any]) -> None:
        """Bridge :class:`PendingActionExecutor` closes to push events.

        Wire this as the executor's ``event_hook`` so a confirmed
        MANUAL_SELL / EMERGENCY_LIQUIDATE close emits POSITION_CLOSED
        (INFO) or STOP_LOSS (WARNING) just like a copy-trade exit.
        """
        outcome = str(payload.get("outcome") or payload.get("condition_id") or "")
        realised = payload.get("realised_pnl_usd", "0")
        acct = payload.get("account_id", "")
        if kind == "stop_loss":
            await self._emit_push_event(
                event_type=EventType.STOP_LOSS,
                severity=Severity.WARNING,
                payload=payload,
                push=PushPayload(
                    title="停損出場（手動指令）",
                    body=f"市場 {outcome} 已平倉，已實現損益 ${realised}",
                    url="/trades",
                    tag=f"position:{acct}:{outcome}",
                    data={"action_id": payload.get("action_id")},
                ),
            )
        else:
            await self._emit_push_event(
                event_type=EventType.POSITION_CLOSED,
                severity=Severity.INFO,
                payload=payload,
                push=PushPayload(
                    title="部位已平倉（手動指令）",
                    body=f"市場 {outcome} 已平倉，已實現損益 ${realised}",
                    url="/trades",
                    tag=f"position:{acct}:{outcome}",
                    data={"action_id": payload.get("action_id")},
                ),
            )

    async def _pending_actions_once(self) -> int:
        """Poll + execute web-confirmed danger-tier actions (PRD §5.7).

        Delegates to the injected :class:`PendingActionExecutor` whose
        order placement routes through the shared :class:`OrderExecutor`
        chokepoint (``assert_live_allowed``) — default-OFF places nothing.
        On each close it emits POSITION_CLOSED / STOP_LOSS as appropriate.
        """
        if self._pending_executor is None:
            return 0
        processed = await self._pending_executor.poll_once()
        for action_id, _results in processed:
            self.events.append(("pending_action_processed", {"action_id": action_id}))
        return len(processed)

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

"""
polyecho.core.trader
====================
Trader-core copy-trade engine — the Phase-4 Wave-2 live money path
(PRD §4.2/§5.5, PolyHermes brief §3/§4/§7/§9).

Security contract (PRD §4.2, §7.2):

* This process is the ONLY one that holds private keys.
* It opens NO network ports; control flows through Postgres.
* Logs never contain private keys / API secrets / signatures.

Automated copy-trade flows dispatch directly to ``PolymarketClient`` /
``BaseRedeemClient`` once a risk-PASSED decision is produced.  Per-op
operator confirmation lives at the web layer (``dangerous-action`` +
account-import passphrase); the trader-core engine is not behind a
global trading lock.

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
)
from polyecho.shared.datasource import get_datasource
from polyecho.shared.datasource.protocol import DataSource
from polyecho.shared.polymarket.client import GeoRestrictionError
from polyecho.shared.risk_mode import RiskMode, apply_conservative, get_risk_mode

logger = logging.getLogger(__name__)

#: Default cadence (seconds) for the supervised-loop integration probe
#: (Task 2).  A *safe* default: long enough that the probe is negligible
#: load and can never become a hot path, short enough that the
#: integration-status row the Dashboard reads stays fresh.  Tunable via
#: the ``integration_probe_interval_seconds`` ctor arg.
INTEGRATION_PROBE_INTERVAL_SECONDS: float = 90.0

#: Hard upper bound (seconds) on a SINGLE ``run_all_probes`` +
#: ``write_integration_status`` cycle.  A probe that hangs past this is
#: cancelled and the cycle is skipped — the cadence (and, more
#: importantly, the trading loop it runs *beside*) is never delayed by a
#: stuck datasource/DB probe.  Strictly < the cadence so cycles never
#: pile up.
INTEGRATION_PROBE_TIMEOUT_SECONDS: float = 20.0

#: Floor the configured cadence can never go below — keeps a
#: mis-configuration from turning the (fail-soft, off-trade-path) probe
#: into a tight loop.
_MIN_INTEGRATION_PROBE_INTERVAL_SECONDS: float = 5.0

#: Idle keepalive cadence (seconds) for the PARKED fixture/supervised
#: engine (false-stale fix).  After ``run_once`` the fixture engine parks
#: forever (``_park`` == an Event that is never set), so it emits NO
#: further ``trader_heartbeat`` — but ``/status`` flags any worker whose
#: last heartbeat is older than ``HEARTBEAT_STALE_SECONDS`` (300 s) as
#: 逾時/stale.  A healthy parked engine is therefore false-flagged ~5 min
#: after its single startup pass.  The parked loop now re-stamps a
#: ``phase="idle"`` heartbeat every ~90 s (well under the 5-min window,
#: >3× margin) via the SAME fail-soft ``_write_heartbeat`` seam.  This is
#: the FIX; the stale threshold itself is deliberately left unchanged.
#: It is OFF the order/redeem path, never touches the live-lock, and only
#: runs in the parked/idle path (live WS/RPC mode is unaffected).
IDLE_KEEPALIVE_INTERVAL_SECONDS: float = 90.0

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
#: C5 ``async (paused: bool) -> None`` — write the ``global_paused``
#: settings row (the documented soft-pause kill-switch).  Used by the
#: geo-restriction emergency-stop: a CLOB region-block auto-trips this so
#: ``global_paused=true`` exactly as the R24 operator runbook would set it
#: by hand.  The live wiring upserts the ``settings`` table; the default
#: keeps an in-proc mirror so the halt is observable DB-free in tests.
GlobalPauseWriter = Callable[[bool], Awaitable[None]]


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
        global_pause_writer: GlobalPauseWriter | None = None,
        integration_probe_interval_seconds: float = (INTEGRATION_PROBE_INTERVAL_SECONDS),
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

        # Task 2: periodic integration probe (supervised loop ONLY).  This
        # runs `run_all_probes` + `write_integration_status` on a slow
        # cadence so the Dashboard's integration-status row stays fresh.
        # It is STRICTLY off the order/redeem path and is spawned as a
        # detached background task (not awaited by the trade loop).  ``asyncio`` keeps only a
        # *weak* reference to a created task, so we hold a strong ref here
        # to stop it being GC'd mid-flight (context7 /python/cpython —
        # "Task Reference Management").  Sub-floor / non-finite configs are
        # clamped so a mis-config can never make this (fail-soft, no-op-on-
        # error) probe a tight loop.
        try:
            _ivl = float(integration_probe_interval_seconds)
            if _ivl != _ivl or _ivl <= 0:  # NaN or non-positive
                _ivl = INTEGRATION_PROBE_INTERVAL_SECONDS
        except (TypeError, ValueError):
            _ivl = INTEGRATION_PROBE_INTERVAL_SECONDS
        self._integration_probe_interval_s: float = max(
            _MIN_INTEGRATION_PROBE_INTERVAL_SECONDS, _ivl
        )
        self._integration_probe_task: asyncio.Task[None] | None = None
        #: Observability-only: count of completed integration-status
        #: writes.  Inspectable by tests; NEVER read by the exit-code
        #: contract (``degenerate_once_pass`` ignores it entirely).
        self.integration_probe_runs: int = 0

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
        # through the same OrderExecutor).  Optional / injected so the
        # engine stays DB-free in tests.
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

        # --- C5 geo-restriction emergency-stop --------------------------
        # When a CLOB call is rejected because trading is geo-restricted in
        # this region, the engine trips the documented soft-pause: it sets
        # ``global_paused=true`` (via the injected writer), emits a
        # CRITICAL event, and stops submitting any further order for the
        # rest of the pass.  ``_geo_halted`` is the in-proc latch so the
        # halt + event fire exactly once (it stays latched for the process
        # lifetime — a geo-block does not self-heal; an operator must lift
        # ``global_paused`` after resolving the region issue).  The default
        # writer is an in-proc mirror so the halt is observable DB-free.
        self._global_pause_writer = global_pause_writer
        self._geo_halted = False

    # ------------------------------------------------------------------
    # Evaluator / risk hooks
    # ------------------------------------------------------------------

    def _template_for(self, account_id: uuid.UUID, leader: str) -> TemplateConfig | None:
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
            is_following=self._is_following(evaluated.account_id, evaluated.leader_address),
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

    async def _record_skip(self, evaluated: EvaluatedCopy, reason: SkipReason, detail: str) -> None:
        self.skips.append((reason.value, detail))
        self.stats.skipped += 1
        self.stats.skip_reasons[reason.value] = self.stats.skip_reasons.get(reason.value, 0) + 1
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

    async def _record_trade(self, evaluated: EvaluatedCopy, info: dict[str, Any]) -> None:
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
                body=(f"領單者 {leader[:10]}… 市場 {outcome} {side_zh} {size} 股 @ {price}"),
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
                    body=(f"市場 {outcome} 已停損平倉，已實現損益 ${realised}"),
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
                    body=(f"市場 {outcome} 已停利平倉，已實現損益 ${realised}"),
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
                body=(f"市場 {outcome} 部位已平倉，已實現損益 ${realised}"),
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
                        f"已觸發每日虧損上限，已暫停跟單。"
                        f"請於儀表板檢視 PnL 後再決定是否復跟"
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
                        f"領單者 {spec.leader_address[:10]}… 近 7 日策略已偏離"
                        f"原追蹤輪廓，已暫停跟單。請於探索頁重新評估"
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
                await self._process_follows(trade, leader, tx_hash, condition_id)
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
        # C5: once a CLOB geo-restriction has tripped the emergency-stop,
        # NO further order is submitted for the rest of the pass — every
        # later follow would hit the same region-block.  Short-circuit to
        # a skip (the in-proc latch is set the instant the first geo error
        # is seen, so this is a no-op until then).
        if self._geo_halted:
            self.stats.skipped += 1
            self.skips.append((SkipReason.LIVE_LOCKED.value, "geo-restriction emergency stop"))
            self.stats.skip_reasons[SkipReason.LIVE_LOCKED.value] = (
                self.stats.skip_reasons.get(SkipReason.LIVE_LOCKED.value, 0) + 1
            )
            logger.info(
                "copy_skip_geo_halted",
                extra={"leader": spec.leader_address, "tx_hash": tx_hash},
            )
            return

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
                "orderbook fetch failed" in detail or "risk context fetch failed" in detail
            ):
                call_site = "orderbook" if "orderbook fetch failed" in detail else "risk_context"
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
        #
        # C5: a CLOB geo-restriction is re-raised from PolymarketClient as
        # GeoRestrictionError and propagated UNCAUGHT through the executor
        # (it is NOT a transient failure to retry).  Catch it HERE: trip
        # the documented emergency-stop (global_paused=true + CRITICAL
        # event) and record the skip — do NOT place the order.  The
        # ``_geo_halted`` latch then short-circuits every later follow.
        try:
            result = await self._executor.execute(evaluated, size_override=risk.adjusted_size)
        except GeoRestrictionError as exc:
            await self._trigger_geo_halt(spec=spec, tx_hash=tx_hash, error=str(exc))
            await self._record_skip(
                evaluated,
                SkipReason.LIVE_LOCKED,
                f"geo-restriction emergency stop: {exc}",
            )
            return

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
                    # A3: durable copy-attempt outcome → leader_trades.status.
                    "status": "success",
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
            # Order was not placed (retries exhausted / non-COPY) — record
            # the skip reason from the executor.  No ledger mutation since
            # only real fills (``result.placed`` branch above) touch the
            # shared FifoLedger.
            await self._record_skip(
                evaluated,
                result.skip_reason or SkipReason.DATA_UNAVAILABLE,
                result.detail or "order not placed",
            )
            # A3 (PolyHermes deep-dive): a copy attempt that did NOT place
            # must still leave a durable ``leader_trades`` row with
            # ``status="failed"`` (PolyHermes' processed_trade.status=FAILED
            # slot).  Without it a WS/poll redelivery of this same
            # known-bad tx has no durable "do not retry" marker once the
            # in-proc LRU has evicted it — the recorder is idempotent on
            # ``(leader_address, tx_hash)`` so this never double-inserts.
            await self._record_trade(
                evaluated,
                {
                    "tx_hash": tx_hash,
                    "side": side.value if side else None,
                    "size": str(size),
                    "price": str(price),
                    "fill_status": "rejected",
                    "status": "failed",
                },
            )

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run_once(self) -> _Stats:
        """Single dry-run fixture pass (the ``--once`` / CI-smoke path).

        Runs both detection producers once into the queue, drains it
        through the full pipeline, then a single reconcile + redeem pass.
        Returns the per-run :class:`_Stats`.
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
                round(self.stats.copy_latency_ms_total / _samples, 3) if _samples > 0 else None
            )
            _max_ms = round(self.stats.copy_latency_ms_max, 3) if _samples > 0 else None
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
        produced *skips* (e.g. legitimate risk rejects / filter
        rejections) — is a HEALTHY pass: return ``None`` (exit 0).
        Skips are expected and must NOT be treated as failure.
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
        """P1-5 / Task 1: write the ``trader_heartbeat`` liveness row.

        Records a UTC timestamp + last-poll info so ``polyecho trader
        status`` and the Dashboard (PRD §7.4/§2.3) can show UP/DOWN and
        when the engine last polled.

        Task 1 migration: the canonical row is now written through the
        FROZEN shared helper :func:`polyecho.shared.health.write_heartbeat`
        (``kind="trader"``) so the persisted shape is exactly
        ``{ts, phase, datasource, counters{}}`` (counters nested).  This
        is kept **semantically identical** to the legacy per-tick/startup
        heartbeat — same trigger points, same numbers.

        Back-compat is preserved without a flag-day: the F0 reader already
        folds core's legacy *top-level* counters into ``counters{}``, so
        the in-proc mirror (:attr:`last_heartbeat`) and the injected
        ``heartbeat_writer`` hook continue to receive the legacy-shaped
        ``info`` dict verbatim (existing ``settings`` upsert + tests
        unchanged); the canonical write is an additive best-effort sibling.

        Every leg is best-effort and time-/exception-bounded — a heartbeat
        write (legacy hook OR the canonical shared helper, including the
        shared-import itself) must NEVER crash or stall the trading loop,
        and it never touches the order/redeem path.
        """
        ts = datetime.now(tz=UTC).isoformat()
        # Canonical counters block (the nested F0 shape).
        counters: dict[str, Any] = {
            "detected": self.stats.detected,
            "copied": self.stats.copied,
            "skipped": self.stats.skipped,
            "placed": self.stats.placed,
            "followed_leaders": sorted({f.leader_address.lower() for f in self._follows}),
        }
        # Legacy-shaped mirror/hook payload (counters ALSO top-level) — the
        # F0 reader folds these, so we don't break the existing row
        # mid-migration; existing tests/cwire upsert see the same dict.
        info: dict[str, Any] = {
            "ts": ts,
            "phase": phase,
            "datasource": self._settings.datasource,
            **counters,
        }
        self.last_heartbeat = info
        logger.info("trader_heartbeat", extra=info)

        # Task 1: canonical write via the FROZEN shared helper, through the
        # ONE shared fail-soft seam ``health_emit.emit_heartbeat_safe``
        # (lazy F0 import + sync/coroutine tolerance + swallow-all-but-
        # CancelledError).  The seam is itself imported lazily + guarded:
        # F0 / the seam live in ``packages/shared`` (outside this worktree)
        # so in an isolated base the import is red — which MUST degrade to
        # "legacy-only heartbeat", never a loop crash.  The
        # ``trader_heartbeat_canonical_write_failed_continuing`` event is
        # kept verbatim (via the seam's ``on_error`` hook) so existing
        # behaviour/tests are unchanged.
        try:
            from polyecho.shared.health_emit import emit_heartbeat_safe

            # F0's write_heartbeat is keyword-only after ``kind`` and stamps
            # ``ts`` itself — do NOT pass ``ts`` (the legacy ``info`` mirror
            # above still records its own ``ts`` for back-compat).
            await emit_heartbeat_safe(
                "trader",
                phase=phase,
                datasource=self._settings.datasource,
                counters=counters,
                on_error=lambda exc: logger.warning(
                    "trader_heartbeat_canonical_write_failed_continuing",
                    extra={"phase": phase, "error": str(exc)},
                ),
            )
        except Exception as exc:  # noqa: BLE001 - best-effort liveness
            # Final backstop: the seam is fail-soft, but a failure importing
            # the seam itself must still degrade to legacy-only — same event
            # name as before for log-assertion parity.
            logger.warning(
                "trader_heartbeat_canonical_write_failed_continuing",
                extra={"phase": phase, "error": str(exc)},
            )

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
                body=(f"Polymarket {call_site} 連線異常，已暫停受影響的跟單，系統將自動重試"),
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

    # ------------------------------------------------------------------
    # C5: geo-restriction emergency-stop
    # ------------------------------------------------------------------

    async def _trigger_geo_halt(
        self,
        *,
        spec: FollowSpec,
        tx_hash: str,
        error: str,
    ) -> None:
        """Trip the documented emergency-stop on a CLOB geo-restriction.

        C5 (PolyHermes deep-dive): Polymarket geo-restricts trading.  When
        a CLOB order is rejected because trading is blocked in this region
        the engine MUST NOT just log it and keep going — every later
        submission would fail the same way.  This is FAIL-SAFE: stopping
        is always safe.  So we auto-trip the SAME posture the R24 operator
        runbook applies by hand:

        * set ``settings.global_paused = true`` (the documented soft-pause
          kill-switch the web /settings page also writes), and
        * emit a CRITICAL :class:`EventType.SYSTEM_API_FAILURE` event so
          ``/alerts`` + Web Push surface it loudly.

        Latched via :attr:`_geo_halted` so the halt + event fire exactly
        once even if many follows hit the block in the same pass.  Every
        leg is best-effort (``global_paused`` write + event emit are both
        guarded) so the halt itself can never crash the loop — but the
        latch is set FIRST so a writer hiccup still stops new submissions.
        """
        if self._geo_halted:
            return
        self._geo_halted = True
        logger.error(
            "geo_restriction_emergency_stop",
            extra={
                "leader": spec.leader_address,
                "tx_hash": tx_hash,
                "error": error,
            },
        )
        # Set global_paused=true — best-effort; the in-proc latch above
        # already stops new submissions even if this write fails.
        if self._global_pause_writer is not None:
            try:
                await self._global_pause_writer(True)
            except Exception as exc:  # noqa: BLE001 - best-effort halt
                logger.warning(
                    "geo_restriction_global_pause_write_failed_continuing",
                    extra={"error": str(exc)},
                )
        await self._emit_push_event(
            event_type=EventType.SYSTEM_API_FAILURE,
            severity=Severity.CRITICAL,
            payload={
                "call_site": "order_place",
                "reason": "geo_restriction",
                "error": error,
                "leader": spec.leader_address,
                "tx_hash": tx_hash,
                "global_paused": True,
            },
            push=PushPayload(
                title="交易區域受限 · 已緊急停止",
                body=(
                    "Polymarket 回報此區域禁止交易，系統已自動全域暫停跟單。"
                    "請於設定頁確認區域問題後再恢復"
                ),
                url="/alerts",
                tag="geo_restriction_halt",
                data={"reason": "geo_restriction"},
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
        order placement routes through the shared :class:`OrderExecutor`.
        Web operators have already confirmed each pending action via
        ``dangerous-action`` (CONFIRM_PHRASE + danger passphrase) before
        the worker picks it up.  On each close it emits POSITION_CLOSED /
        STOP_LOSS as appropriate.
        """
        if self._pending_executor is None:
            return 0
        processed = await self._pending_executor.poll_once()
        for action_id, _results in processed:
            self.events.append(("pending_action_processed", {"action_id": action_id}))
        return len(processed)

    # ------------------------------------------------------------------
    # Task 2: periodic integration probe (supervised loop ONLY)
    # ------------------------------------------------------------------

    async def _run_integration_probe_once(self) -> None:
        """One ``run_all_probes`` → ``write_integration_status`` cycle.

        STRICTLY fail-soft and time-bounded (the two reasons this can
        never harm the engine):

        * **Time-bounded** — the whole probe+write is wrapped in
          :func:`asyncio.timeout` (:data:`INTEGRATION_PROBE_TIMEOUT_SECONDS`,
          strictly < the cadence).  A datasource/DB probe that hangs is
          cancelled; the cycle is skipped.  Because this coroutine runs in
          its OWN detached task (never awaited by the copy-trade loop), a
          slow probe delays only the *next probe*, never a single tick of
          detection / evaluate / risk / execute / redeem.
        * **Exception-bounded** — every failure (including the lazy F0
          import being red in an isolated base, or a probe raising) is
          caught and swallowed; only ``CancelledError`` propagates so a
          real shutdown still tears the task down promptly.

        It calls the FROZEN shared
        :func:`polyecho.shared.health.run_all_probes` /
        :func:`~polyecho.shared.health.write_integration_status` and, on
        top of the frozen snapshot, merges the NON-frozen bounded
        fail-soft WS reachability probes (:mod:`polyecho.core.ws_probe`)
        under the ``polymarket_ws_user`` / ``polymarket_ws_market`` keys
        — the frozen health contract has no WS probe and the dashboard
        renders ``integration.probes`` generically.  It still touches no
        engine state, no ``FifoLedger``, no order/redeem path (it is
        read → write-status only).
        """
        try:
            async with asyncio.timeout(INTEGRATION_PROBE_TIMEOUT_SECONDS):
                from polyecho.core.ws_probe import (
                    probe_polymarket_ws_market,
                    probe_polymarket_ws_user,
                )
                from polyecho.shared.health import (
                    run_all_probes,
                    write_integration_status,
                )

                snapshot = await run_all_probes()
                # Merge the non-frozen WS reachability probes into the
                # frozen snapshot before persisting.  Each is bounded +
                # fail-soft (returns a ProbeResult, never raises), so the
                # sweep's contract is unchanged.
                snapshot["polymarket_ws_user"] = await probe_polymarket_ws_user()
                snapshot["polymarket_ws_market"] = await probe_polymarket_ws_market()
                await write_integration_status(snapshot)
            self.integration_probe_runs += 1
            logger.info(
                "integration_probe_written",
                extra={"runs": self.integration_probe_runs},
            )
        except asyncio.CancelledError:
            raise  # cooperative shutdown — never swallow cancellation
        except BaseException as exc:  # noqa: BLE001 - probe is fail-soft
            # A slow/failing/absent probe must NEVER surface anywhere near
            # the trading loop — log a breadcrumb and move on.
            logger.warning(
                "integration_probe_failed_continuing",
                extra={"error": str(exc)},
            )

    async def _integration_probe_loop(self) -> None:
        """Perpetual cadence driving :meth:`_run_integration_probe_once`.

        A genuine never-exit ``while True`` (mirrors the wiring redeem /
        daily-reconcile cadences): a stray fault in one cycle only waits
        the interval and re-enters; only a real shutdown
        (Cancelled/KeyboardInterrupt/SystemExit) propagates.  This loop
        lives in its own detached task, so its sleeps/awaits are entirely
        independent of — and can never back-pressure — the trade path.
        """
        while True:
            await self._run_integration_probe_once()
            try:
                await asyncio.sleep(self._integration_probe_interval_s)
            except asyncio.CancelledError:
                raise

    async def run(self) -> None:
        """Supervised loop: bounded pass + parked detection/probe TaskGroup.

        Fixture mode performs one bounded ``run_once`` pass then idles
        (restart-safe; reconcile is the durable backfill).  The live WS /
        RPC streams are the Phase-4 W2 deliverable behind the same
        interface.

        Task 2: the periodic integration probe is attached HERE (the
        always-on supervised engine) as a detached sibling task — it runs
        ``run_all_probes`` + ``write_integration_status`` on a slow
        cadence concurrently with the parked loop.  It is NOT awaited on
        the trade path and cannot delay or break it (see
        :meth:`_run_integration_probe_once`).  ``--once`` does NOT reach
        ``run()`` (``__main__`` calls ``run_once()`` directly), so the
        probe + its scheduler never run under ``--once`` and the
        exit-code contract is wholly untouched.
        """
        await self.run_once()
        # Idle until cancelled by the supervising TaskGroup / signal — an
        # Event that is never set is the lint-clean "park forever" idiom
        # (no busy sleep loop).  Live WS/RPC stream tasks attach here in
        # the Phase-4 W2 live build behind the same interface.
        async with asyncio.TaskGroup() as tg:  # pragma: no cover - supervised loop
            tg.create_task(self._park())
            # Strong ref (asyncio holds only a WEAK ref to a created
            # task — context7 /python/cpython "Task Reference
            # Management") so the probe cadence can't be GC'd mid-flight.
            # The loop is a true never-exit ``while True`` that re-raises
            # only cancellation, so it can never trip the TaskGroup's
            # "one child raises ⇒ cancel siblings" rule and abort the
            # parked engine.
            self._integration_probe_task = tg.create_task(self._integration_probe_loop())

    async def _park(self) -> None:
        """Idle the parked fixture engine while keeping liveness fresh.

        False-stale fix.  The previous body was a bare
        ``await asyncio.Event().wait()`` — it parked forever and emitted NO
        further heartbeat, so a perfectly-healthy fixture engine was
        false-flagged 逾時/stale by ``/status`` ~5 min after its single
        startup ``run_once`` pass (the only heartbeats were that pass's
        ``cycle_start`` / ``cycle_complete``).

        Instead of an inert wait, the parked engine now re-stamps a
        ``phase="idle"`` heartbeat every
        :data:`IDLE_KEEPALIVE_INTERVAL_SECONDS` via the SAME
        :meth:`_write_heartbeat` seam the cycle heartbeats use (canonical
        FROZEN-shared write + legacy in-proc mirror/hook, all best-effort).
        The reported counters are simply the last cycle's tallies (the
        engine is idle, so they are correct/unchanged) — ``_write_heartbeat``
        always reads ``self.stats``.

        Contract preservation:

        * **Order/redeem path untouched** — this only writes a liveness
          row; it does not touch the ledger, queue, or any SDK.  Live
          WS/RPC mode does not reach here (live stream tasks attach in
          ``run`` instead).
        * **Fail-soft** — :meth:`_write_heartbeat` swallows every heartbeat
          failure (including an absent shared package) and only lets
          :class:`asyncio.CancelledError` propagate, so a heartbeat problem
          can never abort the parked engine.
        * **TaskGroup-safe** — like :meth:`_integration_probe_loop`, this is
          a true never-exit ``while True`` that re-raises ONLY
          ``CancelledError``; it can never trip the supervising
          ``TaskGroup``'s "one child raises ⇒ cancel siblings" rule and
          tear the engine down.  A cooperative shutdown cancels it cleanly.
        """
        while True:  # pragma: no cover - supervised loop
            try:
                await asyncio.sleep(IDLE_KEEPALIVE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise  # cooperative shutdown — never swallow cancellation
            # Best-effort liveness re-stamp (same fail-soft seam as the
            # cycle heartbeats); cannot raise except CancelledError.
            await self._write_heartbeat(phase="idle")

"""
polyecho.core.pending
=====================
Confirmed-pending-action executor (PRD §5.7, Phase-5 Wave-2).

The Dashboard owns *creating* and *passphrase-verifying* danger-tier
operations (manual sell / emergency liquidate) — see the FROZEN
:mod:`polyecho.shared.pending_actions` contract.  This worker is the
**core-side consumer**: it polls for rows the web layer has already
flipped to ``confirmed`` and *executes* them.

HARD invariants preserved
-------------------------
* It **never re-verifies the passphrase** — that is web's job; core only
  consumes ``status == 'confirmed'`` rows.
* **Defense-in-depth (P0-2)**: an EARLIER fail-closed gate refuses any
  confirmed row when the operator has not provisioned the danger
  passphrase (``is_danger_passphrase_provisioned() is False`` ⇒ no
  confirmed row can be legitimate) or when the row is past its
  ``expires_at`` TTL.  Refused rows are logged once and **not** marked
  processed (left for an operator to investigate).  This is an
  *additional* gate, never a replacement for the live-lock chokepoint.
* Every order placement routes through the existing
  :class:`~polyecho.core.execution.OrderExecutor`, whose live path calls
  :class:`~polyecho.shared.polymarket.client.PolymarketClient.place_order`
  → ``assert_live_allowed`` (the single Phase-4 chokepoint).  With the
  HARD live lock OFF (default) it logs the intended order and places
  **nothing** — identical to the copy-trade dry-run path.
* **Idempotent**: a processed row is marked terminal via a
  ``params['_core_processed_at']`` JSON marker (no new
  :class:`PendingActionStatus` enum value is invented — the contract's
  enum is frozen) *and* tracked in-process, so a row is never executed
  twice even across overlapping polls.

The DB I/O is injected (``rows_source`` / ``mark_processed``) so the
whole executor unit-tests with no DB, mirroring the rest of the engine.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polyecho.core.execution import ExecutionResult, FifoLedger, OrderExecutor
from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    TradeSide,
    WalletBinding,
)
from polyecho.shared.danger_passphrase import is_danger_passphrase_provisioned
from polyecho.shared.pending_actions import PendingActionStatus, PendingActionType

logger = logging.getLogger(__name__)

__all__ = ["OpenPosition", "PendingActionExecutor", "PendingActionRow"]

_ZERO = Decimal(0)

#: JSON marker key written into ``pending_actions.params`` after a row is
#: executed.  We deliberately do NOT add a new ``PendingActionStatus``
#: value (that enum is frozen); a params marker + the in-proc guard make
#: the executor idempotent without touching the contract.
PROCESSED_MARKER: str = "_core_processed_at"


@dataclass(frozen=True)
class OpenPosition:
    """One open position for an account (the liquidation/sell unit).

    Built by the engine from the live FIFO ledger / positions table; kept
    minimal so the executor stays DB-free and unit-testable.
    """

    account_id: uuid.UUID
    condition_id: str
    token_id: str
    outcome: str
    size: Decimal
    price: Decimal
    binding: WalletBinding


@dataclass(frozen=True)
class PendingActionRow:
    """A confirmed ``pending_actions`` row this worker may execute.

    Mirrors the columns we read; ``params`` is the opaque jsonb the web
    layer wrote (e.g. ``{"account_id": ..., "token_id": ..., "size": ...}``
    for MANUAL_SELL).  ``processed`` is ``True`` once
    :data:`PROCESSED_MARKER` is present in ``params``.
    """

    id: str
    action_type: PendingActionType
    status: str
    params: dict[str, Any] = field(default_factory=dict)
    #: TTL bound the web layer recorded (``created_at + ttl_seconds``).
    #: ``None`` ⇒ unknown/unbounded (treated as *not* expired so we never
    #: spuriously reject a row whose source didn't surface the column).
    expires_at: datetime | None = None

    @property
    def processed(self) -> bool:
        return bool(self.params.get(PROCESSED_MARKER))

    def is_expired(self, *, now: datetime | None = None) -> bool:
        """``True`` iff a TTL is known and ``now >= expires_at``.

        Naive datetimes are interpreted as UTC (mirrors the FROZEN
        :mod:`polyecho.shared.pending_actions` ``expires_at`` handling).
        """
        if self.expires_at is None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return (now or datetime.now(tz=UTC)) >= exp


# Injected DB hooks (kept tiny so the executor is pure / unit-testable).
RowsSource = Callable[[], Awaitable[list[PendingActionRow]]]
MarkProcessed = Callable[[str, dict[str, Any]], Awaitable[None]]
PositionsFor = Callable[[uuid.UUID], Awaitable[list[OpenPosition]]]
# Best-effort event hook: (kind, payload) where kind ∈ {position_closed,
# stop_loss}; the engine wires this to ``safe_emit``.
EventHook = Callable[[str, dict[str, Any]], Awaitable[None]]


class PendingActionExecutor:
    """Execute web-confirmed danger-tier actions through the chokepoint.

    Args:
        rows_source:    ``async () -> [PendingActionRow]`` — confirmed rows
                        (``status == 'confirmed'``) the web layer produced.
                        The executor itself additionally filters defensively.
        positions_for:  ``async (account_id) -> [OpenPosition]`` — open
                        positions for EMERGENCY_LIQUIDATE (and the lookup
                        backing a position-less MANUAL_SELL).
        mark_processed: ``async (action_id, new_params) -> None`` — persist
                        the :data:`PROCESSED_MARKER` so the row is terminal.
        executor:       The shared :class:`OrderExecutor` (live-locked).
        event_hook:     Optional best-effort ``(kind, payload)`` emitter.
    """

    def __init__(
        self,
        *,
        rows_source: RowsSource,
        positions_for: PositionsFor,
        mark_processed: MarkProcessed,
        executor: OrderExecutor | None = None,
        event_hook: EventHook | None = None,
    ) -> None:
        self._rows_source = rows_source
        self._positions_for = positions_for
        self._mark_processed = mark_processed
        self._executor = executor if executor is not None else OrderExecutor(
            ledger=FifoLedger()
        )
        self._event_hook = event_hook
        # In-proc guard so overlapping polls never double-execute a row
        # even before the DB marker round-trips.
        self._seen: set[str] = set()

    @property
    def executor(self) -> OrderExecutor:
        return self._executor

    async def poll_once(self) -> list[tuple[str, list[ExecutionResult]]]:
        """Process every eligible confirmed row exactly once.

        Returns a list of ``(action_id, [ExecutionResult, ...])`` — one
        :class:`ExecutionResult` per order the action placed (a
        MANUAL_SELL yields one; an EMERGENCY_LIQUIDATE yields one per open
        position).  With the live lock OFF every result has
        ``placed=False`` / ``skip_reason=LIVE_LOCKED`` (proof the
        chokepoint gated it).
        """
        try:
            rows = await self._rows_source()
        except Exception as exc:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "pending_rows_fetch_failed_continuing", extra={"error": str(exc)}
            )
            return []

        out: list[tuple[str, list[ExecutionResult]]] = []
        for row in rows:
            # Defensive triple-guard: only confirmed, supported, unprocessed,
            # unseen rows execute (web owns confirm+passphrase; we never
            # re-verify).
            if row.status != str(PendingActionStatus.CONFIRMED):
                continue
            if row.action_type not in (
                PendingActionType.MANUAL_SELL,
                PendingActionType.EMERGENCY_LIQUIDATE,
            ):
                continue
            if row.processed or row.id in self._seen:
                continue

            # --- Defense-in-depth gates (P0-2), strictly EARLIER than the
            # live-lock chokepoint (OrderExecutor.execute → assert_live_
            # allowed).  These ADD an earlier fail-closed refusal; they do
            # NOT replace the HARD live lock, which still gates every SELL.

            # (a) Stale-confirmed guard: a confirmed row past its TTL is
            # not a legitimate execution target.  Fail-closed: refuse,
            # log once (seen-set ⇒ no loop-spam), do NOT mark processed.
            if row.is_expired():
                if row.id not in self._seen:
                    self._seen.add(row.id)
                    logger.warning(
                        "pending_action_refused_expired",
                        extra={
                            "action_id": row.id,
                            "action_type": str(row.action_type),
                            "expires_at": (
                                row.expires_at.isoformat()
                                if row.expires_at is not None
                                else None
                            ),
                        },
                    )
                continue

            # (b) Danger-passphrase provisioning guard: if the operator
            # never provisioned the danger passphrase, NO confirmed row
            # can be legitimate (web's confirm path is fail-closed without
            # it).  A confirmed row appearing here is therefore suspicious
            # — refuse, log a structured WARNING once, do NOT mark
            # processed (leave it for an operator to investigate).  The
            # FROZEN helper is itself fail-safe.
            if not await is_danger_passphrase_provisioned():
                if row.id not in self._seen:
                    self._seen.add(row.id)
                    logger.warning(
                        "pending_action_refused_no_danger_passphrase",
                        extra={
                            "action_id": row.id,
                            "action_type": str(row.action_type),
                        },
                    )
                continue

            # P1-6: do NOT claim the row in ``_seen`` before the awaited
            # execution.  Previously a row whose ``_execute_row`` raised was
            # added to ``_seen`` first and then abandoned for the whole
            # process lifetime (no retry, no DB marker, no idempotency
            # round-trip).  We now execute + finalise FIRST and only mark
            # the row seen on success; on any exception the row is left
            # un-seen so the very next poll retries it.  Idempotency is
            # still guaranteed by the durable ``params`` processed marker
            # (``row.processed`` short-circuits a re-poll once ``_finalise``
            # persisted it) and the in-proc ``_seen`` set added below — and
            # the P0 fail-closed expired / not-provisioned guards above
            # already claimed-and-skipped their rows, untouched here.
            try:
                results = await self._execute_row(row)
            except Exception as exc:  # noqa: BLE001 - isolate, leave for retry
                logger.warning(
                    "pending_action_execute_failed_will_retry",
                    extra={"action_id": row.id, "error": str(exc)},
                )
                continue  # NOT added to _seen ⇒ retried next poll

            await self._finalise(row)
            self._seen.add(row.id)  # success ⇒ claim (idempotent w/ marker)
            out.append((row.id, results))
        return out

    # ------------------------------------------------------------------

    async def _execute_row(self, row: PendingActionRow) -> list[ExecutionResult]:
        if row.action_type is PendingActionType.MANUAL_SELL:
            return [await self._manual_sell(row)]
        return await self._emergency_liquidate(row)

    async def _manual_sell(self, row: PendingActionRow) -> ExecutionResult:
        """Place a SELL for the params-specified position (chokepoint-gated)."""
        p = row.params
        account_id = uuid.UUID(str(p["account_id"]))
        token_id = str(p.get("token_id") or p.get("asset") or "")
        condition_id = str(p.get("condition_id") or "")
        outcome = str(p.get("outcome") or token_id or condition_id)
        size = _dec(p.get("size"))
        price = _dec(p.get("price"))
        binding = await self._resolve_binding(
            account_id, condition_id, token_id, p
        )

        result = await self._place_sell(
            account_id=account_id,
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            size=size,
            price=price,
            binding=binding,
        )
        await self._emit_close(
            row, account_id, condition_id, outcome, size, price, result
        )
        return result

    async def _emergency_liquidate(
        self, row: PendingActionRow
    ) -> list[ExecutionResult]:
        """SELL **all** open positions for the account (chokepoint-gated)."""
        account_id = uuid.UUID(str(row.params["account_id"]))
        positions = await self._positions_for(account_id)
        results: list[ExecutionResult] = []
        for pos in positions:
            result = await self._place_sell(
                account_id=pos.account_id,
                condition_id=pos.condition_id,
                token_id=pos.token_id,
                outcome=pos.outcome,
                size=pos.size,
                price=pos.price,
                binding=pos.binding,
            )
            await self._emit_close(
                row,
                pos.account_id,
                pos.condition_id,
                pos.outcome,
                pos.size,
                pos.price,
                result,
            )
            results.append(result)
        logger.info(
            "emergency_liquidate_processed",
            extra={
                "action_id": row.id,
                "account_id": str(account_id),
                "positions": len(positions),
            },
        )
        return results

    async def _resolve_binding(
        self,
        account_id: uuid.UUID,
        condition_id: str,
        token_id: str,
        params: dict[str, Any],
    ) -> WalletBinding:
        # Prefer a binding already resolved among the account's open
        # positions (so the SELL signs with the correct funder/sig type).
        try:
            for pos in await self._positions_for(account_id):
                if pos.condition_id == condition_id or pos.token_id == token_id:
                    return pos.binding
        except Exception:  # noqa: BLE001 - fall back to a default EOA binding
            logger.warning(
                "binding_lookup_failed_defaulting_eoa",
                extra={"account_id": str(account_id)},
            )
        funder = str(params.get("funder") or "")
        return WalletBinding(
            signature_type=int(params.get("signature_type", 0) or 0),
            funder=funder,
            wallet_mode=str(params.get("wallet_mode") or "eoa"),
            derived=False,
        )

    async def _place_sell(
        self,
        *,
        account_id: uuid.UUID,
        condition_id: str,
        token_id: str,
        outcome: str,
        size: Decimal,
        price: Decimal,
        binding: WalletBinding,
    ) -> ExecutionResult:
        """Build a SELL :class:`EvaluatedCopy` and run it through the
        shared :class:`OrderExecutor` so ``assert_live_allowed`` gates it.
        """
        decision = Decision(
            outcome=DecisionOutcome.COPY,
            side=TradeSide.SELL,
            token_id=token_id or condition_id,
            price=price,
            size=size,
            order_type="FOK",
            detail=f"pending-action SELL {size}@{price} ({outcome})",
        )
        evaluated = EvaluatedCopy(
            leader_address="__pending_action__",
            tx_hash=f"pending:{token_id or condition_id}",
            account_id=account_id,
            condition_id=condition_id,
            binding=binding,
            decision=decision,
            raw={"outcome": outcome, "source": "pending_action"},
        )
        # OrderExecutor.execute() short-circuits on the HARD live lock
        # (default OFF ⇒ places nothing); the live path calls
        # PolymarketClient.place_order → assert_live_allowed.
        return await self._executor.execute(evaluated)

    async def _emit_close(
        self,
        row: PendingActionRow,
        account_id: uuid.UUID,
        condition_id: str,
        outcome: str,
        size: Decimal,
        price: Decimal,
        result: ExecutionResult,
    ) -> None:
        if self._event_hook is None:
            return
        realised = (
            result.sell_match.realised_pnl_usd
            if result.sell_match is not None
            else _ZERO
        )
        # STOP_LOSS when the close realised a loss; otherwise POSITION_CLOSED.
        kind = "stop_loss" if realised < _ZERO else "position_closed"
        await self._event_hook(
            kind,
            {
                "action_id": row.id,
                "action_type": str(row.action_type),
                "account_id": str(account_id),
                "condition_id": condition_id,
                "outcome": outcome,
                "size": str(size),
                "price": str(price),
                "realised_pnl_usd": str(realised),
                "placed": result.placed,
            },
        )

    async def _finalise(self, row: PendingActionRow) -> None:
        """Persist the idempotency marker so the row never re-executes."""
        new_params = dict(row.params)
        new_params[PROCESSED_MARKER] = datetime.now(tz=UTC).isoformat()
        try:
            await self._mark_processed(row.id, new_params)
        except Exception as exc:  # noqa: BLE001 - in-proc guard still holds
            logger.warning(
                "pending_action_mark_failed",
                extra={"action_id": row.id, "error": str(exc)},
            )
        logger.info(
            "pending_action_processed",
            extra={"action_id": row.id, "action_type": str(row.action_type)},
        )


def _dec(value: Any) -> Decimal:
    """Coerce a params value to :class:`Decimal` (``None``/blank → 0)."""
    if value is None or value == "":
        return _ZERO
    return Decimal(str(value))

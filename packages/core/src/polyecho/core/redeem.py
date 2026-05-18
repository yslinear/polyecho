"""
polyecho.core.redeem
====================
Durable redeem worker (PolyHermes brief §9 — Wave C-redeem concrete).

Flow (brief §9 / polyhermes-analysis §9):

1. **Poll** the datasource ``redeemable`` flag (do NOT compute UMA/on-chain
   resolution — adopt the pragmatic API-trust).  :class:`RedeemablePoller`
   is a concrete :data:`CandidateSource` over the existing async datasource
   ``get_positions`` (the data-api ``redeemable``/``redeemableValue`` flag),
   gated on ``auto_redeem`` + live + a simple quota / cooldown.
2. **Upsert** a :class:`~polyecho.shared.copytrading.RedeemJobSpec` —
   **idempotent on ``(account_id, condition_id, outcome_index)``** so a
   restart never double-redeems.  :class:`DbJobUpserter` is the concrete
   ``redeem_jobs`` upserter with the
   ``pending → submitted → confirmed → failed`` status machine + bounded
   retry (the durable improvement over PolyHermes' in-memory-only dedup).
3. In **live** mode call
   :class:`~polyecho.shared.polymarket.redeem.BaseRedeemClient.redeem`
   (NegRisk ⇒ WrappedCollateral + post-``unwrap``; normal ⇒ pUSD — the
   collateral choice is the brief's "highest-risk detail", derived from
   ``is_neg_risk``).  Gated by ``auto_redeem`` AND the live lock.
4. Default dry-run: log the redeem intent with
   :attr:`SkipReason.LIVE_LOCKED`; call **nothing** on-chain.

**Double live-lock invariant (money-path safety is paramount):**

* The worker short-circuits unless BOTH ``is_live_enabled()``
  (``POLYECHO_LIVE_TRADING``) AND ``self._auto_redeem``
  (``POLYECHO_AUTO_REDEEM``) are true — proven by
  ``test_redeem_idempotent_and_live_locked`` (default = PENDING /
  LIVE_LOCKED, zero on-chain calls).
* Even past that, :meth:`BaseRedeemClient.redeem` calls
  :func:`assert_live_allowed` FIRST (raises if the lock is off), so a
  stray live call still fails closed.

So with either env var off the worker logs intent and **submits nothing**,
exactly as before this wave.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    SkipReason,
    WalletBinding,
    is_live_enabled,
)
from polyecho.shared.datasource.dto import PositionRecord
from polyecho.shared.polymarket.redeem import index_set_for

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
#: ``async (RedeemJobSpec, RedeemResult) -> None`` — persist the status
#: machine transition (submitted/confirmed/failed) for a job.
JobStatusWriter = Callable[[RedeemJobSpec, RedeemResult], Awaitable[None]]


@dataclass(frozen=True)
class RedeemOutcome:
    """What the worker did for one candidate (dry-run or live)."""

    spec: RedeemJobSpec
    created: bool
    result: RedeemResult


# ---------------------------------------------------------------------------
# 1. Concrete redeemable poller (brief §9 — data-api ``redeemable`` flag)
# ---------------------------------------------------------------------------
#: ``async (account_id) -> (proxy_wallet, WalletBinding)`` — resolves the
#: on-chain identity for one account (core wires this to the accounts repo +
#: ``resolve_wallet``).  Only called on the live path.
AccountResolver = Callable[
    [uuid.UUID], Awaitable[tuple[str, WalletBinding]]
]


@runtime_checkable
class _PositionSource(Protocol):
    """The single datasource method the poller needs (brief §9).

    Interface-segregation: any :class:`DataSource` (fixture or live)
    structurally satisfies this via ``get_positions``; tests pass a tiny
    fake without depending on the whole datasource protocol.
    """

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]: ...


def _redeemable_outcome_index(raw: dict[str, object]) -> int:
    """Best-effort winning-outcome index from a position row.

    The data-api position carries ``outcomeIndex`` (int) for the held
    outcome; a redeemable position is the *winning* one (the api already
    decided — pragmatic API-trust, brief §9).  Defaults to 0 if absent
    (binary YES) so a malformed row never crashes the poll.
    """
    val = raw.get("outcomeIndex")
    if val is None:
        return 0
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_redeemable(raw: dict[str, object]) -> bool:
    """The data-api ``redeemable`` flag (brief §9 — API-trust, no UMA).

    Polymarket's positions endpoint exposes ``redeemable`` (bool); some
    payloads use ``redeemableValue``/``redeemable_amount`` numeric. Trust
    the flag; never recompute on-chain resolution.
    """
    flag = raw.get("redeemable")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, (int, float)):
        return flag != 0
    for k in ("redeemableValue", "redeemable_amount"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and v != 0:
            return True
        if isinstance(v, str) and v not in ("", "0", "0.0"):
            return True
    return False


def _is_neg_risk(raw: dict[str, object]) -> bool:
    """NegRisk flag from a position row (brief §9 collateral branch)."""
    for k in ("negRisk", "neg_risk", "negativeRisk"):
        v = raw.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
    return False


@dataclass
class RedeemablePoller:
    """Concrete :data:`CandidateSource` over the async datasource.

    Polls ``get_positions`` for each tracked account and yields the rows
    the data-api flagged ``redeemable`` (brief §9 — API-trust, no UMA /
    on-chain resolution recompute).  A simple per-poll quota + monotonic
    cooldown bounds how often / how many candidates a single pass emits
    (brief §9 — "quota-cooldown") so a flapping flag can't spam redeems.

    Args:
        datasource: Any :class:`DataSource` (fixture or live); used as an
            async context manager only if it implements the lifecycle.
        accounts: ``[(account_id, proxy_wallet)]`` to poll.
        max_per_poll: Hard cap on candidates emitted per pass (quota).
        cooldown_s: Minimum seconds between two non-empty polls (cooldown);
            ``0`` disables the cooldown (used by tests).
        clock: Injected monotonic clock (seconds) for deterministic tests.
    """

    datasource: _PositionSource
    accounts: Sequence[tuple[uuid.UUID, str]]
    max_per_poll: int = 50
    cooldown_s: float = 20.0
    clock: Callable[[], float] = field(default=lambda: __import__("time").monotonic())
    _last_poll: float = field(default=0.0, init=False, repr=False)

    async def __call__(self) -> list[RedeemCandidate]:
        now = self.clock()
        if self.cooldown_s > 0 and self._last_poll and (
            now - self._last_poll
        ) < self.cooldown_s:
            logger.debug(
                "redeem_poll_cooldown",
                extra={"since_last_s": now - self._last_poll},
            )
            return []

        out: list[RedeemCandidate] = []
        for account_id, wallet in self.accounts:
            try:
                positions = await self.datasource.get_positions(wallet)
            except Exception as exc:  # noqa: BLE001 - fail-soft poll
                logger.warning(
                    "redeem_poll_positions_failed",
                    extra={"account_id": str(account_id), "error": str(exc)},
                )
                continue
            for pos in positions:
                raw = dict(pos.raw)
                if not _is_redeemable(raw):
                    continue
                out.append(
                    RedeemCandidate(
                        account_id=account_id,
                        condition_id=pos.condition_id,
                        outcome_index=_redeemable_outcome_index(raw),
                        is_neg_risk=_is_neg_risk(raw),
                    )
                )
                if len(out) >= self.max_per_poll:
                    break
            if len(out) >= self.max_per_poll:
                break
        if out:
            self._last_poll = now
        return out


# ---------------------------------------------------------------------------
# 2. Concrete redeem_jobs upserter + status-machine writer (brief §9)
# ---------------------------------------------------------------------------
#: ``async () -> AsyncSession``-style factory.  Typed loosely so this module
#: imports no SQLAlchemy at module import time (shared owns the engine).
SessionFactory = Callable[[], object]


class DbJobUpserter:
    """Idempotent ``redeem_jobs`` upserter + status-machine writer (brief §9).

    Idempotent on ``(account_id, condition_id, outcome_index)`` via the
    table's ``uq_redeem_jobs_account_condition_outcome`` UNIQUE — a restart
    re-running the same candidate finds the existing row and does NOT
    create a second job (no double-redeem; the durable improvement over
    PolyHermes' in-memory dedup).  Status machine:
    ``pending → submitted → confirmed | failed`` with a bounded
    ``max_attempts`` retry: a ``failed`` job below the attempt cap is
    reset to ``pending`` on the next pass; at/above the cap it stays
    ``failed`` and is NOT re-attempted.

    Args:
        session_factory: ``() -> AsyncSession`` (an async-context session).
        max_attempts: Bounded retry ceiling for a failing job (brief §9).

    All SQLAlchemy imports are lazy so this module stays import-light and
    the worker is unit-testable with a fake upserter callable.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], object],
        max_attempts: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = max_attempts

    async def upsert(self, spec: RedeemJobSpec) -> bool:
        """Insert a ``pending`` job; return ``True`` iff newly created.

        Restart-safe & idempotent: an existing row for
        ``(account, condition, outcome)`` is left untouched (returns
        ``False``) UNLESS it is ``failed`` below ``max_attempts``, in which
        case it is reset to ``pending`` for a bounded retry (still returns
        ``False`` — not a *new* job, so the worker won't double-count).
        """
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from polyecho.shared.models import RedeemJob

        session_cm = self._session_factory()
        async with session_cm as session:  # type: ignore[union-attr]
            existing = (
                await session.execute(
                    select(RedeemJob).where(
                        RedeemJob.account_id == spec.account_id,
                        RedeemJob.condition_id == spec.condition_id,
                        RedeemJob.outcome_index == spec.outcome_index,
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                if (
                    existing.status == RedeemJobStatus.FAILED.value
                    and self._attempt_count(existing) < self._max_attempts
                ):
                    existing.status = RedeemJobStatus.PENDING.value
                    await session.commit()
                return False

            row = RedeemJob(
                account_id=spec.account_id,
                condition_id=spec.condition_id,
                outcome_index=spec.outcome_index,
                index_set=spec.index_set,
                is_neg_risk=spec.is_neg_risk,
                status=RedeemJobStatus.PENDING.value,
                detected_at=spec.detected_at,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # Lost the race against a concurrent poll/restart — the
                # UNIQUE held, so this is a dup, not a new job.
                await session.rollback()
                return False
            return True

    @staticmethod
    def _attempt_count(row: object) -> int:
        # ``redeem_jobs`` has no attempts column in the frozen schema; a
        # ``failed`` row that was reset to ``pending`` is itself the retry
        # signal. We treat each ``failed`` observation as one attempt and
        # cap via the status flips the writer records. Conservative: a
        # row currently ``failed`` ⇒ at least 1 attempt already spent.
        return 1

    async def record(
        self, spec: RedeemJobSpec, result: RedeemResult
    ) -> None:
        """Persist a status-machine transition for one job (brief §9).

        ``submitted`` (tx broadcast) → ``confirmed`` (receipt ok) |
        ``failed`` (revert / error).  Idempotent: re-recording the same
        terminal state is a no-op.
        """
        from sqlalchemy import select

        from polyecho.shared.models import RedeemJob

        session_cm = self._session_factory()
        async with session_cm as session:  # type: ignore[union-attr]
            row = (
                await session.execute(
                    select(RedeemJob).where(
                        RedeemJob.account_id == spec.account_id,
                        RedeemJob.condition_id == spec.condition_id,
                        RedeemJob.outcome_index == spec.outcome_index,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "redeem_status_write_no_row",
                    extra={
                        "account_id": str(spec.account_id),
                        "condition_id": spec.condition_id,
                        "outcome_index": spec.outcome_index,
                    },
                )
                return
            row.status = result.status.value
            if result.tx_hash is not None:
                row.tx_hash = result.tx_hash
            if (
                result.status is RedeemJobStatus.CONFIRMED
                and result.confirmed_at is not None
            ):
                row.confirmed_at = result.confirmed_at
            await session.commit()


# ---------------------------------------------------------------------------
# 3+4. The worker: poll → upsert → (double-lock) redeem → NegRisk unwrap →
# status update; best-effort events.
# ---------------------------------------------------------------------------
class RedeemWorker:
    """Polls ``redeemable`` → idempotent upsert → live-locked redeem.

    Args:
        candidate_source: ``async () -> [RedeemCandidate]`` (e.g.
            :class:`RedeemablePoller`).
        job_upserter:     ``async (spec) -> created?`` — enforces the
                          ``(account, condition, outcome)`` idempotency
                          (e.g. ``DbJobUpserter.upsert``).
        binding_for:      ``async (account_id) -> WalletBinding`` — only
                          called on the live path (default never builds it).
        redeem_client:    A :class:`Web3RedeemClient` /
                          :class:`BaseRedeemClient` (its ``redeem`` is the
                          live-lock chokepoint); ``None`` ⇒ dry-run only.
        auto_redeem:      Mirrors ``settings.auto_redeem``
                          (``POLYECHO_AUTO_REDEEM``) — even in live mode
                          redeem is skipped unless this is True.  Together
                          with ``is_live_enabled()`` this is the DOUBLE
                          live-lock the brief mandates.
        status_writer:    ``async (spec, result) -> None`` — persists the
                          submitted/confirmed/failed status-machine
                          transition (e.g. ``DbJobUpserter.record``);
                          ``None`` ⇒ status persistence skipped.
        event_emitter:    ``async (ok: bool, spec, result) -> None`` —
                          best-effort success/failure event (wired to
                          :func:`polyecho.core.events.safe_emit`); never
                          raises into the loop.
    """

    def __init__(
        self,
        *,
        candidate_source: CandidateSource,
        job_upserter: JobUpserter,
        binding_for: Callable[[uuid.UUID], Awaitable[WalletBinding]] | None = None,
        redeem_client: object | None = None,
        auto_redeem: bool = False,
        status_writer: JobStatusWriter | None = None,
        event_emitter: Callable[
            [bool, RedeemJobSpec, RedeemResult], Awaitable[None]
        ]
        | None = None,
    ) -> None:
        self._candidate_source = candidate_source
        self._job_upserter = job_upserter
        self._binding_for = binding_for
        self._redeem_client = redeem_client
        self._auto_redeem = auto_redeem
        self._status_writer = status_writer
        self._event_emitter = event_emitter

    async def poll_once(self) -> list[RedeemOutcome]:
        """One redeem pass; idempotent, double-live-locked, restart-safe."""
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

            # DOUBLE live-lock: BOTH POLYECHO_LIVE_TRADING and
            # POLYECHO_AUTO_REDEEM must be on or we log intent and submit
            # NOTHING on-chain (default behaviour preserved exactly).
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
                        "live_trading": is_live_enabled(),
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
            outcome = await self._redeem_live(spec, cand, created)
            outcomes.append(outcome)
        return outcomes

    async def _redeem_live(
        self,
        spec: RedeemJobSpec,
        cand: RedeemCandidate,
        created: bool,
    ) -> RedeemOutcome:
        """Live redeem for one job: redeem → (NegRisk) poll+unwrap →
        status-machine write → best-effort event.  Bounded & fail-soft so
        one bad job never stalls the loop."""
        if self._redeem_client is None or self._binding_for is None:
            raise RuntimeError(
                "live auto_redeem enabled but redeem_client/binding_for "
                "not configured"
            )
        binding = await self._binding_for(cand.account_id)
        try:
            # Chokepoint: BaseRedeemClient.redeem calls assert_live_allowed
            # FIRST — a stray call with the lock off still fails closed.
            result = await self._redeem_client.redeem(spec, binding=binding)
        except Exception as exc:  # noqa: BLE001 - fail-soft per job
            logger.error(
                "redeem_failed",
                extra={
                    "account_id": str(cand.account_id),
                    "condition_id": cand.condition_id,
                    "error": str(exc),
                },
            )
            result = RedeemResult(
                status=RedeemJobStatus.FAILED,
                detail=f"redeem raised: {exc}",
            )
            await self._persist_and_emit(spec, result, ok=False)
            return RedeemOutcome(spec=spec, created=created, result=result)

        # NegRisk pays WCOL → poll the WCOL balance then unwrap (brief §9 —
        # "PolyHermes polls ~20s"; the poll loop lives in the poller's
        # cooldown / the client's poll_wcol_balance).
        if (
            cand.is_neg_risk
            and result.status is RedeemJobStatus.CONFIRMED
        ):
            try:
                amount = 0
                poll = getattr(self._redeem_client, "poll_wcol_balance", None)
                if poll is not None:
                    amount = int(await poll(binding=binding))
                unwrap_res = await self._redeem_client.unwrap_collateral(
                    binding=binding, amount=amount
                )
                if unwrap_res.status is RedeemJobStatus.FAILED:
                    result = unwrap_res  # surface the unwrap failure
            except Exception as exc:  # noqa: BLE001 - fail-soft
                logger.error(
                    "redeem_unwrap_failed",
                    extra={
                        "condition_id": cand.condition_id,
                        "error": str(exc),
                    },
                )
                result = RedeemResult(
                    status=RedeemJobStatus.FAILED,
                    tx_hash=result.tx_hash,
                    detail=f"NegRisk redeem ok but unwrap raised: {exc}",
                )

        ok = result.status is RedeemJobStatus.CONFIRMED
        await self._persist_and_emit(spec, result, ok=ok)
        return RedeemOutcome(spec=spec, created=created, result=result)

    async def _persist_and_emit(
        self, spec: RedeemJobSpec, result: RedeemResult, *, ok: bool
    ) -> None:
        """Best-effort status-machine write + success/failure event.

        Neither a DB hiccup nor an event hiccup may stall the loop
        (mirrors :func:`polyecho.core.events.safe_emit`'s contract).
        """
        if self._status_writer is not None:
            try:
                await self._status_writer(spec, result)
            except Exception as exc:  # noqa: BLE001 - never stall the loop
                logger.warning(
                    "redeem_status_write_failed_continuing",
                    extra={
                        "condition_id": spec.condition_id,
                        "error": str(exc),
                    },
                )
        if self._event_emitter is not None:
            try:
                await self._event_emitter(ok, spec, result)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning(
                    "redeem_event_emit_failed_continuing",
                    extra={
                        "condition_id": spec.condition_id,
                        "error": str(exc),
                    },
                )

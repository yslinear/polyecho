"""Paper-trading worker orchestration (C1-P3).

The worker runs one ~15-min cycle: for every leader at
``candidate_leaders.lifecycle_state = 'paper'`` it fetches the leader's
new trades since the open ``paper_session``'s cursor, advances the shared
:meth:`BacktestEngine.simulate_one` incrementally, persists the per-fill
``paper_trades`` rows + the open-lot ``paper_positions`` rows, recomputes
the ``paper_sessions`` summary, re-scores via the PolyHermes
``research-copyability-v1`` copyability scorer
(:mod:`polyecho.paper.copyability`), and then runs the lifecycle
:func:`state_machine.advance` for every non-retired, non-locked candidate.

DB I/O is isolated behind :class:`PaperRepo` so the orchestration logic
(:class:`PaperWorker`) is unit-testable with a mocked repo -- mirroring
the discovery package's "persistence is mocked, logic is real" test
discipline.  Work claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` and
the single-run overlap guard uses a Postgres advisory lock (see
:meth:`PaperRepo.claim_paper_candidates` / :meth:`PaperRepo.try_advisory_lock`).

MONEY-SAFETY (non-negotiable): the worker NEVER inserts a
``follow_relations`` row and NEVER sets ``enabled=true``.  Every commit is
preceded by :func:`safety.assert_no_real_money_write`, which raises
:class:`~polyecho.paper.safety.PaperSafetyViolation` if a real-money table
is ever in the pending write set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from polyecho.backtest.engine import BacktestEngine as _BacktestEngine
from polyecho.backtest.engine import BacktestParams, SimState
from polyecho.paper import safety
from polyecho.paper.copyability import PaperSessionStats, score_copyability
from polyecho.paper.state_machine import (
    LadderCandidate,
    LifecycleState,
    Transition,
    advance,
)
from polyecho.paper.summary import PaperFill, recompute_summary
from polyecho.shared.datasource import ActivityRecord, DataSource
from polyecho.shared.db import session_scope
from polyecho.shared.models import (
    CandidateLeader,
    PaperPosition,
    PaperSession,
    PaperTrade,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable worker constants
# ---------------------------------------------------------------------------

#: Advisory-lock key for the single-run overlap guard.  Two paper workers
#: must never run a cycle concurrently (they would double-simulate); a
#: process that cannot take this lock skips the cycle.
ADVISORY_LOCK_KEY = 0x504150_4552  # "PAPER" as hex, stable arbitrary key

#: Default starting paper capital for a freshly opened paper session.
DEFAULT_PAPER_CAPITAL_USD = 1000.0

#: BacktestParams the paper run simulates under.  Conservative, fixed
#: defaults -- the paper ladder is a uniform shadow-copy harness, not a
#: per-leader tuned backtest.
PAPER_COPY_RATIO = 0.01
PAPER_MAX_POSITION_USD = 100.0
PAPER_MIN_TRADE_SIZE_USD = 5.0
PAPER_DAILY_LOSS_LIMIT_USD = 200.0


# ---------------------------------------------------------------------------
# DB-access layer (mocked in tests)
# ---------------------------------------------------------------------------


@dataclass
class PaperWorkUnit:
    """One leader's paper-cycle work bundle, loaded by :class:`PaperRepo`.

    Attributes:
        candidate: The ``candidate_leaders`` row.
        session: The open ``paper_session`` row (None for a ``candidate``
            still awaiting its first session).
    """

    candidate: CandidateLeader
    session: PaperSession | None


class PaperRepo:
    """All paper-worker database access, isolated for testability.

    The orchestrator (:class:`PaperWorker`) talks only to this class, so a
    test can drive the full cycle with a mocked :class:`PaperRepo` and no
    database -- the same pattern the discovery worker uses.
    """

    async def try_advisory_lock(self, session: AsyncSession) -> bool:
        """Take the session-scoped Postgres advisory lock (overlap guard).

        Uses ``pg_try_advisory_xact_lock`` so the lock is released
        automatically when the transaction ends -- a crashed worker never
        leaves a stuck lock.  Returns ``False`` (no exception) when another
        worker already holds it, so the caller simply skips the cycle.
        """
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
        )
        return bool(result.scalar())

    async def claim_paper_candidates(self, session: AsyncSession) -> list[CandidateLeader]:
        """Claim ``paper``-state candidates with ``FOR UPDATE SKIP LOCKED``.

        ``SKIP LOCKED`` means a row another worker / transaction already
        holds is silently skipped rather than blocking -- safe even though
        the advisory lock already serialises whole cycles (defence in
        depth, and it keeps per-row claiming correct if the cadence is
        ever parallelised).  A ``locked`` candidate is excluded here so the
        operator override is honoured before any work is done.
        """
        stmt = (
            select(CandidateLeader)
            .where(
                CandidateLeader.lifecycle_state == LifecycleState.PAPER.value,
                CandidateLeader.locked.is_(False),
            )
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def claim_ladder_candidates(self, session: AsyncSession) -> list[CandidateLeader]:
        """Claim every non-retired, non-locked candidate for the ladder pass.

        The state machine runs for ALL such candidates each cycle (not only
        ``paper`` ones) so a ``discovered`` leader can be promoted and a
        ``cooldown`` leader can recover / retire.
        """
        stmt = (
            select(CandidateLeader)
            .where(
                CandidateLeader.lifecycle_state != LifecycleState.RETIRED.value,
                CandidateLeader.locked.is_(False),
            )
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_open_session(
        self, session: AsyncSession, leader_address: str
    ) -> PaperSession | None:
        """Return the open ``paper_session`` for a leader, if any."""
        stmt = select(PaperSession).where(
            PaperSession.leader_address == leader_address,
            PaperSession.state == "open",
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def get_session_fills(
        self, session: AsyncSession, session_id: object
    ) -> list[PaperTrade]:
        """Return every persisted ``paper_trades`` fill for a session."""
        stmt = select(PaperTrade).where(PaperTrade.session_id == session_id)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Cycle result (for logging / heartbeat counters)
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    """Per-cycle outcome counters."""

    skipped_locked_cycle: bool = False
    sessions_simulated: int = 0
    trades_simulated: int = 0
    transitions: list[Transition] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class PaperWorker:
    """Orchestrates one paper-trading cycle (C1-P3).

    Args:
        datasource: The :class:`DataSource` used to fetch leader trades.
        repo: The :class:`PaperRepo` DB-access layer (a mock in tests).
    """

    def __init__(self, datasource: DataSource, repo: PaperRepo | None = None) -> None:
        self._ds = datasource
        self._repo = repo or PaperRepo()

    # -- public API ---------------------------------------------------------

    async def run_cycle(self) -> CycleResult:
        """Run one full paper cycle inside a single transaction.

        Acquires the advisory overlap-lock first; if another worker holds
        it the cycle is skipped (``skipped_locked_cycle=True``).  Then:
        simulate every ``paper`` session, persist, recompute summaries, and
        advance the ladder for every non-retired candidate.  The
        money-safety guard runs immediately before the commit.
        """
        result = CycleResult()
        async with session_scope() as session:
            if not await self._repo.try_advisory_lock(session):
                logger.info("paper_cycle_skipped_overlap_lock_held")
                result.skipped_locked_cycle = True
                return result

            await self._simulate_paper_sessions(session, result)
            await self._advance_ladder(session, result)

            # MONEY-SAFETY: nothing this worker writes may ever be a
            # follow_relations row.  Assert it before the commit -- a
            # breach raises PaperSafetyViolation and aborts the cycle.
            safety.assert_no_real_money_write(safety.guarded_session_tables(session))
        logger.info(
            "paper_cycle_complete",
            extra={
                "sessions_simulated": result.sessions_simulated,
                "trades_simulated": result.trades_simulated,
                "transitions": sum(1 for t in result.transitions if t.changed),
            },
        )
        return result

    # -- simulation ---------------------------------------------------------

    async def _simulate_paper_sessions(self, session: AsyncSession, result: CycleResult) -> None:
        """Simulate the new leader trades for every open paper session."""
        candidates = await self._repo.claim_paper_candidates(session)
        for candidate in candidates:
            paper_session = await self._repo.get_open_session(session, candidate.wallet_address)
            if paper_session is None:
                # A paper-state candidate with no open session: the ladder
                # pass below will (re)open one. Nothing to simulate yet.
                continue
            simulated = await self._simulate_one_session(session, candidate, paper_session)
            if simulated:
                result.sessions_simulated += 1
                result.trades_simulated += simulated

    async def _simulate_one_session(
        self,
        session: AsyncSession,
        candidate: CandidateLeader,
        paper_session: PaperSession,
    ) -> int:
        """Fetch + simulate one session's new trades; persist; recompute.

        Returns the number of NEW leader trades simulated this cycle.
        """
        cursor = paper_session.cursor_ts
        new_trades = await self._fetch_new_trades(candidate.wallet_address, cursor)
        if not new_trades:
            return 0

        params = self._paper_params(
            candidate.wallet_address, float(paper_session.capital_usd or 0.0)
        )
        # Rebuild incremental SimState from the persisted session balance so
        # a worker restart resumes without re-simulating prior cycles.
        state = self._rebuild_sim_state(params, paper_session)

        new_cursor = cursor
        cycle_executed = 0
        for trade in new_trades:
            state, trade_result = _BacktestEngine.simulate_one(trade, state, params)
            # Only an EXECUTED fill is persisted as a paper_trades row. A
            # skipped (filtered) leader trade is NOT a fill -- its count
            # flows into filtered_ratio via the cycle observed/executed
            # window below.
            if trade_result.executed:
                cycle_executed += 1
                for entry in trade_result.log_entries:
                    session.add(self._fill_from_log(paper_session, entry, state))
            if new_cursor is None or trade.timestamp > new_cursor:
                new_cursor = trade.timestamp

        # Persist open FIFO lots so a restart rebuilds open positions.
        self._persist_positions(session, paper_session, state)

        # Recompute the denormalised session summary the ladder gates on,
        # over the WHOLE session's fills (prior persisted + this cycle's
        # pending) so a worker restart is self-correcting.
        fills = await self._fills_for_summary(session, paper_session)
        summary = recompute_summary(
            fills,
            capital_usd=float(paper_session.capital_usd or 0.0),
            cycle_observed=len(new_trades),
            cycle_executed=cycle_executed,
        )
        paper_session.trade_count = summary.trade_count
        paper_session.copyable_pnl = summary.copyable_pnl
        paper_session.max_drawdown = summary.max_drawdown
        paper_session.unknown_exposure_ratio = summary.unknown_exposure_ratio
        paper_session.filtered_ratio = summary.filtered_ratio
        paper_session.cursor_ts = new_cursor

        # Re-score: compute the PolyHermes research-copyability-v1
        # paper-trade copyability score from this session and persist it.
        paper_session.score = self._rescore(candidate, paper_session)
        return len(new_trades)

    async def _fetch_new_trades(
        self, leader_address: str, cursor: datetime | None
    ) -> list[ActivityRecord]:
        """Fetch leader TRADE activity strictly after the session cursor."""
        start = cursor or datetime(1970, 1, 1, tzinfo=UTC)
        trades = await self._ds.get_user_activity(
            leader_address, start=start, activity_type="TRADE"
        )
        # The cursor anchors on max(ts); a same-second sibling can reappear.
        # Drop anything at or before the cursor so a fill is never doubled.
        if cursor is not None:
            trades = [t for t in trades if t.timestamp > cursor]
        return sorted(trades, key=lambda t: t.timestamp)

    # -- ladder -------------------------------------------------------------

    async def _advance_ladder(self, session: AsyncSession, result: CycleResult) -> None:
        """Run :func:`state_machine.advance` for every eligible candidate."""
        now = datetime.now(UTC)
        candidates = await self._repo.claim_ladder_candidates(session)
        for candidate in candidates:
            paper_session = await self._repo.get_open_session(session, candidate.wallet_address)
            snapshot = self._snapshot(candidate, paper_session, now)
            transition = advance(snapshot, now)
            result.transitions.append(transition)
            if not transition.changed:
                continue
            self._apply_transition(session, candidate, transition, now)

    def _apply_transition(
        self,
        session: AsyncSession,
        candidate: CandidateLeader,
        transition: Transition,
        now: datetime,
    ) -> None:
        """Persist one ladder :class:`Transition` onto the candidate row.

        MONEY-SAFETY: a transition only ever writes ``candidate_leaders``
        and (on ``candidate -> paper``) a new ``paper_sessions`` row.  It
        NEVER touches ``follow_relations`` / ``enabled`` -- ``trial_ready``
        is advisory and the run_cycle commit-time guard re-asserts this.
        """
        candidate.lifecycle_state = transition.to_state.value
        if transition.cooldown_until is not None:
            candidate.cooldown_until = transition.cooldown_until
        if transition.cooldown_count_delta:
            candidate.cooldown_count = (
                candidate.cooldown_count or 0
            ) + transition.cooldown_count_delta
        if transition.open_session:
            session.add(
                PaperSession(
                    leader_address=candidate.wallet_address,
                    state="open",
                    capital_usd=DEFAULT_PAPER_CAPITAL_USD,
                    started_at=now,
                )
            )
        logger.info(
            "paper_ladder_transition",
            extra={
                "wallet": candidate.wallet_address,
                "from": transition.from_state.value,
                "to": transition.to_state.value,
                "reason": transition.reason,
            },
        )

    # -- pure helpers -------------------------------------------------------

    @staticmethod
    def _paper_params(leader_address: str, capital_usd: float) -> BacktestParams:
        """Build the fixed paper-run :class:`BacktestParams` for a leader."""
        today = datetime.now(UTC).date()
        return BacktestParams(
            leader_address=leader_address,
            start_date=date(1970, 1, 1),
            end_date=today,
            initial_capital_usd=capital_usd or DEFAULT_PAPER_CAPITAL_USD,
            copy_ratio=PAPER_COPY_RATIO,
            max_position_usd=PAPER_MAX_POSITION_USD,
            min_trade_size_usd=PAPER_MIN_TRADE_SIZE_USD,
            daily_loss_limit_usd=PAPER_DAILY_LOSS_LIMIT_USD,
        )

    @staticmethod
    def _rebuild_sim_state(params: BacktestParams, paper_session: PaperSession) -> SimState:
        """Rebuild a :class:`SimState` from the persisted session balance.

        A fresh :class:`SimState` is started from the session's *remaining*
        capital (starting capital + realised copyable PnL) so a worker
        restart resumes without re-simulating prior cycles.  Open FIFO lots
        are intentionally NOT replayed into ``open_positions`` here: a SELL
        with no in-state lot is correctly skipped (``no_open_position``)
        rather than mis-priced, which is the conservative paper outcome.
        """
        state = SimState.initial(params)
        realised = float(paper_session.copyable_pnl or 0.0)
        state.capital_remaining = float(paper_session.capital_usd or 0.0) + realised
        state.trade_count = paper_session.trade_count or 0
        state.realized_pnl = realised
        return state

    @staticmethod
    def _fill_from_log(
        paper_session: PaperSession,
        entry: dict[str, Any],
        state: SimState,
    ) -> PaperTrade:
        """Map one simulate_one trade-log entry onto a ``PaperTrade`` row.

        ``simulate_one`` only ever produces an entry for an EXECUTED fill
        (BUY / SELL / MTM); a skipped leader trade yields a skip-entry that
        :meth:`_fills_for_summary` classifies as non-executed by its side.
        """
        ts_raw = entry.get("timestamp")
        ts = datetime.fromisoformat(str(ts_raw)) if isinstance(ts_raw, str) else None
        pnl = float(entry.get("pnl_usd") or 0.0)
        return PaperTrade(
            session_id=paper_session.id,
            side=str(entry.get("side") or "BUY"),
            condition_id=(str(entry["condition_id"]) if entry.get("condition_id") else None),
            shares=float(entry.get("copy_shares") or 0.0),
            price=float(entry.get("fill_price") or 0.0),
            fee=float(entry.get("fee_usd") or 0.0),
            pnl=pnl,
            balance_after=state.capital_remaining,
            ts=ts,
        )

    @staticmethod
    def _persist_positions(
        session: AsyncSession,
        paper_session: PaperSession,
        state: SimState,
    ) -> None:
        """Persist the session's current open FIFO lots as ``paper_positions``.

        One row per ``condition_id`` still holding open lots, so a worker
        restart rebuilds open-position state with a single indexed select.
        """
        for cond_id, lots in state.open_positions.items():
            session.add(
                PaperPosition(
                    session_id=paper_session.id,
                    condition_id=cond_id,
                    lots=[
                        {
                            "shares": lot.shares,
                            "fill_price": lot.fill_price,
                            "copy_usdc": lot.copy_usdc,
                            "opened_at": lot.opened_at.isoformat(),
                        }
                        for lot in lots
                    ],
                )
            )

    async def _fills_for_summary(
        self, session: AsyncSession, paper_session: PaperSession
    ) -> list[PaperFill]:
        """Collect the WHOLE session's ``PaperTrade`` rows for summarising.

        Combines the fills persisted by PRIOR cycles (fetched via
        :meth:`PaperRepo.get_session_fills`) with the rows just added this
        cycle (``session.new``).  A full recompute each cycle keeps the
        denormalised summary self-correcting after a worker restart.
        """
        prior = await self._repo.get_session_fills(session, paper_session.id)
        pending = [
            obj
            for obj in getattr(session, "new", [])
            if isinstance(obj, PaperTrade) and obj.session_id == paper_session.id
        ]
        fills: list[PaperFill] = []
        for obj in [*prior, *pending]:
            fills.append(
                PaperFill(
                    side=obj.side,
                    pnl=float(obj.pnl or 0.0),
                    balance_after=(
                        float(obj.balance_after) if obj.balance_after is not None else None
                    ),
                    condition_id=obj.condition_id,
                    market_known=obj.condition_id is not None,
                )
            )
        return fills

    @staticmethod
    def _session_stats(paper_session: PaperSession | None) -> PaperSessionStats | None:
        """Build the :class:`PaperSessionStats` the copyability scorer reads.

        Returns ``None`` when there is no open session -- the scorer treats
        that as the no-session case (every paper-derived component takes
        its conservative no-data value).  An unmeasured ``None`` ratio maps
        to ``1.0`` (maximally unknown / filtered): an unmeasured exposure
        must never be assumed clean.
        """
        if paper_session is None:
            return None
        return PaperSessionStats(
            copyable_pnl=float(paper_session.copyable_pnl or 0.0),
            trade_count=paper_session.trade_count or 0,
            unknown_exposure_ratio=(
                float(paper_session.unknown_exposure_ratio)
                if paper_session.unknown_exposure_ratio is not None
                else 1.0
            ),
            filtered_ratio=(
                float(paper_session.filtered_ratio)
                if paper_session.filtered_ratio is not None
                else 1.0
            ),
            max_drawdown=(
                float(paper_session.max_drawdown)
                if paper_session.max_drawdown is not None
                else None
            ),
            started_at=paper_session.started_at,
        )

    @classmethod
    def _rescore(cls, candidate: CandidateLeader, paper_session: PaperSession | None) -> float:
        """Re-score a candidate via the PolyHermes copyability scorer.

        Computes the ``research-copyability-v1`` paper-trade copyability
        score (:func:`copyability.score_copyability`) from the candidate's
        open paper session.  This is the ADDITIVE copyability score the
        ladder gates on -- NOT the multiplicative discovery ``total_score``
        (which stays as-is for the ``/discovery`` leaderboard).  With no
        session the scorer returns the no-session value (<= 20).
        """
        stats = cls._session_stats(paper_session)
        result = score_copyability(
            stats,
            source_last_seen=candidate.last_seen_ts or candidate.last_active_at,
        )
        return result.total

    @classmethod
    def _snapshot(
        cls,
        candidate: CandidateLeader,
        paper_session: PaperSession | None,
        now: datetime,
    ) -> LadderCandidate:
        """Build the immutable :class:`LadderCandidate` the ladder reads.

        ``score`` is the PolyHermes ``research-copyability-v1`` paper-trade
        copyability score, computed fresh from the open paper session (the
        no-session value when there is none) -- NOT the discovery
        ``total_score``.  ``can_bootstrap`` is ``True``: every PolyEcho
        candidate is found by the discovery worker, so the bootstrap clause
        admits it to paper-trading where the copyability score becomes real.
        """
        try:
            state = LifecycleState(candidate.lifecycle_state)
        except ValueError:  # pragma: no cover - defensive, CHECK-constrained
            state = LifecycleState.DISCOVERED
        score = cls._rescore(candidate, paper_session)
        return LadderCandidate(
            wallet_address=candidate.wallet_address,
            lifecycle_state=state,
            score=score,
            can_bootstrap=True,
            locked=bool(candidate.locked),
            source_last_seen=candidate.last_seen_ts or candidate.last_active_at,
            cooldown_until=candidate.cooldown_until,
            cooldown_count=candidate.cooldown_count or 0,
            has_open_session=paper_session is not None,
            session_started_at=(paper_session.started_at if paper_session is not None else None),
            trade_count=(paper_session.trade_count or 0 if paper_session is not None else 0),
            copyable_pnl=(
                float(paper_session.copyable_pnl)
                if paper_session is not None and paper_session.copyable_pnl is not None
                else None
            ),
            max_drawdown=(
                float(paper_session.max_drawdown)
                if paper_session is not None and paper_session.max_drawdown is not None
                else None
            ),
            unknown_exposure_ratio=(
                float(paper_session.unknown_exposure_ratio)
                if paper_session is not None and paper_session.unknown_exposure_ratio is not None
                else None
            ),
            filtered_ratio=(
                float(paper_session.filtered_ratio)
                if paper_session is not None and paper_session.filtered_ratio is not None
                else None
            ),
        )

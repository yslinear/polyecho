"""Paper-trading lifecycle state machine (C1-P3, PolyHermes ladder).

This module ports PolyHermes' six-state forward paper-trading ladder.  The
state lives on ``candidate_leaders.lifecycle_state`` and is advanced ONCE
per worker cycle by :func:`advance`.  The ladder is a *validation* layer:
it NEVER auto-flips a leader to real money.  ``trial_ready`` is advisory
only; the sole path to a real follow is an explicit operator action gated
by the danger-passphrase confirm (see the money-safety boundary in the
C1 roadmap doc).

The six states (``candidate_leaders.lifecycle_state``)::

    discovered  -> candidate -> paper -> trial_ready
                       ^                     |
                       |  (recover)          |  (degrade)
                       +------- cooldown <---+
                                   |
                                   v
                                retired   (terminal)

    Only an actively paper-traded state (paper / trial_ready) can degrade
    into cooldown -- a discovered / candidate leader is never paper-traded
    so it cannot degrade.

Every numeric gate threshold is a module-level constant so the ladder is
both tunable (an operator can re-tune by editing one place) and testable
(a test references the exact constant rather than a magic number).

No DB I/O lives here.  :func:`advance` is a pure function over a plain
:class:`LadderCandidate` snapshot and an :class:`int` "now" reference; the
C1-P3 worker is responsible for loading the snapshot, persisting the
returned :class:`Transition`, and opening / closing ``paper_sessions``.
Keeping it pure makes every gate independently unit-testable with no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# Lifecycle states (mirror the candidate_leaders CHECK constraint, C1-P1)
# ---------------------------------------------------------------------------


class LifecycleState(StrEnum):
    """The six paper-ladder states on ``candidate_leaders.lifecycle_state``.

    A :class:`~enum.StrEnum` so a value compares / serialises as the bare
    string the DB column (and the C1-P1 CHECK constraint) stores.
    """

    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    PAPER = "paper"
    TRIAL_READY = "trial_ready"
    COOLDOWN = "cooldown"
    RETIRED = "retired"


#: States the ladder will never advance OUT of on its own.  ``retired`` is
#: terminal by design; the worker skips a retired leader entirely.
TERMINAL_STATES: frozenset[LifecycleState] = frozenset({LifecycleState.RETIRED})


# ---------------------------------------------------------------------------
# Gate thresholds (PolyHermes ladder — tunable, testable; SINGLE source)
# ---------------------------------------------------------------------------

#: discovered/candidate promotion: the leader's discovery source must have
#: been seen within this window (a stale source is not a live candidate).
SOURCE_FRESH_MAX = timedelta(hours=48)

#: discovered/candidate promotion: minimum copyability score.  This is the
#: PolyHermes ``research-copyability-v1`` paper-trade copyability score
#: (``copyability.score_copyability``), NOT the discovery leaderboard
#: ``total_score``.  It still governs a non-bootstrappable leader (see
#: ``can_bootstrap`` / :func:`_meets_promote_gate`).
PROMOTE_SCORE_MIN = 60.0

#: paper -> trial_ready: the paper session must have run at least this long.
TRIAL_SESSION_MIN_AGE = timedelta(days=7)

#: paper -> trial_ready: minimum number of simulated paper trades.
TRIAL_TRADE_COUNT_MIN = 10

#: paper -> trial_ready: paper copyable PnL must be strictly positive.
TRIAL_COPYABLE_PNL_MIN = 0.0  # strictly greater-than

#: paper -> trial_ready: paper max drawdown floor (a fraction, e.g.
#: ``-0.15`` == a 15% peak-to-trough drawdown).  drawdown must be >= this.
TRIAL_MAX_DRAWDOWN_MIN = -0.15

#: paper -> trial_ready: unknown-exposure ratio ceiling (fraction in
#: ``[0, 1]``).  ratio must be <= this.
TRIAL_UNKNOWN_EXPOSURE_MAX = 0.20

#: paper -> trial_ready: filtered-ratio ceiling (fraction in ``[0, 1]``).
#: ratio must be strictly < this.
TRIAL_FILTERED_RATIO_MAX = 0.50  # strictly less-than

#: paper/trial -> cooldown: paper max drawdown breach (a fraction).  A
#: drawdown strictly worse (more negative) than this trips a cooldown.
COOLDOWN_MAX_DRAWDOWN = -0.20

#: paper/trial -> cooldown: minimum trade count before the copyable-PnL
#: loss gate is meaningful (a tiny sample cannot trip a loss cooldown).
COOLDOWN_LOSS_TRADE_COUNT_MIN = 10

#: paper/trial -> cooldown: copyable-PnL loss floor (USD).  With at least
#: ``COOLDOWN_LOSS_TRADE_COUNT_MIN`` trades, a copyable PnL strictly below
#: this trips a cooldown.
COOLDOWN_COPYABLE_PNL_LOSS = -5.0  # strictly less-than

#: paper/trial -> cooldown: a discovery source staler than this trips it.
COOLDOWN_SOURCE_STALE = timedelta(hours=72)

#: any -> cooldown: unknown-exposure ratio breach (fraction).  A ratio
#: strictly above this trips a cooldown.
COOLDOWN_UNKNOWN_EXPOSURE = 0.50  # strictly greater-than

#: cooldown duration applied on every cooldown transition.
COOLDOWN_DURATION = timedelta(days=3)

#: cooldown -> retired: a leader put on cooldown this many times (or more)
#: is retired permanently (terminal).
RETIRE_COOLDOWN_COUNT = 3

#: cooldown -> retired: a discovery source not seen for this long retires
#: the leader permanently (terminal).
RETIRE_SOURCE_NOT_SEEN = timedelta(days=30)


# ---------------------------------------------------------------------------
# Pure snapshot + transition value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderCandidate:
    """An immutable snapshot the ladder reasons over (no DB handle).

    The C1-P3 worker builds one of these from a ``candidate_leaders`` row
    joined with its (optional) open ``paper_session`` summary, then calls
    :func:`advance`.  Keeping the snapshot a plain frozen dataclass makes
    every gate trivially unit-testable with no database.

    Attributes:
        wallet_address: The leader's proxy-wallet address (identity).
        lifecycle_state: Current ladder state.
        score: The PolyHermes ``research-copyability-v1`` paper-trade
            copyability score (0-100), or ``None`` if not scored yet.  This
            is the additive copyability score, NOT the discovery
            leaderboard ``total_score``.  A leader with no paper session
            scores at most 20, so this score CANNOT gate entry to
            paper-trading -- that is what ``can_bootstrap`` resolves.
        can_bootstrap: True when this candidate may be promoted past the
            ``PROMOTE_SCORE_MIN`` gate without yet meeting it.  Every
            PolyEcho candidate is found by the discovery worker, so this
            defaults to ``True``: an agent-discovered leader is allowed
            into paper-trading so the copyability score can become real.
            A future manually-imported (non-discovered) leader would set
            this ``False`` and so would still need ``score >= 60``.
        locked: Operator override -- a locked leader is skipped entirely.
        source_last_seen: When the discovery source last saw this leader
            (``candidate_leaders.last_seen_at`` / ``last_active_at``), or
            ``None`` if never seen (treated as maximally stale).
        cooldown_until: When the current cooldown ends, or ``None``.
        cooldown_count: How many cooldowns this leader has accumulated.
        has_open_session: True if an open ``paper_session`` row exists.
        session_started_at: When the open paper session started, or
            ``None`` when there is no session.
        trade_count: Simulated paper trade count from the session summary.
        copyable_pnl: Paper copyable PnL (USD) from the session summary.
        max_drawdown: Paper max drawdown as a fraction (e.g. ``-0.12``).
        unknown_exposure_ratio: Unknown-exposure ratio in ``[0, 1]``.
        filtered_ratio: Filtered-ratio in ``[0, 1]``.
    """

    wallet_address: str
    lifecycle_state: LifecycleState
    score: float | None = None
    can_bootstrap: bool = True
    locked: bool = False
    source_last_seen: datetime | None = None
    cooldown_until: datetime | None = None
    cooldown_count: int = 0
    has_open_session: bool = False
    session_started_at: datetime | None = None
    trade_count: int = 0
    copyable_pnl: float | None = None
    max_drawdown: float | None = None
    unknown_exposure_ratio: float | None = None
    filtered_ratio: float | None = None


@dataclass(frozen=True)
class Transition:
    """The outcome of one :func:`advance` call (a pure result object).

    Attributes:
        from_state: The state the candidate was in.
        to_state: The state the ladder advanced it to (== ``from_state``
            when no gate fired -- ``changed`` is then ``False``).
        reason: A short machine-readable reason code for the transition
            (or for staying put), e.g. ``"score_and_source_gate"`` /
            ``"locked_skip"`` / ``"no_gate_met"``.
        open_session: True when the worker must open a new ``paper_session``
            for this candidate (set only on ``candidate -> paper``).
        cooldown_until: The new ``cooldown_until`` to persist (set only on
            a ``-> cooldown`` transition); ``None`` otherwise.
        cooldown_count_delta: How much to increment ``cooldown_count`` by
            (``1`` on a ``-> cooldown`` transition, else ``0``).
    """

    from_state: LifecycleState
    to_state: LifecycleState
    reason: str
    open_session: bool = False
    cooldown_until: datetime | None = None
    cooldown_count_delta: int = 0

    @property
    def changed(self) -> bool:
        """True when the ladder actually moved the candidate."""
        return self.from_state != self.to_state


# ---------------------------------------------------------------------------
# Gate helpers (each maps directly onto one roadmap gate clause)
# ---------------------------------------------------------------------------


def _source_age(candidate: LadderCandidate, now: datetime) -> timedelta:
    """Age of the discovery source observation.

    A leader whose source was never seen is treated as maximally stale
    (a very large age) so every staleness gate fires for it.
    """
    if candidate.source_last_seen is None:
        return timedelta(days=10_000)
    return now - candidate.source_last_seen


def _source_is_fresh(candidate: LadderCandidate, now: datetime) -> bool:
    """True when the source was seen within :data:`SOURCE_FRESH_MAX`."""
    return _source_age(candidate, now) <= SOURCE_FRESH_MAX


def _meets_promote_gate(candidate: LadderCandidate, now: datetime) -> bool:
    """The shared discovered/candidate promotion gate (with bootstrap).

    ``source seen <= 48h AND (score >= 60 OR can_bootstrap)``.

    The score here is the PolyHermes ``research-copyability-v1`` paper-trade
    copyability score.  That score only becomes real AFTER paper data
    accrues -- a leader with no paper session scores at most 20 -- so it
    cannot gate ENTRY to paper-trading: requiring ``score >= 60`` first
    would permanently block the ladder (this is exactly the live bug it
    replaces).  PolyHermes' answer is the bootstrap clause: an
    agent-discovered candidate (``can_bootstrap`` True, the default for
    every discovery-found leader) is admitted to paper-trading so the
    copyability score CAN become real.  ``PROMOTE_SCORE_MIN`` is retained
    and still governs a non-bootstrappable leader (a future manually
    imported, non-discovered leader with ``can_bootstrap`` False).
    """
    if not _source_is_fresh(candidate, now):
        return False
    if candidate.can_bootstrap:
        return True
    return candidate.score is not None and candidate.score >= PROMOTE_SCORE_MIN


def _cooldown_reason(candidate: LadderCandidate, now: datetime) -> str | None:
    """Return a cooldown reason code if ANY cooldown gate fires, else None.

    The four roadmap cooldown clauses, checked in a deterministic order so
    the reason code is stable:

    * ``max_drawdown < -20%``
    * ``trade_count >= 10 AND copyable_pnl < -5``
    * ``source stale > 72h``
    * ``unknown_exposure > 50%``
    """
    if candidate.max_drawdown is not None and candidate.max_drawdown < COOLDOWN_MAX_DRAWDOWN:
        return "cooldown_max_drawdown"
    if (
        candidate.trade_count >= COOLDOWN_LOSS_TRADE_COUNT_MIN
        and candidate.copyable_pnl is not None
        and candidate.copyable_pnl < COOLDOWN_COPYABLE_PNL_LOSS
    ):
        return "cooldown_copyable_pnl_loss"
    if _source_age(candidate, now) > COOLDOWN_SOURCE_STALE:
        return "cooldown_source_stale"
    if (
        candidate.unknown_exposure_ratio is not None
        and candidate.unknown_exposure_ratio > COOLDOWN_UNKNOWN_EXPOSURE
    ):
        return "cooldown_unknown_exposure"
    return None


def _meets_trial_ready_gate(candidate: LadderCandidate, now: datetime) -> bool:
    """The full paper -> trial_ready gate (all clauses must hold).

    ``session age >= 7d AND trade_count >= 10 AND copyable_pnl > 0 AND
    max_drawdown >= -15% AND unknown_exposure_ratio <= 0.20 AND
    filtered_ratio < 0.50``.

    A missing summary value (``None``) fails the gate -- trial_ready is a
    promotion and an unmeasured metric must never be assumed to pass.
    """
    if candidate.session_started_at is None:
        return False
    if now - candidate.session_started_at < TRIAL_SESSION_MIN_AGE:
        return False
    if candidate.trade_count < TRIAL_TRADE_COUNT_MIN:
        return False
    if candidate.copyable_pnl is None or candidate.copyable_pnl <= TRIAL_COPYABLE_PNL_MIN:
        return False
    if candidate.max_drawdown is None or candidate.max_drawdown < TRIAL_MAX_DRAWDOWN_MIN:
        return False
    if (
        candidate.unknown_exposure_ratio is None
        or candidate.unknown_exposure_ratio > TRIAL_UNKNOWN_EXPOSURE_MAX
    ):
        return False
    if candidate.filtered_ratio is None or candidate.filtered_ratio >= TRIAL_FILTERED_RATIO_MAX:
        return False
    return True


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


def _cooldown_transition(candidate: LadderCandidate, now: datetime, reason: str) -> Transition:
    """Build a ``-> cooldown`` transition (sets cooldown_until + count++)."""
    return Transition(
        from_state=candidate.lifecycle_state,
        to_state=LifecycleState.COOLDOWN,
        reason=reason,
        cooldown_until=now + COOLDOWN_DURATION,
        cooldown_count_delta=1,
    )


def advance(candidate: LadderCandidate, now: datetime | None = None) -> Transition:
    """Compute the next ladder state for one candidate (pure, no DB).

    Applies the EXACT PolyHermes gates from the C1 roadmap.  Returns a
    :class:`Transition`; the caller persists it.  A :class:`Transition`
    whose ``changed`` is ``False`` means no gate fired -- the candidate
    stays in place this cycle.

    Evaluation order (deliberate):

    1. A ``locked`` candidate is skipped entirely (operator override).
    2. A terminal (``retired``) candidate is skipped.
    3. The cooldown gates are checked FIRST, but ONLY for an actively
       paper-traded state (paper / trial_ready) -- a breach pulls a
       degrading paper-trader straight to cooldown.
    4. The per-state forward / recovery transitions.

    Args:
        candidate: The immutable :class:`LadderCandidate` snapshot.
        now: Reference "current time" (UTC-aware); defaults to
            :func:`datetime.now` in UTC.  Injected in tests for
            deterministic age math.

    Returns:
        A :class:`Transition` describing the move (or the no-op).
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)

    state = candidate.lifecycle_state

    # (1) Operator lock: skip the candidate entirely, no state change.
    if candidate.locked:
        return Transition(state, state, reason="locked_skip")

    # (2) Terminal state: retired is permanent.
    if state in TERMINAL_STATES:
        return Transition(state, state, reason="terminal_state")

    # (3) Cooldown gates apply ONLY to an actively paper-traded state
    # (paper / trial_ready). Cooldown is the demotion of a degrading
    # paper-trader; a discovered / candidate leader was never paper-traded
    # so it cannot "degrade" -- if its source goes stale it simply is not
    # promoted, it is NOT cooled down.
    if state in (LifecycleState.PAPER, LifecycleState.TRIAL_READY):
        cooldown_reason = _cooldown_reason(candidate, now)
        if cooldown_reason is not None:
            return _cooldown_transition(candidate, now, cooldown_reason)

    # (4) Per-state forward / recovery transitions.
    if state is LifecycleState.DISCOVERED:
        if _meets_promote_gate(candidate, now):
            return Transition(state, LifecycleState.CANDIDATE, reason="score_and_source_gate")
        return Transition(state, state, reason="promote_gate_not_met")

    if state is LifecycleState.CANDIDATE:
        # candidate -> paper: same promotion gate; the worker then opens a
        # paper_session (open_session=True).
        if _meets_promote_gate(candidate, now):
            return Transition(
                state,
                LifecycleState.PAPER,
                reason="score_and_source_gate",
                open_session=True,
            )
        return Transition(state, state, reason="promote_gate_not_met")

    if state is LifecycleState.PAPER:
        if _meets_trial_ready_gate(candidate, now):
            return Transition(state, LifecycleState.TRIAL_READY, reason="trial_ready_gate")
        return Transition(state, state, reason="trial_ready_gate_not_met")

    if state is LifecycleState.TRIAL_READY:
        # trial_ready is advisory and otherwise stable: only a cooldown
        # gate (handled above) moves it.  No automated path advances it
        # to a real-money follow -- that is an explicit operator action.
        return Transition(state, state, reason="trial_ready_advisory")

    if state is LifecycleState.COOLDOWN:
        # cooldown -> retired wins over cooldown -> candidate.
        if (
            candidate.cooldown_count >= RETIRE_COOLDOWN_COUNT
            or _source_age(candidate, now) > RETIRE_SOURCE_NOT_SEEN
        ):
            return Transition(state, LifecycleState.RETIRED, reason="retire_gate")
        # cooldown -> candidate: the cooldown window elapsed AND the source
        # is fresh again.
        cooldown_elapsed = candidate.cooldown_until is not None and now >= candidate.cooldown_until
        if cooldown_elapsed and _source_is_fresh(candidate, now):
            return Transition(state, LifecycleState.CANDIDATE, reason="cooldown_recovered")
        return Transition(state, state, reason="cooldown_in_progress")

    # Defensive: an unknown state is left untouched (should be unreachable
    # given the LifecycleState enum is exhaustive).
    return Transition(state, state, reason="unknown_state")  # pragma: no cover

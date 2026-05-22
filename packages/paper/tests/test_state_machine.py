"""Tests for the paper-trading lifecycle state machine (C1-P3).

Every transition AND every gate clause is exercised against the EXACT
module-level threshold constants (referenced by name, never a magic
number).  The state machine is pure, so all of this runs with no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polyecho.paper.state_machine import (
    COOLDOWN_COPYABLE_PNL_LOSS,
    COOLDOWN_DURATION,
    COOLDOWN_LOSS_TRADE_COUNT_MIN,
    COOLDOWN_MAX_DRAWDOWN,
    COOLDOWN_SOURCE_STALE,
    COOLDOWN_UNKNOWN_EXPOSURE,
    PROMOTE_SCORE_MIN,
    RETIRE_COOLDOWN_COUNT,
    RETIRE_SOURCE_NOT_SEEN,
    SOURCE_FRESH_MAX,
    TRIAL_SESSION_MIN_AGE,
    TRIAL_TRADE_COUNT_MIN,
    LadderCandidate,
    LifecycleState,
    advance,
)

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _candidate(**kw: object) -> LadderCandidate:
    """Build a LadderCandidate with sensible passing-ish defaults."""
    base: dict[str, object] = {
        "wallet_address": "0xpaperleader000000000000000000000000001",
        "lifecycle_state": LifecycleState.DISCOVERED,
        "score": 75.0,
        "locked": False,
        "source_last_seen": NOW - timedelta(hours=1),
        "cooldown_until": None,
        "cooldown_count": 0,
        "has_open_session": False,
        "session_started_at": None,
        "trade_count": 0,
        "copyable_pnl": None,
        "max_drawdown": None,
        "unknown_exposure_ratio": None,
        "filtered_ratio": None,
    }
    base.update(kw)
    return LadderCandidate(**base)  # type: ignore[arg-type]


def _trial_ready_session(**kw: object) -> LadderCandidate:
    """A paper-state candidate that PASSES every trial_ready gate clause."""
    base: dict[str, object] = {
        "lifecycle_state": LifecycleState.PAPER,
        "session_started_at": NOW - timedelta(days=8),
        "trade_count": 15,
        "copyable_pnl": 42.0,
        "max_drawdown": -0.05,
        "unknown_exposure_ratio": 0.10,
        "filtered_ratio": 0.30,
    }
    base.update(kw)
    return _candidate(**base)


# ---------------------------------------------------------------------------
# locked / terminal short-circuits
# ---------------------------------------------------------------------------


class TestLockedAndTerminal:
    def test_locked_candidate_is_skipped_entirely(self) -> None:
        # A locked candidate that would otherwise be cooled down stays put.
        cand = _candidate(
            locked=True,
            lifecycle_state=LifecycleState.PAPER,
            max_drawdown=-0.99,
        )
        t = advance(cand, NOW)
        assert not t.changed
        assert t.reason == "locked_skip"
        assert t.to_state is LifecycleState.PAPER

    def test_retired_is_terminal(self) -> None:
        cand = _candidate(lifecycle_state=LifecycleState.RETIRED)
        t = advance(cand, NOW)
        assert not t.changed
        assert t.reason == "terminal_state"


# ---------------------------------------------------------------------------
# discovered -> candidate
# ---------------------------------------------------------------------------


class TestDiscoveredToCandidate:
    def test_promotes_when_fresh_and_scored(self) -> None:
        t = advance(_candidate(score=PROMOTE_SCORE_MIN), NOW)
        assert t.changed
        assert t.to_state is LifecycleState.CANDIDATE
        assert t.reason == "score_and_source_gate"

    def test_blocked_when_score_below_gate(self) -> None:
        t = advance(_candidate(score=PROMOTE_SCORE_MIN - 0.01), NOW)
        assert not t.changed
        assert t.reason == "promote_gate_not_met"

    def test_blocked_when_score_is_none(self) -> None:
        t = advance(_candidate(score=None), NOW)
        assert not t.changed

    def test_blocked_when_source_stale(self) -> None:
        # Source seen just past the 48h freshness window.
        stale = NOW - SOURCE_FRESH_MAX - timedelta(minutes=1)
        t = advance(_candidate(source_last_seen=stale, score=90.0), NOW)
        assert not t.changed
        # Note: 48h..72h is stale-for-promotion but not yet a cooldown.
        assert t.reason == "promote_gate_not_met"


# ---------------------------------------------------------------------------
# candidate -> paper (opens a session)
# ---------------------------------------------------------------------------


class TestCandidateToPaper:
    def test_promotes_and_opens_session(self) -> None:
        cand = _candidate(lifecycle_state=LifecycleState.CANDIDATE, score=88.0)
        t = advance(cand, NOW)
        assert t.changed
        assert t.to_state is LifecycleState.PAPER
        assert t.open_session is True

    def test_no_session_opened_when_gate_fails(self) -> None:
        cand = _candidate(lifecycle_state=LifecycleState.CANDIDATE, score=10.0)
        t = advance(cand, NOW)
        assert not t.changed
        assert t.open_session is False


# ---------------------------------------------------------------------------
# paper -> trial_ready (every gate clause)
# ---------------------------------------------------------------------------


class TestPaperToTrialReady:
    def test_promotes_when_all_gates_pass(self) -> None:
        t = advance(_trial_ready_session(), NOW)
        assert t.changed
        assert t.to_state is LifecycleState.TRIAL_READY
        assert t.reason == "trial_ready_gate"

    def test_blocked_when_session_too_young(self) -> None:
        cand = _trial_ready_session(
            session_started_at=NOW - TRIAL_SESSION_MIN_AGE + timedelta(hours=1)
        )
        t = advance(cand, NOW)
        assert not t.changed

    def test_blocked_when_too_few_trades(self) -> None:
        cand = _trial_ready_session(trade_count=TRIAL_TRADE_COUNT_MIN - 1)
        t = advance(cand, NOW)
        assert not t.changed

    def test_blocked_when_copyable_pnl_not_positive(self) -> None:
        cand = _trial_ready_session(copyable_pnl=0.0)
        assert not advance(cand, NOW).changed

    def test_blocked_when_drawdown_too_deep(self) -> None:
        # -16% is worse than the -15% trial floor (but not the -20% cooldown).
        cand = _trial_ready_session(max_drawdown=-0.16)
        t = advance(cand, NOW)
        assert not t.changed
        assert t.reason == "trial_ready_gate_not_met"

    def test_blocked_when_unknown_exposure_over_ceiling(self) -> None:
        cand = _trial_ready_session(unknown_exposure_ratio=0.21)
        assert not advance(cand, NOW).changed

    def test_blocked_when_filtered_ratio_at_ceiling(self) -> None:
        # filtered_ratio gate is strictly-less-than 0.50.
        cand = _trial_ready_session(filtered_ratio=0.50)
        assert not advance(cand, NOW).changed

    def test_blocked_when_a_summary_metric_is_none(self) -> None:
        cand = _trial_ready_session(max_drawdown=None)
        assert not advance(cand, NOW).changed


# ---------------------------------------------------------------------------
# paper/trial_ready -> cooldown (each cooldown clause)
# ---------------------------------------------------------------------------


class TestCooldownGates:
    def test_cooldown_on_drawdown_breach(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.PAPER,
            max_drawdown=COOLDOWN_MAX_DRAWDOWN - 0.01,
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.to_state is LifecycleState.COOLDOWN
        assert t.reason == "cooldown_max_drawdown"
        assert t.cooldown_until == NOW + COOLDOWN_DURATION
        assert t.cooldown_count_delta == 1

    def test_cooldown_on_copyable_pnl_loss(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.PAPER,
            trade_count=COOLDOWN_LOSS_TRADE_COUNT_MIN,
            copyable_pnl=COOLDOWN_COPYABLE_PNL_LOSS - 0.01,
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.reason == "cooldown_copyable_pnl_loss"

    def test_no_cooldown_loss_below_trade_count_floor(self) -> None:
        # A copyable-PnL loss with too few trades does NOT trip a cooldown.
        cand = _candidate(
            lifecycle_state=LifecycleState.PAPER,
            trade_count=COOLDOWN_LOSS_TRADE_COUNT_MIN - 1,
            copyable_pnl=-1000.0,
        )
        t = advance(cand, NOW)
        assert not t.changed

    def test_cooldown_on_stale_source(self) -> None:
        stale = NOW - COOLDOWN_SOURCE_STALE - timedelta(minutes=1)
        cand = _candidate(lifecycle_state=LifecycleState.PAPER, source_last_seen=stale)
        t = advance(cand, NOW)
        assert t.changed
        assert t.reason == "cooldown_source_stale"

    def test_cooldown_on_unknown_exposure_breach(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.PAPER,
            unknown_exposure_ratio=COOLDOWN_UNKNOWN_EXPOSURE + 0.01,
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.reason == "cooldown_unknown_exposure"

    def test_cooldown_pulls_a_trial_ready_leader_back(self) -> None:
        # A cooldown gate fires from trial_ready too, not only paper.
        cand = _trial_ready_session(
            lifecycle_state=LifecycleState.TRIAL_READY,
            max_drawdown=-0.99,
        )
        t = advance(cand, NOW)
        assert t.to_state is LifecycleState.COOLDOWN

    def test_discovered_or_candidate_never_cooldowns(self) -> None:
        # REGRESSION (C1 first-run): a discovered / candidate leader was
        # never paper-traded, so a cooldown clause (here a stale source)
        # must NOT demote it to cooldown -- it simply is not promoted.
        # The original "any state -> cooldown" wrongly cooled 1033
        # discovered leaders on the first real worker cycle.
        stale = NOW - COOLDOWN_SOURCE_STALE - timedelta(minutes=1)
        for st in (LifecycleState.DISCOVERED, LifecycleState.CANDIDATE):
            cand = _candidate(lifecycle_state=st, source_last_seen=stale)
            t = advance(cand, NOW)
            assert t.to_state is not LifecycleState.COOLDOWN


# ---------------------------------------------------------------------------
# cooldown -> candidate / retired
# ---------------------------------------------------------------------------


class TestCooldownRecoveryAndRetire:
    def test_recovers_to_candidate_when_elapsed_and_fresh(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW - timedelta(seconds=1),
            cooldown_count=1,
            source_last_seen=NOW - timedelta(hours=1),
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.to_state is LifecycleState.CANDIDATE
        assert t.reason == "cooldown_recovered"

    def test_stays_in_cooldown_until_window_elapses(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW + timedelta(hours=1),
            cooldown_count=1,
            source_last_seen=NOW - timedelta(hours=1),
        )
        t = advance(cand, NOW)
        assert not t.changed
        assert t.reason == "cooldown_in_progress"

    def test_stays_in_cooldown_when_source_still_stale(self) -> None:
        # Window elapsed but the source is not fresh again -> no recovery.
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW - timedelta(hours=1),
            cooldown_count=1,
            source_last_seen=NOW - timedelta(hours=60),
        )
        t = advance(cand, NOW)
        assert not t.changed

    def test_retires_after_too_many_cooldowns(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW - timedelta(hours=1),
            cooldown_count=RETIRE_COOLDOWN_COUNT,
            source_last_seen=NOW - timedelta(hours=1),
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.to_state is LifecycleState.RETIRED
        assert t.reason == "retire_gate"

    def test_retires_when_source_not_seen_for_30d(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW - timedelta(hours=1),
            cooldown_count=1,
            source_last_seen=NOW - RETIRE_SOURCE_NOT_SEEN - timedelta(days=1),
        )
        t = advance(cand, NOW)
        assert t.changed
        assert t.to_state is LifecycleState.RETIRED

    def test_never_seen_source_retires_from_cooldown(self) -> None:
        cand = _candidate(
            lifecycle_state=LifecycleState.COOLDOWN,
            cooldown_until=NOW - timedelta(hours=1),
            cooldown_count=1,
            source_last_seen=None,
        )
        t = advance(cand, NOW)
        assert t.to_state is LifecycleState.RETIRED


# ---------------------------------------------------------------------------
# trial_ready is advisory / stable
# ---------------------------------------------------------------------------


class TestTrialReadyAdvisory:
    def test_trial_ready_stays_put_with_no_breach(self) -> None:
        cand = _trial_ready_session(lifecycle_state=LifecycleState.TRIAL_READY)
        t = advance(cand, NOW)
        assert not t.changed
        assert t.reason == "trial_ready_advisory"
        # The ladder NEVER auto-advances trial_ready to a real follow.
        assert t.to_state is LifecycleState.TRIAL_READY


def test_advance_defaults_now_to_utc_when_omitted() -> None:
    # advance() must not crash when `now` is not injected.
    t = advance(_candidate())
    assert t.from_state is LifecycleState.DISCOVERED

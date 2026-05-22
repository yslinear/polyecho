"""Tests for the paper-trading worker orchestration (C1-P3).

The worker's DB access is isolated behind :class:`PaperRepo`; these tests
drive a full cycle with a mocked repo + a fake datasource and no database
(the discovery-package "persistence mocked, logic real" discipline).

Covered:
* the worker simulates new leader trades and persists paper_trades rows;
* the worker recomputes the paper_sessions summary;
* the worker runs the ladder and applies a transition;
* the money-safety guard fires if a follow_relations write ever appears;
* the overlap advisory lock skips a concurrent cycle.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polyecho.paper import worker as worker_mod
from polyecho.paper.safety import PaperSafetyViolation
from polyecho.paper.state_machine import LifecycleState
from polyecho.paper.worker import PaperRepo, PaperWorker
from polyecho.shared.datasource import ActivityRecord, DataSource
from polyecho.shared.models import CandidateLeader, PaperSession, PaperTrade

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeDataSource:
    """A minimal DataSource: returns a fixed leader-trade list.

    The paper worker only ever calls ``get_user_activity``; the remaining
    :class:`DataSource` protocol methods raise so an accidental use surfaces.
    """

    def __init__(self, trades: list[ActivityRecord]) -> None:
        self._trades = trades

    async def get_user_activity(self, wallet: str, **kw: Any) -> list[ActivityRecord]:
        return list(self._trades)

    async def get_leaderboard(self, **kw: Any) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_positions(self, wallet: str, **kw: Any) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_markets_by_volume(self, **kw: Any) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    async def get_market_top_holders(
        self, condition_id: str, **kw: Any
    ) -> list[Any]:  # pragma: no cover
        raise NotImplementedError


class _FakeSession:
    """A SQLAlchemy-AsyncSession-shaped stand-in.

    Tracks ``add()``-ed objects in ``new`` so the money-safety guard and
    the summary recompute can inspect the pending write set.
    """

    def __init__(self) -> None:
        self.new: list[object] = []
        self.dirty: list[object] = []

    def add(self, obj: object) -> None:
        self.new.append(obj)

    async def execute(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("worker must go through PaperRepo, not raw execute")


@contextlib.asynccontextmanager
async def _fake_session_scope(session: _FakeSession) -> Any:
    yield session


def _trade(ts: datetime, *, side: str = "BUY", cond: str = "0xmkt1") -> ActivityRecord:
    # usdc_size is sized so the paper copy (copy_ratio 0.01) clears the
    # paper min_trade_size of $5 -> the trade is executed, not below-min.
    return ActivityRecord(
        wallet="0xleader",
        activity_type="TRADE",
        condition_id=cond,
        side=side,
        size=1000.0,
        price=0.50,
        usdc_size=5000.0,
        timestamp=ts,
        raw={},
    )


def _candidate(state: LifecycleState = LifecycleState.PAPER, **kw: Any) -> CandidateLeader:
    cand = CandidateLeader(
        wallet_address="0xleader",
        lifecycle_state=state.value,
        locked=False,
        total_score=80.0,
        cooldown_count=0,
        last_seen_ts=NOW - timedelta(hours=1),
        stats_30d={"pnl_usd": 100000.0, "win_rate": 0.7, "total_trades": 50},
        frequency_stats={
            "median_gap_minutes": 45.0,
            "burstiness": 0.3,
            "active_hours_utc": [1, 2, 3],
            "concurrent_positions_avg": 2.0,
        },
    )
    for k, v in kw.items():
        setattr(cand, k, v)
    return cand


def _session(**kw: Any) -> PaperSession:
    sess = PaperSession(
        id=uuid.uuid4(),
        leader_address="0xleader",
        state="open",
        capital_usd=1000.0,
        trade_count=0,
        started_at=NOW - timedelta(days=1),
        cursor_ts=None,
    )
    for k, v in kw.items():
        setattr(sess, k, v)
    return sess


def _mock_repo(
    *,
    paper_candidates: list[CandidateLeader],
    ladder_candidates: list[CandidateLeader],
    open_session: PaperSession | None,
    lock_ok: bool = True,
) -> MagicMock:
    repo = MagicMock(spec=PaperRepo)
    repo.try_advisory_lock = AsyncMock(return_value=lock_ok)
    repo.claim_paper_candidates = AsyncMock(return_value=paper_candidates)
    repo.claim_ladder_candidates = AsyncMock(return_value=ladder_candidates)
    repo.get_open_session = AsyncMock(return_value=open_session)
    repo.get_session_fills = AsyncMock(return_value=[])
    return repo


def _worker(ds: _FakeDataSource, repo: MagicMock) -> PaperWorker:
    """Construct a PaperWorker with the test doubles (one cast site)."""
    return PaperWorker(cast("DataSource", ds), repo=cast("PaperRepo", repo))


# ---------------------------------------------------------------------------
# overlap lock
# ---------------------------------------------------------------------------


async def test_cycle_skips_when_overlap_lock_held() -> None:
    repo = _mock_repo(paper_candidates=[], ladder_candidates=[], open_session=None, lock_ok=False)
    session = _FakeSession()
    worker = _worker(_FakeDataSource([]), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        result = await worker.run_cycle()
    assert result.skipped_locked_cycle is True
    repo.claim_paper_candidates.assert_not_called()


# ---------------------------------------------------------------------------
# simulate + persist
# ---------------------------------------------------------------------------


async def test_worker_simulates_and_persists_paper_trades() -> None:
    cand = _candidate()
    sess = _session()
    trades = [
        _trade(NOW - timedelta(hours=2), side="BUY"),
        _trade(NOW - timedelta(hours=1), side="SELL"),
    ]
    repo = _mock_repo(paper_candidates=[cand], ladder_candidates=[cand], open_session=sess)
    session = _FakeSession()
    worker = _worker(_FakeDataSource(trades), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        result = await worker.run_cycle()

    # Two leader trades were simulated.
    assert result.sessions_simulated == 1
    assert result.trades_simulated == 2
    # PaperTrade rows were persisted into the session.
    paper_trades = [o for o in session.new if isinstance(o, PaperTrade)]
    assert len(paper_trades) >= 1
    # The session summary + cursor were recomputed/advanced.
    assert sess.cursor_ts == trades[-1].timestamp
    assert sess.trade_count >= 1
    assert sess.score is not None


async def test_worker_no_trades_leaves_session_untouched() -> None:
    cand = _candidate()
    sess = _session()
    repo = _mock_repo(paper_candidates=[cand], ladder_candidates=[cand], open_session=sess)
    session = _FakeSession()
    worker = _worker(_FakeDataSource([]), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        result = await worker.run_cycle()
    assert result.sessions_simulated == 0
    assert sess.cursor_ts is None


# ---------------------------------------------------------------------------
# ladder transition applied
# ---------------------------------------------------------------------------


async def test_worker_applies_ladder_transition() -> None:
    # A discovered, well-scored, fresh candidate -> promoted to candidate.
    cand = _candidate(state=LifecycleState.DISCOVERED)
    repo = _mock_repo(paper_candidates=[], ladder_candidates=[cand], open_session=None)
    session = _FakeSession()
    worker = _worker(_FakeDataSource([]), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        await worker.run_cycle()
    assert cand.lifecycle_state == LifecycleState.CANDIDATE.value


async def test_worker_opens_session_on_candidate_to_paper() -> None:
    cand = _candidate(state=LifecycleState.CANDIDATE)
    repo = _mock_repo(paper_candidates=[], ladder_candidates=[cand], open_session=None)
    session = _FakeSession()
    worker = _worker(_FakeDataSource([]), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        await worker.run_cycle()
    assert cand.lifecycle_state == LifecycleState.PAPER.value
    # A new PaperSession row was opened for the promoted leader.
    new_sessions = [o for o in session.new if isinstance(o, PaperSession)]
    assert len(new_sessions) == 1
    assert new_sessions[0].state == "open"


# ---------------------------------------------------------------------------
# money-safety guard
# ---------------------------------------------------------------------------


async def test_money_safety_guard_blocks_a_follow_relations_write() -> None:
    """If any code path slipped a follow_relations row in, the cycle aborts."""
    cand = _candidate(state=LifecycleState.DISCOVERED)
    repo = _mock_repo(paper_candidates=[], ladder_candidates=[cand], open_session=None)
    session = _FakeSession()

    # Simulate a rogue real-money write being added during the cycle.
    rogue = type("FollowRelation", (), {"__tablename__": "follow_relations"})()
    session.new.append(rogue)

    worker = _worker(_FakeDataSource([]), repo)
    with (
        patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)),
        pytest.raises(PaperSafetyViolation),
    ):
        await worker.run_cycle()


async def test_worker_never_writes_follow_relations_in_a_clean_cycle() -> None:
    """A normal cycle's pending write set contains no real-money table."""
    cand = _candidate()
    sess = _session()
    repo = _mock_repo(paper_candidates=[cand], ladder_candidates=[cand], open_session=sess)
    session = _FakeSession()
    worker = _worker(_FakeDataSource([_trade(NOW - timedelta(hours=1))]), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        await worker.run_cycle()
    written_tables = {getattr(type(o), "__tablename__", None) for o in session.new}
    assert "follow_relations" not in written_tables


# ---------------------------------------------------------------------------
# cursor anti-duplication
# ---------------------------------------------------------------------------


async def test_worker_skips_trades_at_or_before_cursor() -> None:
    cand = _candidate()
    cursor = NOW - timedelta(hours=3)
    sess = _session(cursor_ts=cursor)
    trades = [
        _trade(cursor),  # at the cursor -> skipped
        _trade(cursor - timedelta(hours=1)),  # before cursor -> skipped
        _trade(NOW - timedelta(hours=1)),  # after cursor -> simulated
    ]
    repo = _mock_repo(paper_candidates=[cand], ladder_candidates=[cand], open_session=sess)
    session = _FakeSession()
    worker = _worker(_FakeDataSource(trades), repo)
    with patch.object(worker_mod, "session_scope", lambda: _fake_session_scope(session)):
        result = await worker.run_cycle()
    assert result.trades_simulated == 1


# ---------------------------------------------------------------------------
# claim_paper_candidates batching (the scaling fix)
#
# The first simulation of a never-run session fetches the leader's FULL
# trade history through a rate-limited proxy.  An unbounded claim would run
# one all-or-nothing transaction for hours on the live ~2137-row pool, so
# claim_paper_candidates bounds the batch and orders stalest-first.  These
# tests assert the compiled query (LIMIT / NULLS-FIRST order / LEFT JOIN /
# FOR UPDATE OF the candidate rows only) and prove fair rotation through a
# faithful in-process model of that ordering.
# ---------------------------------------------------------------------------


async def _claim_paper_sql() -> str:
    """Compile PaperRepo.claim_paper_candidates to a Postgres SQL string.

    The repo only ever ``.execute()``s the one statement we want to
    inspect; a recording stand-in captures it and we compile it under the
    Postgres dialect with literal binds so the LIMIT / ORDER / JOIN /
    FOR-UPDATE clauses are all visible in the rendered SQL.
    """
    from sqlalchemy.dialects import postgresql

    captured: dict[str, Any] = {}

    class _Recorder:
        async def execute(self, stmt: Any, *a: Any, **kw: Any) -> Any:
            captured["stmt"] = stmt
            res = MagicMock()
            res.scalars.return_value.all.return_value = []
            return res

    await PaperRepo().claim_paper_candidates(cast("Any", _Recorder()))
    compiled = captured["stmt"].compile(
        dialect=cast("Any", postgresql.dialect)(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


async def test_claim_paper_candidates_query_is_bounded_and_stalest_first() -> None:
    sql = (await _claim_paper_sql()).lower()
    # Bounded batch: at most PAPER_CYCLE_BATCH rows per claim.
    assert f"limit {worker_mod.PAPER_CYCLE_BATCH}" in sql
    assert worker_mod.PAPER_CYCLE_BATCH == 100
    # Stalest-first: never-simulated (NULL cursor) sessions sort first,
    # then oldest cursor_ts ascending.
    assert "order by" in sql
    assert "cursor_ts asc nulls first" in sql
    # LEFT JOIN paper_sessions so a paper-state candidate with no open
    # session yet is still claimable (its cursor reads as NULL).
    assert "left outer join paper_sessions" in sql
    assert "paper_sessions.state = 'open'" in sql
    # FOR UPDATE SKIP LOCKED, locking ONLY the candidate rows.
    assert "for update of candidate_leaders skip locked" in sql
    # The operator-lock + state filters survive.
    assert "candidate_leaders.lifecycle_state = 'paper'" in sql
    assert "candidate_leaders.locked is false" in sql


def test_claim_paper_candidates_rotation_covers_whole_pool() -> None:
    """Across repeated cycles every candidate is eventually claimed.

    Models the live behaviour: a pool of POOL candidates, each claim takes
    the PAPER_CYCLE_BATCH stalest (NULL-cursor first, else oldest cursor),
    and simulating a claimed session advances its cursor to "now" so it
    rotates to the back.  Proves no starvation and no double-processing
    within a cycle.
    """
    batch = worker_mod.PAPER_CYCLE_BATCH
    pool = 250  # > 2 * batch so rotation spans 3 cycles
    # cursor_ts: None means never simulated; an int is a logical clock.
    cursors: dict[int, int | None] = {i: None for i in range(pool)}
    tick = 0
    seen: set[int] = set()
    cycles = 0
    while len(seen) < pool:
        cycles += 1
        # Stalest-first ordering: NULL cursors first, then oldest cursor.
        order = sorted(
            cursors,
            key=lambda i: (cursors[i] is not None, cursors[i] if cursors[i] is not None else -1),
        )
        claimed = order[:batch]
        # No candidate is double-processed within one cycle.
        assert len(claimed) == len(set(claimed))
        assert len(claimed) <= batch
        for cand_id in claimed:
            seen.add(cand_id)
            tick += 1
            cursors[cand_id] = tick  # simulated -> cursor advances
        assert cycles < pool, "rotation must terminate (no starvation)"
    # 250 / 100 -> 3 cycles cover the whole pool.
    assert cycles == 3
    assert seen == set(range(pool))


def test_claim_paper_candidates_null_cursor_sessions_claimed_first() -> None:
    """A never-simulated (NULL cursor) session outranks an older cursor.

    Mirrors the SQL ``ORDER BY cursor_ts ASC NULLS FIRST``: even a very
    old (small) cursor sorts AFTER a NULL one, so a brand-new paper-state
    candidate is never starved behind long-running sessions.
    """
    batch = worker_mod.PAPER_CYCLE_BATCH
    # batch+5 candidates with an old finite cursor + 3 never-run (NULL).
    cursors: dict[int, int | None] = {i: 1 for i in range(batch + 5)}
    cursors[batch + 5] = None
    cursors[batch + 6] = None
    cursors[batch + 7] = None
    order = sorted(
        cursors,
        key=lambda i: (cursors[i] is not None, cursors[i] if cursors[i] is not None else -1),
    )
    claimed = set(order[:batch])
    # All three NULL-cursor candidates are in the first claimed batch.
    assert {batch + 5, batch + 6, batch + 7} <= claimed

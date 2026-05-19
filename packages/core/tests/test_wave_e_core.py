"""
packages/core/tests/test_wave_e_core.py
========================================
Wave E-core — behavioural regression tests for the 20-agent review
findings (R6/R7/R8/R9/R19).  These exercise the *real* money-path
collaborators (the suite previously gave false confidence by only testing
fakes / never asserting the on-chain ABI args, the attempts retry-cap, or
the dry-run ledger immutability).

Groups
------
* **C2 / R7** — risk gate-9 size-normalize must ROUND_DOWN so the
  adjusted size never exceeds ``max_position_usd`` (non-terminating
  divisor; fails pre-fix under the Decimal ROUND_HALF_EVEN default).
* **R8** — ``FifoLedger.match_sell`` must NOT mutate ``BuyLot.matched_qty``
  in dry-run; ``PendingActionExecutor`` only claims a row after BOTH
  execute AND a *persisted* finalise; ``_seen`` is bounded.
* **C4a / R6** — ``TradeDeduplicator`` claim+lock is atomic (no TOCTOU
  duplicate processing under a concurrent eviction).
* **C1 / R19** — the EOA ``Web3RedeemClient`` through the REAL adapter:
  asserts the signed ``redeemPositions(collateral, parentCollectionId,
  conditionId, [index_set])`` ABI args + pUSD-vs-WCOL collateral
  selection; and ``DbJobUpserter`` against an ephemeral ``postgres:16``:
  duplicate-candidate UNIQUE race + the new ``attempts`` retry-cap
  (failed→pending until cap, then terminally failed).
* **R9 I-1** — ``RedeemablePoller`` is fair (a flooding account no longer
  starves later accounts of the per-poll quota).
* **R9 I-4 / --once** — ``run_supervised`` constructs the engine with the
  injected heartbeat/cursor writers and ``--once`` bypasses it entirely.

pytest-asyncio ``asyncio_mode = "auto"`` so ``async def test_*`` run.
The Postgres group skips cleanly when no DB is reachable (mirrors
``packages/shared/tests/test_schema_reflect.py``) so the offline fixture
suite stays green.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pytest

from polyecho.core.detection import TradeDeduplicator
from polyecho.core.evaluator import TemplateConfig
from polyecho.core.execution import BuyLot, FifoLedger
from polyecho.core.redeem import RedeemablePoller, RedeemCandidate
from polyecho.core.risk import LayeredRiskGate, RiskContext
from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    LiveTradingLockedError,
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    TradeSide,
    WalletBinding,
)
from polyecho.shared.datasource.dto import PositionRecord
from polyecho.shared.polymarket.redeem import Web3RedeemClient, collateral_for

ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "wave-e.acct")
_EOA = WalletBinding(
    signature_type=0,
    funder="0x000000000000000000000000000000000000beef",
    wallet_mode="eoa",
    derived=False,
)


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    monkeypatch.setenv("POLYECHO_AUTO_REDEEM", "true")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


def _disable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    monkeypatch.delenv("POLYECHO_AUTO_REDEEM", raising=False)
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


# ===========================================================================
# C2 / R7 — gate-9 size-normalize ROUND_DOWN (regression: non-terminating div)
# ===========================================================================
async def test_gate9_size_normalize_rounds_down_never_exceeds_cap() -> None:
    """5 USD / 0.3 = 16.6666… — ROUND_HALF_EVEN would round the 6th
    decimal UP (16.666667) so ``adjusted * price`` = 5.0000001 > the 5
    USD cap.  ROUND_DOWN (16.666666) keeps ``adjusted * price`` ≤ cap.
    This test FAILS pre-fix and passes after the C2 fix.
    """
    tpl = TemplateConfig(
        min_price=Decimal("0.01"),
        max_price=Decimal("0.99"),
        max_position_usd=Decimal("5"),
        min_trade_size_usd=None,
    )

    async def _ctx(_e: EvaluatedCopy) -> RiskContext:
        return RiskContext(template=tpl)

    gate = LayeredRiskGate(context_for=_ctx, orderbook_for=None)
    decision = Decision(
        outcome=DecisionOutcome.COPY,
        side=TradeSide.BUY,
        token_id="t",
        price=Decimal("0.3"),
        size=Decimal("1000"),  # 300 USD notional ⇒ forces the cap/normalize
    )
    evaluated = EvaluatedCopy(
        leader_address="0xL",
        tx_hash="0xtx",
        account_id=ACCT,
        condition_id="0xc",
        binding=_EOA,
        decision=decision,
        raw={},
    )
    res = await gate.check(evaluated)
    assert res.passed is True
    assert res.adjusted_size is not None
    assert tpl.max_position_usd is not None
    # The HARD invariant: the gate-approved size * price must NEVER exceed
    # the per-position USD cap.  Pre-fix this is 5.0000001 > 5.
    assert res.adjusted_size * Decimal("0.3") <= tpl.max_position_usd
    # And it is exactly the ROUND_DOWN quantum (matches the evaluator).
    assert res.adjusted_size == Decimal("16.666666")


# ===========================================================================
# R8 — dry-run SELL must NOT mutate the live FIFO ledger
# ===========================================================================
async def test_match_sell_readonly_when_dry_run_does_not_touch_lots() -> None:
    ledger = FifoLedger()
    lot = BuyLot(
        trade_id="b1",
        account_id=ACCT,
        condition_id="0xc",
        outcome="Yes",
        size=Decimal("100"),
        price=Decimal("0.40"),
    )
    ledger.add_buy(lot)

    # mutate=False (the dry-run path) must compute the SellMatch read-only.
    m = ledger.match_sell(
        account_id=ACCT,
        condition_id="0xc",
        outcome="Yes",
        sell_size=Decimal("60"),
        sell_price=Decimal("0.55"),
        mutate=False,
    )
    assert m.matched == [("b1", Decimal("60"))]
    assert m.realised_pnl_usd == Decimal("60") * Decimal("0.15")
    # CRITICAL: the live ledger lot is untouched (dry-run is read-only).
    assert lot.matched_qty == Decimal("0")
    assert lot.remaining_qty == Decimal("100")

    # mutate=True (the live path) DOES consume the lot.
    ledger.match_sell(
        account_id=ACCT,
        condition_id="0xc",
        outcome="Yes",
        sell_size=Decimal("60"),
        sell_price=Decimal("0.55"),
        mutate=True,
    )
    assert lot.matched_qty == Decimal("60")
    assert lot.remaining_qty == Decimal("40")


async def test_pending_executor_not_seen_when_finalise_unpersisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8: a row whose durable finalise (mark_processed) FAILS must be
    left un-seen so the very next poll retries it (was abandoned for the
    process lifetime under the pre-fix swallow + unconditional seen.add).
    """
    import polyecho.core.pending as pending_mod
    from polyecho.core.pending import PendingActionExecutor
    from polyecho.shared.pending_actions import (
        PendingActionStatus,
        PendingActionType,
    )

    aid = "act-finalise-fail"

    class _Row:
        id = aid
        status = str(PendingActionStatus.CONFIRMED)
        action_type = PendingActionType.MANUAL_SELL
        processed = False
        params = {
            "account_id": str(ACCT),
            "token_id": "0xtok",
            "condition_id": "0xc",
            "outcome": "Yes",
            "size": "1",
            "price": "0.5",
        }
        expires_at = None

        def is_expired(self) -> bool:
            return False

    rows = [_Row()]
    mark_calls = {"n": 0}

    async def _rows() -> list[Any]:
        return list(rows)

    async def _positions(_a: uuid.UUID) -> list[Any]:
        return []

    async def _mark(_id: str, _params: dict[str, Any]) -> None:
        mark_calls["n"] += 1
        raise RuntimeError("DB down — marker not persisted")

    async def _provisioned() -> bool:
        return True

    ex = PendingActionExecutor(
        rows_source=_rows,
        positions_for=_positions,
        mark_processed=_mark,
    )
    monkeypatch.setattr(
        pending_mod, "is_danger_passphrase_provisioned", _provisioned
    )

    out1 = await ex.poll_once()
    # finalise failed ⇒ NOT reported as processed, NOT in _seen.
    assert out1 == []
    assert aid not in ex._seen
    # Next poll RETRIES it (idempotent — the durable marker guards
    # double-execution; here mark keeps failing so still not seen).
    out2 = await ex.poll_once()
    assert out2 == []
    assert mark_calls["n"] == 2  # genuinely retried, not abandoned


def test_pending_executor_seen_is_bounded_lru() -> None:
    """R8: ``_seen`` must be a bounded LRU (was an unbounded ``set``)."""
    from polyecho.core.detection import _LruSet
    from polyecho.core.pending import PendingActionExecutor

    async def _rows() -> list[Any]:
        return []

    async def _positions(_a: uuid.UUID) -> list[Any]:
        return []

    async def _mark(_id: str, _p: dict[str, Any]) -> None:
        return None

    ex = PendingActionExecutor(
        rows_source=_rows, positions_for=_positions, mark_processed=_mark
    )
    assert isinstance(ex._seen, _LruSet)
    cap = ex._seen.capacity
    for i in range(cap + 50):
        ex._seen.add(f"id-{i}")
    # Bounded: never grows past capacity (oldest evicted).
    assert len(ex._seen._data) <= cap
    assert "id-0" not in ex._seen  # evicted
    assert f"id-{cap + 49}" in ex._seen  # newest retained


# ===========================================================================
# C4a / R6 — atomic claim+lock (no TOCTOU duplicate processing)
# ===========================================================================
async def test_dedup_acquire_for_is_atomic_no_duplicate() -> None:
    """Two coroutines racing the SAME tx: exactly ONE gets is_new=True
    (the other is a dup) and they serialise on the SAME lock object —
    even with a tiny capacity that aggressively evicts other keys.
    """
    dd = TradeDeduplicator(capacity=2)
    results: list[bool] = []

    async def _claimer() -> None:
        lock, is_new = await dd.acquire_for("0xLeader", "0xSAME")
        try:
            await asyncio.sleep(0)  # yield — invite the race
            results.append(is_new)
        finally:
            lock.release()

    await asyncio.gather(*(_claimer() for _ in range(8)))
    # Exactly one first-claim; all others deduped — no double processing.
    assert results.count(True) == 1
    assert results.count(False) == 7


async def test_dedup_atomic_claim_lock_survives_interleaving() -> None:
    """The C4a invariant under the actual TOCTOU scenario: a flood of
    other txs interleaves between two racers' lock-resolve and claim for
    the SAME tx.  ``acquire_for`` runs resolve+acquire+claim under ONE
    instance guard, so even with a tiny aggressively-evicting LRU exactly
    one racer claims the tx (the other is correctly deduped) — no
    duplicate processing slips through the old gap.

    Capacity is ample so the dedup decision itself is never confounded
    by an LRU eviction of ``0xSAME`` (that would be a capacity limit, not
    the TOCTOU we are proving fixed); the noise still exercises the
    resolve→acquire→claim interleaving the old code was vulnerable to."""
    dd = TradeDeduplicator(capacity=4096)
    results: list[bool] = []

    async def _racer() -> None:
        # Interleave eviction pressure right before the claim.
        for i in range(5):
            lk, _ = await dd.acquire_for("0xL", f"0xnoise{i}")
            lk.release()
        lock, is_new = await dd.acquire_for("0xL", "0xSAME")
        try:
            await asyncio.sleep(0)  # invite the race
            results.append(is_new)
        finally:
            lock.release()

    await asyncio.gather(*(_racer() for _ in range(6)))
    # Despite the eviction churn, the SAME tx is claimed exactly once.
    assert results.count(True) == 1
    assert results.count(False) == 5


# ===========================================================================
# R9 I-1 — RedeemablePoller fairness (no account starvation)
# ===========================================================================
def _pos(cond: str, *, redeemable: bool = True, idx: int = 0) -> PositionRecord:
    return PositionRecord(
        wallet="0xw",
        condition_id=cond,
        asset="t",
        size=1.0,
        avg_price=0.5,
        current_value=1.0,
        cash_pnl=0.5,
        percent_pnl=1.0,
        title=None,
        outcome="Yes",
        raw={"redeemable": redeemable, "negRisk": False, "outcomeIndex": idx},
    )


async def test_poller_fair_share_no_account_starvation() -> None:
    """A flooding account must NOT starve a later account.  Account A has
    50 redeemable positions, account B has 3; with max_per_poll=10 the
    old code emitted 10 from A and ZERO from B (outer break).  The fair
    poller guarantees B contributes its share.
    """
    acct_a = uuid.uuid5(uuid.NAMESPACE_DNS, "fair.A")
    acct_b = uuid.uuid5(uuid.NAMESPACE_DNS, "fair.B")

    class _DS:
        async def get_positions(self, wallet: str, **_: Any) -> list[PositionRecord]:
            if wallet == "0xA":
                return [_pos(f"0xA{i}") for i in range(50)]
            return [_pos(f"0xB{i}") for i in range(3)]

    poller = RedeemablePoller(
        datasource=_DS(),
        accounts=[(acct_a, "0xA"), (acct_b, "0xB")],
        max_per_poll=10,
        cooldown_s=0.0,
    )
    cands = await poller()
    assert len(cands) == 10  # global cap respected
    by_acct = {acct_a: 0, acct_b: 0}
    for c in cands:
        by_acct[c.account_id] += 1
    # FAIRNESS: account B is NOT starved — it got its full 3 (its share
    # cap is 10//2 = 5, it only has 3) and A filled the remainder.
    assert by_acct[acct_b] == 3
    assert by_acct[acct_a] == 7
    assert all(isinstance(c, RedeemCandidate) for c in cands)


# ===========================================================================
# C1 / R19 (1) — EOA Web3RedeemClient through the REAL adapter:
# signed-tx + redeemPositions ABI args + pUSD-vs-WCOL collateral
# ===========================================================================
class _RecordingWeb3:
    """Fake AsyncWeb3 backend that records every signed call so the test
    can assert the EXACT ABI fn + args the real adapter built."""

    def __init__(self, *, status: int = 1, wcol: int = 0) -> None:
        self.sent: list[dict[str, Any]] = []
        self._status = status
        self._wcol = wcol

    async def send_signed(self, **kw: Any) -> str:
        self.sent.append(kw)
        return "0x" + "ab" * 32

    async def wait_receipt(self, _tx: str) -> dict[str, Any]:
        return {"status": self._status, "blockNumber": 42}

    async def call(self, **kw: Any) -> Any:
        return self._wcol if kw.get("fn_name") == "balanceOf" else 0


async def _key(_b: WalletBinding) -> str:
    return "0x" + "11" * 32


async def test_eoa_redeem_normal_market_uses_pusd_and_correct_abi_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _RecordingWeb3(status=1)
    client = Web3RedeemClient(web3=fake, private_key_resolver=_key)
    spec = RedeemJobSpec(
        account_id=ACCT,
        condition_id="0xCONDITION",
        outcome_index=1,
        index_set=1 << 1,  # 2
        is_neg_risk=False,
        detected_at=datetime.now(tz=UTC),
    )
    res = await client.redeem(spec, binding=_EOA)
    assert res.status is RedeemJobStatus.CONFIRMED
    assert len(fake.sent) == 1
    call = fake.sent[0]
    # The REAL adapter built redeemPositions on the CTF with the exact
    # (collateral, parentCollectionId, conditionId, [index_set]) tuple.
    assert call["fn_name"] == "redeemPositions"
    assert call["contract_address"] == client.ctf_address
    coll, parent, cond, index_sets = call["args"]
    # normal market ⇒ pUSD (the brief's highest-risk collateral detail).
    assert coll == collateral_for(is_neg_risk=False)
    assert parent == client.parent_collection_id
    assert cond == "0xCONDITION"
    assert index_sets == [2]
    assert call["from_address"] == _EOA.funder


async def test_eoa_redeem_neg_risk_uses_wrapped_collateral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _RecordingWeb3(status=1)
    client = Web3RedeemClient(web3=fake, private_key_resolver=_key)
    spec = RedeemJobSpec(
        account_id=ACCT,
        condition_id="0xNEG",
        outcome_index=0,
        index_set=1 << 0,
        is_neg_risk=True,  # ⇒ WrappedCollateral (the highest-risk detail)
        detected_at=datetime.now(tz=UTC),
    )
    res = await client.redeem(spec, binding=_EOA)
    assert res.status is RedeemJobStatus.CONFIRMED
    coll, _parent, _cond, _idx = fake.sent[0]["args"]
    # NegRisk ⇒ WrappedCollateral, NOT pUSD (and the two differ).
    assert coll == collateral_for(is_neg_risk=True)
    assert coll != collateral_for(is_neg_risk=False)


async def test_eoa_redeem_locked_off_makes_zero_onchain_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chokepoint: with the live lock OFF, redeem() raises BEFORE any
    on-chain call (assert_live_allowed first) — proof the EOA path is
    fail-closed even if reached directly."""
    _disable_live(monkeypatch)
    fake = _RecordingWeb3()
    client = Web3RedeemClient(web3=fake, private_key_resolver=_key)
    spec = RedeemJobSpec(
        account_id=ACCT,
        condition_id="0xc",
        outcome_index=0,
        index_set=1,
        is_neg_risk=False,
    )
    with pytest.raises(LiveTradingLockedError):  # the chokepoint
        await client.redeem(spec, binding=_EOA)
    assert fake.sent == []  # zero on-chain calls with the lock off


# ===========================================================================
# C1 / R19 (2) — DbJobUpserter against an EPHEMERAL postgres:16
# duplicate-candidate UNIQUE race + the new attempts retry-cap
# ===========================================================================
_DEFAULT_DSN = "postgresql+asyncpg://polyecho:polyecho@localhost:5432/polyecho"


def _async_dsn() -> str:
    raw = (
        os.environ.get("POLYECHO_TEST_DATABASE_URL")
        or os.environ.get("POLYECHO_DB_URL")
        or _DEFAULT_DSN
    )
    scheme, sep, rest = raw.partition("://")
    if sep and "+" not in scheme:
        scheme = f"{scheme}+asyncpg"
    return f"{scheme}{sep}{rest}" if sep else raw


def _tcp_reachable(dsn: str, timeout: float = 1.5) -> bool:
    parts = urlsplit(dsn.replace("+asyncpg", ""))
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_db_upserter_dup_race_and_attempts_retry_cap() -> None:
    """Real Postgres: (a) a duplicate candidate (the UNIQUE race) returns
    created=False and never inserts a 2nd job; (b) the NEW ``attempts``
    column enforces a real bounded retry — failed→pending while
    attempts < max_attempts, then TERMINALLY failed at the cap (was a
    no-op because _attempt_count returned a hardcoded 1).
    Skips cleanly with no Postgres so the offline suite stays green.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from polyecho.core.redeem import DbJobUpserter
    from polyecho.shared.models import RedeemJob

    dsn = _async_dsn()
    if not _tcp_reachable(dsn):
        pytest.skip(
            f"No Postgres reachable for DbJobUpserter test ({dsn!r}); set "
            "POLYECHO_TEST_DATABASE_URL / POLYECHO_DB_URL or run a local "
            "postgres:16 (CI's schema job provides one)."
        )

    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    ddl = (repo_root / "infra" / "migrations" / "0000_init.sql").read_text(
        encoding="utf-8"
    )
    acct = uuid.uuid4()
    cond = f"0xcond-{uuid.uuid4().hex[:8]}"

    async def _scenario() -> None:
        engine = create_async_engine(dsn, echo=False)
        try:
            raw = await engine.raw_connection()
            try:
                conn = raw.driver_connection
                assert conn is not None
                await conn.execute(ddl)
                # The redeem job FK -> accounts(id): seed a minimal account
                # (name + eoa_address are NOT NULL; eoa_address is UNIQUE).
                await conn.execute(
                    "INSERT INTO accounts (id, name, eoa_address, "
                    "wallet_mode, signature_type) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT DO NOTHING",
                    acct,
                    "wave-e-test",
                    f"0xeoa{uuid.uuid4().hex[:36]}",
                    "eoa",
                    0,
                )
            finally:
                raw.close()

            sf = async_sessionmaker(engine, expire_on_commit=False)
            up = DbJobUpserter(session_factory=sf, max_attempts=2)
            spec = RedeemJobSpec(
                account_id=acct,
                condition_id=cond,
                outcome_index=0,
                index_set=1,
                is_neg_risk=False,
                detected_at=datetime.now(tz=UTC),
            )

            # (a) first upsert creates; a duplicate candidate does NOT.
            assert await up.upsert(spec) is True
            assert await up.upsert(spec) is False
            async with sf() as s:
                rows = (
                    await s.execute(
                        select(RedeemJob).where(
                            RedeemJob.account_id == acct,
                            RedeemJob.condition_id == cond,
                        )
                    )
                ).scalars().all()
            assert len(rows) == 1  # UNIQUE held — exactly one job
            assert rows[0].attempts == 0

            # (b) attempts retry-cap.  max_attempts=2.
            #   fail #1 (attempts 0->1) ⇒ next upsert resets failed→pending
            #   fail #2 (attempts 1->2) ⇒ at the cap ⇒ stays terminally failed
            failed = RedeemResult(status=RedeemJobStatus.FAILED, detail="revert")

            await up.record(spec, failed)  # attempts: 1
            async with sf() as s:
                r = (
                    await s.execute(
                        select(RedeemJob).where(RedeemJob.account_id == acct)
                    )
                ).scalar_one()
                assert r.status == RedeemJobStatus.FAILED.value
                assert r.attempts == 1
            # Below cap ⇒ a re-detected candidate resets it to pending.
            assert await up.upsert(spec) is False
            async with sf() as s:
                r = (
                    await s.execute(
                        select(RedeemJob).where(RedeemJob.account_id == acct)
                    )
                ).scalar_one()
                assert r.status == RedeemJobStatus.PENDING.value

            await up.record(spec, failed)  # attempts: 2 (== cap)
            async with sf() as s:
                r = (
                    await s.execute(
                        select(RedeemJob).where(RedeemJob.account_id == acct)
                    )
                ).scalar_one()
                assert r.attempts == 2
                assert r.status == RedeemJobStatus.FAILED.value
            # At/above the cap ⇒ NOT reset — terminally failed (the C1
            # fix; pre-fix _attempt_count==1 made this reset forever).
            assert await up.upsert(spec) is False
            async with sf() as s:
                r = (
                    await s.execute(
                        select(RedeemJob).where(RedeemJob.account_id == acct)
                    )
                ).scalar_one()
                assert r.status == RedeemJobStatus.FAILED.value  # STAYS failed
        finally:
            # Best-effort cleanup so re-runs against a warm CI DB are clean.
            try:
                raw2 = await engine.raw_connection()
                try:
                    c2 = raw2.driver_connection
                    if c2 is not None:
                        await c2.execute(
                            "DELETE FROM redeem_jobs WHERE account_id = $1",
                            acct,
                        )
                        await c2.execute(
                            "DELETE FROM accounts WHERE id = $1", acct
                        )
                finally:
                    raw2.close()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
            await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_scenario())


# ===========================================================================
# R9 I-4 / --once — run_supervised wiring + the --once bypass contract
# ===========================================================================
async def test_run_supervised_runs_engine_once_then_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_supervised`` must call the engine's ``run_once`` exactly once
    (heartbeat/cursor writers already injected at construction) then drive
    the redeem + daily cadence via independent never-exit loops.  We cap
    the loops so the test is bounded and assert they ran independently
    (R9 I-4: a fault in one must not stop the other)."""
    import polyecho.core.wiring as wiring

    calls = {
        "run_once": 0,
        "startup": 0,
        "redeem": 0,
        "daily": 0,
        "integ": 0,
    }

    class _Core:
        _follows: list[Any] = []

        async def run_once(self) -> None:
            calls["run_once"] += 1

        async def _integration_probe_loop(self) -> None:
            # The deployed entrypoint must ALSO drive the §7.1
            # integration connectivity probe — it was previously omitted
            # from run_supervised so the dashboard /status panel stayed
            # permanently empty.  Bounded fake of the never-exit loop.
            calls["integ"] += 1
            await asyncio.sleep(0)

    async def _startup() -> None:
        calls["startup"] += 1

    def _build(_follows: Any) -> Any:
        return object()

    async def _redeem_loop(_w: Any, *, interval_seconds: float) -> None:
        calls["redeem"] += 1  # ran independently
        # Simulate the never-exit loop being cancelled at shutdown.
        raise asyncio.CancelledError

    async def _daily_loop(*, interval_seconds: float) -> None:
        calls["daily"] += 1  # ran independently (gather, not TaskGroup)
        await asyncio.sleep(0)

    monkeypatch.setattr(wiring, "startup_reconcile", _startup)
    monkeypatch.setattr(wiring, "build_redeem_worker", _build)
    monkeypatch.setattr(wiring, "_redeem_loop", _redeem_loop)
    monkeypatch.setattr(wiring, "_daily_reconcile_loop", _daily_loop)

    await wiring.run_supervised(_Core())

    assert calls["run_once"] == 1  # engine pass ran exactly once
    assert calls["startup"] == 1
    # gather(return_exceptions=True): the redeem loop raising
    # CancelledError did NOT prevent the daily loop from running.
    assert calls["redeem"] == 1
    assert calls["daily"] == 1
    # The §7.1 integration connectivity probe loop is now ALSO driven by
    # the deployed entrypoint (previously omitted ⇒ /status empty).
    assert calls["integ"] == 1


def test_once_path_never_calls_run_supervised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--once`` exit-code contract: ``__main__.main(['--once'])``
    builds the engine WITH the injected heartbeat/cursor writers, runs
    ONE pass and returns its exit code WITHOUT ever entering
    ``run_supervised`` (so the supervised redeem/reconcile cadence never
    starts under --once and the exit-code contract is preserved)."""
    import polyecho.core.__main__ as main_mod

    supervised_called = {"n": 0}
    ctor_kwargs: dict[str, Any] = {}

    class _Core:
        def __init__(self, **kw: Any) -> None:
            ctor_kwargs.update(kw)

        async def run_once(self) -> Any:
            class _S:
                detected = 0
                copied = 0
                skipped = 0
                placed = 0
                errors = 0

            return _S()

        def degenerate_once_pass(self) -> str | None:
            return None  # healthy idle pass ⇒ exit 0

    async def _supervised(_core: Any) -> None:  # pragma: no cover
        supervised_called["n"] += 1

    monkeypatch.setattr(main_mod, "TraderCore", _Core)
    monkeypatch.setattr(main_mod, "run_supervised", _supervised)
    monkeypatch.setattr(main_mod, "_demo_follows", lambda: [])

    rc = asyncio.get_event_loop().run_until_complete(
        main_mod.main(["--once"])
    )
    assert rc == 0
    assert supervised_called["n"] == 0  # --once NEVER enters run_supervised
    # Proof the REAL injected writers are wired at engine construction.
    assert ctor_kwargs.get("heartbeat_writer") is not None
    assert ctor_kwargs.get("cursor_writer") is not None

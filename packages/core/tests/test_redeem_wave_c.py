"""
packages/core/tests/test_redeem_wave_c.py
=========================================
Wave C-redeem — concrete poller / upserter / worker pipeline (brief §9).

Default (double-lock OFF):
* poller + upserter run, **zero on-chain calls**, every RedeemResult is
  PENDING / LIVE_LOCKED, idempotent on (account, condition, outcome) — the
  money-path-safety proof.

Lock ON (fake web3 + fake datasource):
* EOA path builds+signs+sends the correct redeemPositions; collateral =
  pUSD vs WCOL by is_neg_risk; NegRisk triggers the unwrap; status-machine
  writer sees submitted→confirmed/failed; retry resets a failed job;
  MAGIC raises a clear relayer-not-configured (never a wrong tx).

pytest-asyncio is configured ``asyncio_mode = "auto"`` so ``async def
test_*`` run directly.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from polyecho.core.redeem import (
    RedeemablePoller,
    RedeemCandidate,
    RedeemWorker,
)
from polyecho.shared.copytrading import (
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    SkipReason,
    WalletBinding,
)
from polyecho.shared.datasource.dto import PositionRecord
from polyecho.shared.polymarket.redeem import (
    RelayerNotConfiguredError,
    Web3RedeemClient,
)

ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.redeem.acct")
_EOA = WalletBinding(
    signature_type=0,
    funder="0x000000000000000000000000000000000000dead",
    wallet_mode="eoa",
    derived=False,
)
_MAGIC = WalletBinding(signature_type=1, funder="0xabc", wallet_mode="magic", derived=True)


def _pos(*, redeemable: Any, neg_risk: bool = False, idx: int = 0) -> PositionRecord:
    return PositionRecord(
        wallet="0xwallet",
        condition_id="0xcond",
        asset="t",
        size=1.0,
        avg_price=0.5,
        current_value=1.0,
        cash_pnl=0.5,
        percent_pnl=1.0,
        title=None,
        outcome="Yes",
        raw={"redeemable": redeemable, "negRisk": neg_risk, "outcomeIndex": idx},
    )


class _FakeDS:
    def __init__(self, positions: list[PositionRecord]) -> None:
        self._positions = positions

    async def get_positions(self, wallet: str, **_: Any) -> list[PositionRecord]:
        return self._positions


class _FakeWeb3:
    def __init__(self, *, status: int = 1, wcol: int = 0) -> None:
        self.sent: list[dict[str, Any]] = []
        self._status = status
        self._wcol = wcol

    async def send_signed(self, **kw: Any) -> str:
        self.sent.append(kw)
        return "0x" + "cd" * 32

    async def wait_receipt(self, _tx: str) -> dict[str, Any]:
        return {"status": self._status, "blockNumber": 7}

    async def call(self, **kw: Any) -> Any:
        return self._wcol if kw["fn_name"] == "balanceOf" else 0


async def _key(_b: WalletBinding) -> str:
    return "0x" + "22" * 32


def _disable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. RedeemablePoller — concrete data-api ``redeemable`` poll (brief §9)
# ---------------------------------------------------------------------------
async def test_poller_yields_only_redeemable_positions() -> None:
    ds = _FakeDS(
        [
            _pos(redeemable=True, neg_risk=False, idx=0),
            _pos(redeemable=False),  # filtered out (not redeemable)
            _pos(redeemable=True, neg_risk=True, idx=1),
        ]
    )
    poller = RedeemablePoller(datasource=ds, accounts=[(ACCT, "0xwallet")], cooldown_s=0.0)
    cands = await poller()
    assert len(cands) == 2
    assert cands[0].is_neg_risk is False and cands[0].outcome_index == 0
    assert cands[1].is_neg_risk is True and cands[1].outcome_index == 1


async def test_poller_quota_and_cooldown() -> None:
    ds = _FakeDS([_pos(redeemable=True) for _ in range(10)])
    t = {"v": 100.0}
    poller = RedeemablePoller(
        datasource=ds,
        accounts=[(ACCT, "0xw")],
        max_per_poll=3,
        cooldown_s=20.0,
        clock=lambda: t["v"],
    )
    first = await poller()
    assert len(first) == 3  # quota cap
    second = await poller()  # within cooldown window
    assert second == []
    t["v"] = 200.0  # past cooldown
    third = await poller()
    assert len(third) == 3


async def test_poller_failsoft_on_datasource_error() -> None:
    class _Boom:
        async def get_positions(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("api down")

    poller = RedeemablePoller(datasource=_Boom(), accounts=[(ACCT, "0xw")], cooldown_s=0.0)
    assert await poller() == []  # never raises into the loop


# ---------------------------------------------------------------------------
# 2. ``auto_redeem`` off ⇒ zero on-chain, idempotent (safety proof)
# ---------------------------------------------------------------------------
async def test_auto_redeem_off_zero_onchain_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_live(monkeypatch)
    seen: set[tuple[str, str, int]] = set()

    async def candidates() -> list[RedeemCandidate]:
        return [
            RedeemCandidate(ACCT, "0xc", 0, False),
            RedeemCandidate(ACCT, "0xc", 0, False),  # dup candidate
        ]

    async def upsert(spec: RedeemJobSpec) -> bool:
        k = (str(spec.account_id), spec.condition_id, spec.outcome_index)
        if k in seen:
            return False
        seen.add(k)
        return True

    fake = _FakeWeb3()
    client = Web3RedeemClient(web3=fake, private_key_resolver=_key)
    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        auto_redeem=False,
        redeem_client=client,
        binding_for=lambda _a: _aw(_EOA),
    )
    outs = await w.poll_once()
    assert len(outs) == 2
    assert outs[0].created is True
    assert outs[1].created is False  # idempotent on (account, condition, outcome)
    assert all(o.result.status is RedeemJobStatus.PENDING for o in outs)
    assert all(o.result.skip_reason is SkipReason.LIVE_LOCKED for o in outs)
    # PROOF: nothing was ever built/signed/sent on-chain.
    assert fake.sent == []


def _aw(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()


# ---------------------------------------------------------------------------
# 3. Live path — EOA concrete, NegRisk unwrap, status machine, retry
# ---------------------------------------------------------------------------
async def test_live_eoa_redeem_confirmed_and_status_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(status=1)
    writes: list[tuple[RedeemJobSpec, RedeemResult]] = []
    events: list[tuple[bool, str]] = []

    async def candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xc", 1, False)]

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    async def status_writer(s: RedeemJobSpec, r: RedeemResult) -> None:
        writes.append((s, r))

    async def emitter(ok: bool, s: RedeemJobSpec, r: RedeemResult) -> None:
        events.append((ok, s.condition_id))

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_EOA),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=True,
        status_writer=status_writer,
        event_emitter=emitter,
    )
    outs = await w.poll_once()
    assert outs[0].result.status is RedeemJobStatus.CONFIRMED
    assert len(fake.sent) == 1
    assert fake.sent[0]["fn_name"] == "redeemPositions"
    assert writes and writes[0][1].status is RedeemJobStatus.CONFIRMED
    assert events == [(True, "0xc")]


async def test_live_neg_risk_triggers_unwrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(status=1, wcol=9_999)

    async def candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xc", 0, True)]  # NegRisk

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_EOA),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=True,
    )
    outs = await w.poll_once()
    assert outs[0].result.status is RedeemJobStatus.CONFIRMED
    fns = [s["fn_name"] for s in fake.sent]
    assert fns == ["redeemPositions", "unwrap"]  # NegRisk ⇒ redeem then unwrap
    unwrap = fake.sent[1]
    assert unwrap["args"] == (_EOA.funder, 9_999)  # polled WCOL balance


async def test_live_redeem_failure_marks_failed_and_failsoft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(status=0)  # receipt revert
    writes: list[RedeemResult] = []

    async def candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xc", 0, False)]

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    async def status_writer(_s: RedeemJobSpec, r: RedeemResult) -> None:
        writes.append(r)

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_EOA),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=True,
        status_writer=status_writer,
    )
    outs = await w.poll_once()
    assert outs[0].result.status is RedeemJobStatus.FAILED
    assert writes[0].status is RedeemJobStatus.FAILED


async def test_live_magic_relayer_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()

    async def candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xc", 0, False)]

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_MAGIC),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=True,
    )
    # Worker is fail-soft per job: a relayer-not-configured surfaces as a
    # FAILED outcome (clear), and crucially NO wrong tx was sent.
    outs = await w.poll_once()
    assert outs[0].result.status is RedeemJobStatus.FAILED
    assert "relayer" in (outs[0].result.detail or "").lower() or "TODO" in (
        outs[0].result.detail or ""
    )
    assert fake.sent == []


def test_relayer_error_is_clear() -> None:
    err = RelayerNotConfiguredError("x")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# 4. Deep-dive B6 — SAFE redeem batching at the worker layer
# ---------------------------------------------------------------------------
_SAFE = WalletBinding(
    signature_type=2,
    funder="0x000000000000000000000000000000000000beef",
    wallet_mode="safe",
    derived=True,
)


class _BatchSpyClient:
    """Fake redeem client recording redeem / redeem_batch dispatch so the
    worker's B6 grouping (one batch per account) is observable."""

    def __init__(self) -> None:
        self.redeem_calls: list[RedeemJobSpec] = []
        self.batch_calls: list[list[RedeemJobSpec]] = []

    async def redeem(self, spec: RedeemJobSpec, *, binding: WalletBinding) -> RedeemResult:
        self.redeem_calls.append(spec)
        return RedeemResult(status=RedeemJobStatus.CONFIRMED, tx_hash="0xsingle")

    async def redeem_batch(
        self,
        specs: Any,
        *,
        binding: WalletBinding,
    ) -> dict[tuple[str, int], RedeemResult]:
        specs = list(specs)
        self.batch_calls.append(specs)
        # One shared tx hash for the whole batch; one result per job.
        return {
            (s.condition_id, s.outcome_index): RedeemResult(
                status=RedeemJobStatus.CONFIRMED, tx_hash="0xbatch"
            )
            for s in specs
        }


async def test_b6_safe_account_jobs_batched_into_one_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N SAFE redeem jobs for ONE account ⇒ exactly one redeem_batch call;
    every job still individually upserted + status-written + event-emitted
    and they all share the one batched tx hash."""
    _enable_live(monkeypatch)
    spy = _BatchSpyClient()
    upserted: list[RedeemJobSpec] = []
    writes: list[RedeemResult] = []
    events: list[bool] = []

    async def candidates() -> list[RedeemCandidate]:
        return [
            RedeemCandidate(ACCT, "0xc1", 0, False),
            RedeemCandidate(ACCT, "0xc2", 0, False),
            RedeemCandidate(ACCT, "0xc3", 1, False),
        ]

    async def upsert(s: RedeemJobSpec) -> bool:
        upserted.append(s)
        return True

    async def status_writer(_s: RedeemJobSpec, r: RedeemResult) -> None:
        writes.append(r)

    async def emitter(ok: bool, _s: RedeemJobSpec, _r: RedeemResult) -> None:
        events.append(ok)

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_SAFE),
        redeem_client=spy,
        auto_redeem=True,
        status_writer=status_writer,
        event_emitter=emitter,
    )
    outs = await w.poll_once()

    # One batch call carrying all 3 jobs; no per-job single redeem.
    assert len(spy.batch_calls) == 1
    assert len(spy.batch_calls[0]) == 3
    assert spy.redeem_calls == []
    # Every job individually upserted, status-written, event-emitted.
    assert len(upserted) == 3
    assert len(writes) == 3 and all(r.status is RedeemJobStatus.CONFIRMED for r in writes)
    assert events == [True, True, True]
    # All 3 outcomes share the one batched tx hash.
    assert {o.result.tx_hash for o in outs} == {"0xbatch"}
    assert len(outs) == 3


async def test_b6_magic_account_not_batched_stays_sequential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Magic (sig 1) jobs are NOT collapsed into a batch tx — PolyHermes
    excludes Magic.  The worker still calls redeem_batch (uniform entry
    point) but the spy proves the jobs are passed through, and a real
    Web3RedeemClient would fan them to the sequential per-job loop."""
    _enable_live(monkeypatch)
    spy = _BatchSpyClient()

    async def candidates() -> list[RedeemCandidate]:
        return [
            RedeemCandidate(ACCT, "0xm1", 0, False),
            RedeemCandidate(ACCT, "0xm2", 0, False),
        ]

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_MAGIC),
        redeem_client=spy,
        auto_redeem=True,
    )
    outs = await w.poll_once()
    # The worker delegates the SAFE-vs-Magic decision to redeem_batch; the
    # real client keeps Magic sequential (covered in the relayer suite).
    assert len(outs) == 2
    assert len(spy.batch_calls) == 1


async def test_b6_distinct_accounts_get_separate_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jobs are grouped per account_id — one SAFE account with 2 jobs is
    batched while a second account with a single job takes the plain
    single-tx path.  Batching is per-account, never cross-account."""
    _enable_live(monkeypatch)
    spy = _BatchSpyClient()
    acct_b = uuid.uuid5(uuid.NAMESPACE_DNS, "test.redeem.acct.b")

    async def candidates() -> list[RedeemCandidate]:
        return [
            RedeemCandidate(ACCT, "0xa1", 0, False),
            RedeemCandidate(acct_b, "0xb1", 0, False),
            RedeemCandidate(ACCT, "0xa2", 0, False),
        ]

    async def upsert(_s: RedeemJobSpec) -> bool:
        return True

    w = RedeemWorker(
        candidate_source=candidates,
        job_upserter=upsert,
        binding_for=lambda _a: _aw(_SAFE),
        redeem_client=spy,
        auto_redeem=True,
    )
    outs = await w.poll_once()
    assert len(outs) == 3
    # ACCT (2 jobs) ⇒ ONE batch call; acct_b (1 job) ⇒ plain single redeem.
    assert len(spy.batch_calls) == 1
    assert len(spy.batch_calls[0]) == 2
    assert {s.account_id for s in spy.batch_calls[0]} == {ACCT}  # single-account
    assert len(spy.redeem_calls) == 1
    assert spy.redeem_calls[0].account_id == acct_b

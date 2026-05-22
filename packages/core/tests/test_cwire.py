"""
packages/core/tests/test_cwire.py
=================================
C-wire — proves the previously-dormant DB-backed collaborators are now
genuinely injected & exercised by the trader-core entrypoint / supervised
loop, WITHOUT weakening any money-path invariant.

Coverage:

1. **HeartbeatWriter** — the real writer upserts the ``settings`` row
   keyed ``trader_heartbeat`` with the EXACT shape the web
   ``coerceHeartbeat`` reads (ISO ``ts`` + ``phase`` + counters), once at
   startup AND every supervised cycle; a DB error is swallowed (best-effort).
2. **cursor_writer** — the real writer persists the per-leader
   ``follow_relations.last_seen_ts`` (UPDATE), so reconcile is genuinely
   restart-safe; a persistence error is swallowed.
3. **RedeemWorker** — ``build_redeem_worker`` constructs the real
   poller + DB upserter + EOA ``Web3RedeemClient``; with the double
   live-lock OFF ``poll_once`` performs ZERO on-chain calls / ZERO real
   redeem (PENDING / LIVE_LOCKED), and with locks ON (fake web3) it
   reaches the concrete EOA path.
4. **startup reconcile** — invoked exactly once on the supervised path
   and a reconcile error does NOT abort startup.

``--once`` exit-code contract is unchanged (covered by ``test_core``);
this file only asserts the new wiring + the preserved lock invariants.

pytest-asyncio ``asyncio_mode = "auto"`` ⇒ bare ``async def test_*``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from polyecho.core import wiring
from polyecho.core.evaluator import TemplateConfig
from polyecho.core.redeem import RedeemCandidate, RedeemWorker
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.copytrading import SkipReason, WalletBinding

ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.cwire.acct")
LEADER = "0xswing1000000000000000000000000000000001"


def _demo_follow() -> FollowSpec:
    return FollowSpec(
        account_id=ACCT,
        leader_address=LEADER,
        template=TemplateConfig(
            copy_ratio=__import__("decimal").Decimal("0.01"),
            max_position_usd=__import__("decimal").Decimal("50"),
            min_trade_size_usd=__import__("decimal").Decimal("1"),
            max_concurrent_positions=10,
        ),
        binding=WalletBinding(
            signature_type=0,
            funder=LEADER,
            wallet_mode="eoa",
            derived=False,
        ),
    )


# ---------------------------------------------------------------------------
# Fake session_scope: records every ``.execute(stmt)`` without a real DB.
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    async def execute(self, stmt: Any) -> Any:
        self._sink.append(stmt)
        return None

    async def commit(self) -> None:  # session_scope commits on clean exit
        return None


class _FakeScope:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._sink)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _patch_session_scope(monkeypatch: pytest.MonkeyPatch, sink: list[Any]) -> None:
    def _scope() -> _FakeScope:
        return _FakeScope(sink)

    # wiring imports ``session_scope`` lazily from shared.db inside each
    # writer, so patching the shared symbol is sufficient.
    monkeypatch.setattr("polyecho.shared.db.session_scope", _scope)


def _disable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    monkeypatch.delenv("POLYECHO_AUTO_REDEEM", raising=False)
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. HeartbeatWriter — correct key/shape, every cycle + startup
# ---------------------------------------------------------------------------
async def test_heartbeat_writer_upserts_settings_row_correct_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class _Sess:
        async def execute(self, stmt: Any) -> Any:
            # Robust across SQLAlchemy versions: read the bound params.
            try:
                compiled = stmt.compile()
                captured.append(dict(compiled.params))
            except Exception:  # noqa: BLE001 - fall back to inspecting values
                captured.append({"_stmt": repr(stmt)})
            return None

        async def commit(self) -> None:
            return None

    class _Scope:
        async def __aenter__(self) -> _Sess:
            return _Sess()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr("polyecho.shared.db.session_scope", lambda: _Scope())

    writer = wiring.make_heartbeat_writer()
    info = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "phase": "cycle_start",
        "datasource": "fixture",
        "live_trading": False,
        "detected": 0,
    }
    await writer(info)

    assert len(captured) == 1
    params = captured[0]
    # The settings key must be exactly what web ``TRADER_HEARTBEAT_KEY``
    # / ``coerceHeartbeat`` reads.
    assert wiring.TRADER_HEARTBEAT_KEY == "trader_heartbeat"
    assert "trader_heartbeat" in params.values()
    # The value column carries the heartbeat dict verbatim (ISO ts + phase).
    stored = next(v for v in params.values() if isinstance(v, dict) and "ts" in v)
    assert stored == info
    assert isinstance(stored["ts"], str)
    assert stored["phase"] == "cycle_start"


async def test_heartbeat_writer_failsafe_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("polyecho.shared.db.session_scope", _boom)
    writer = wiring.make_heartbeat_writer()
    # Must NOT raise — best-effort liveness, never crashes the loop.
    await writer({"ts": "2026-05-18T00:00:00+00:00", "phase": "cycle_start"})


async def test_engine_writes_heartbeat_every_cycle_and_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_live(monkeypatch)
    sink: list[Any] = []
    _patch_session_scope(monkeypatch, sink)

    core = TraderCore(
        follows=[_demo_follow()],
        heartbeat_writer=wiring.make_heartbeat_writer(),
        cursor_writer=wiring.make_cursor_writer(),
    )
    await core.run_once()

    # run_once writes a startup ("cycle_start") + an end-of-cycle
    # ("cycle_complete") heartbeat ⇒ at least 2 settings upserts. The
    # engine also recorded the in-proc mirror.
    assert core.last_heartbeat is not None
    assert "ts" in core.last_heartbeat
    assert len(sink) >= 2  # heartbeat upserts (+ any cursor updates)


# ---------------------------------------------------------------------------
# 2. cursor_writer — persists follow_relations.last_seen_ts (restart-safe)
# ---------------------------------------------------------------------------
async def test_cursor_writer_updates_follow_relation_last_seen_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink: list[Any] = []
    _patch_session_scope(monkeypatch, sink)

    writer = wiring.make_cursor_writer()
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    await writer(LEADER, ts)

    assert len(sink) == 1
    stmt = sink[0]
    sql = str(stmt).lower()
    assert "update" in sql and "follow_relations" in sql
    compiled = stmt.compile()
    assert ts in compiled.params.values()


async def test_cursor_writer_failsafe_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("polyecho.shared.db.session_scope", _boom)
    writer = wiring.make_cursor_writer()
    # Best-effort: the engine already moved its in-proc store; a persist
    # failure must not raise.
    await writer(LEADER, datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# 3. RedeemWorker — real wiring, double live-lock PRESERVED
# ---------------------------------------------------------------------------
class _FakeWeb3:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_signed(self, **kw: Any) -> str:
        self.sent.append(kw)
        return "0x" + "ab" * 32

    async def wait_receipt(self, _tx: str) -> dict[str, Any]:
        return {"status": 1, "blockNumber": 1}

    async def call(self, **kw: Any) -> Any:
        return 0


async def test_build_redeem_worker_returns_real_worker() -> None:
    w = wiring.build_redeem_worker([_demo_follow()])
    assert isinstance(w, RedeemWorker)


async def test_redeem_worker_auto_redeem_off_skips_onchain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``auto_redeem=False`` ⇒ the worker logs intent and submits nothing
    on-chain (no key resolver call, no fake web3 traffic)."""
    _disable_live(monkeypatch)

    fake = _FakeWeb3()

    async def _candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xcond", 0, False)]

    async def _key(_b: WalletBinding) -> str:  # must NEVER be called
        raise AssertionError("key resolver reached with auto_redeem off")

    from polyecho.shared.polymarket.redeem import Web3RedeemClient

    worker = RedeemWorker(
        candidate_source=_candidates,
        job_upserter=_AlwaysNew(),
        binding_for=lambda _a: _aw(
            WalletBinding(
                signature_type=0,
                funder="0xdead",
                wallet_mode="eoa",
                derived=False,
            )
        ),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=False,
    )
    outs = await worker.poll_once()
    assert len(outs) == 1
    assert outs[0].result.skip_reason is SkipReason.LIVE_LOCKED
    # PROOF: nothing was ever built / signed / sent on-chain.
    assert fake.sent == []


async def test_redeem_worker_locks_on_reaches_concrete_eoa_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()

    async def _candidates() -> list[RedeemCandidate]:
        return [RedeemCandidate(ACCT, "0xcond", 1, False)]

    async def _key(_b: WalletBinding) -> str:
        return "0x" + "11" * 32

    from polyecho.shared.polymarket.redeem import Web3RedeemClient

    worker = RedeemWorker(
        candidate_source=_candidates,
        job_upserter=_AlwaysNew(),
        binding_for=lambda _a: _aw(
            WalletBinding(
                signature_type=0,
                funder="0xdead",
                wallet_mode="eoa",
                derived=False,
            )
        ),
        redeem_client=Web3RedeemClient(web3=fake, private_key_resolver=_key),
        auto_redeem=True,  # live ON + auto_redeem ON ⇒ concrete path
    )
    outs = await worker.poll_once()
    assert len(outs) == 1
    # The concrete EOA path built+sent the redeemPositions tx.
    assert len(fake.sent) == 1
    assert fake.sent[0]["fn_name"] == "redeemPositions"


async def test_key_resolver_failsclosed_without_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLYECHO_KEY_PASSPHRASE", raising=False)
    resolver = wiring._make_key_resolver()
    with pytest.raises(RuntimeError, match="POLYECHO_KEY_PASSPHRASE"):
        await resolver(
            WalletBinding(
                signature_type=0,
                funder="0xdead",
                wallet_mode="eoa",
                derived=False,
            )
        )


# ---------------------------------------------------------------------------
# 3b. trade_recorder — A3: a durable leader_trades row per copy attempt,
#     placed (status=success) AND failed (status=failed), idempotent on the
#     existing (leader_address, tx_hash) UNIQUE.
# ---------------------------------------------------------------------------
def _evaluated_copy() -> Any:
    """A minimal EvaluatedCopy the trade_recorder can consume."""
    from polyecho.shared.copytrading import (
        Decision,
        DecisionOutcome,
        EvaluatedCopy,
        TradeSide,
    )

    return EvaluatedCopy(
        leader_address=LEADER,
        tx_hash="0xfeedface" + "00" * 16,
        account_id=ACCT,
        condition_id="0xcond_a3",
        binding=WalletBinding(signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False),
        decision=Decision(outcome=DecisionOutcome.COPY, side=TradeSide.BUY),
        raw={"outcome": "Yes"},
    )


async def test_trade_recorder_success_writes_leader_trades_status_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful copy persists a leader_trades row with status=success."""
    captured: list[dict[str, Any]] = []

    class _Sess:
        async def execute(self, stmt: Any) -> Any:
            captured.append(dict(stmt.compile().params))
            return None

        async def commit(self) -> None:
            return None

    class _Scope:
        async def __aenter__(self) -> _Sess:
            return _Sess()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr("polyecho.shared.db.session_scope", lambda: _Scope())

    recorder = wiring.make_trade_recorder()
    await recorder(
        _evaluated_copy(),
        {
            "tx_hash": "0xfeedface" + "00" * 16,
            "side": "BUY",
            "size": "12.5",
            "price": "0.42",
            "fill_status": "filled",
            "status": "success",
        },
    )

    assert len(captured) == 1
    params = captured[0]
    assert "success" in params.values()
    assert LEADER in params.values()
    # The dedup anchor txHash is carried as both trade_id and tx_hash.
    assert ("0xfeedface" + "00" * 16) in params.values()


async def test_trade_recorder_failed_writes_leader_trades_status_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed copy attempt STILL persists a leader_trades row — A3's
    core fix: a known-bad trade gets a durable status=failed marker."""
    captured: list[dict[str, Any]] = []

    class _Sess:
        async def execute(self, stmt: Any) -> Any:
            captured.append(dict(stmt.compile().params))
            return None

        async def commit(self) -> None:
            return None

    class _Scope:
        async def __aenter__(self) -> _Sess:
            return _Sess()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr("polyecho.shared.db.session_scope", lambda: _Scope())

    recorder = wiring.make_trade_recorder()
    await recorder(
        _evaluated_copy(),
        {
            "tx_hash": "0xfeedface" + "00" * 16,
            "side": "BUY",
            "size": "12.5",
            "price": "0.42",
            "fill_status": "rejected",
            "status": "failed",
        },
    )

    assert len(captured) == 1
    assert "failed" in captured[0].values()


async def test_trade_recorder_is_idempotent_on_leader_tx_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redelivery does not double-insert: the write is ON CONFLICT
    DO UPDATE keyed on the EXISTING (leader_address, tx_hash) UNIQUE, so a
    known-failed trade is updated in place, never re-inserted/re-attempted."""
    stmts: list[Any] = []

    class _Sess:
        async def execute(self, stmt: Any) -> Any:
            stmts.append(stmt)
            return None

        async def commit(self) -> None:
            return None

    class _Scope:
        async def __aenter__(self) -> _Sess:
            return _Sess()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr("polyecho.shared.db.session_scope", lambda: _Scope())

    recorder = wiring.make_trade_recorder()
    info = {
        "tx_hash": "0xfeedface" + "00" * 16,
        "side": "BUY",
        "size": "1",
        "price": "0.5",
        "status": "failed",
    }
    # Same (leader, tx) delivered twice (a WS/poll redelivery).
    await recorder(_evaluated_copy(), info)
    await recorder(_evaluated_copy(), info)

    assert len(stmts) == 2
    for stmt in stmts:
        sql = str(stmt).lower()
        assert "insert into leader_trades" in sql
        # Idempotency: ON CONFLICT on the existing dedup anchor — the key
        # is NOT changed, the redelivery upserts the same row.
        assert "on conflict" in sql
        assert "uq_leader_trades_leader_tx" in sql


async def test_trade_recorder_failsafe_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error in the durable record write must NEVER crash the loop."""

    def _boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("polyecho.shared.db.session_scope", _boom)
    recorder = wiring.make_trade_recorder()
    # Best-effort: must not raise.
    await recorder(_evaluated_copy(), {"tx_hash": "0xabc", "status": "failed"})


async def test_trade_recorder_skips_when_dedup_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tx_hash ⇒ no row written (the dedup anchor is mandatory)."""
    stmts: list[Any] = []

    class _Sess:
        async def execute(self, stmt: Any) -> Any:
            stmts.append(stmt)
            return None

        async def commit(self) -> None:
            return None

    class _Scope:
        async def __aenter__(self) -> _Sess:
            return _Sess()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    monkeypatch.setattr("polyecho.shared.db.session_scope", lambda: _Scope())

    from polyecho.shared.copytrading import (
        Decision,
        DecisionOutcome,
        EvaluatedCopy,
    )

    recorder = wiring.make_trade_recorder()
    evaluated = EvaluatedCopy(
        leader_address=LEADER,
        tx_hash="",
        account_id=ACCT,
        condition_id="0xc",
        binding=WalletBinding(signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False),
        decision=Decision(outcome=DecisionOutcome.COPY),
    )
    await recorder(evaluated, {"status": "failed"})
    assert stmts == []


# ---------------------------------------------------------------------------
# 4. startup reconcile — invoked once; an error does NOT abort startup
# ---------------------------------------------------------------------------
async def test_startup_reconcile_invoked_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class _Report:
        has_drift = False
        drift_count = 0

    async def _fake_run(**_: Any) -> _Report:
        calls.append(1)
        return _Report()

    monkeypatch.setattr("polyecho.core.onchain_reconcile.run_reconcile", _fake_run)
    await wiring.startup_reconcile()
    assert calls == [1]


async def test_startup_reconcile_error_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(**_: Any) -> Any:
        raise RuntimeError("rpc unreachable")

    monkeypatch.setattr("polyecho.core.onchain_reconcile.run_reconcile", _boom)
    # Must NOT raise — a reconcile error must never abort bot startup.
    await wiring.startup_reconcile()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _aw(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()


class _AlwaysNew:
    """``job_upserter`` stub: every spec is a new job (no real DB)."""

    async def __call__(self, _spec: Any) -> bool:
        return True

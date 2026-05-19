"""
polyecho.core trader-core engine tests (Phase-4 Wave-2).

Fixture-first, NO network, NO real funds, NO DB (all collaborators are
injected / in-memory).  Proves:

* cross-source dedup on ``tx_hash`` (same tx twice ⇒ one process),
* every risk gate + the 7 PolyEcho-over-PolyHermes safety improvements,
* FIFO sell-match accounting,
* the HARD live lock: default-off ⇒ ZERO placement (asserted),
* reconcile backfill (WS-gap closure),
* redeem idempotency / live-lock,
* ``python -m polyecho.core --once`` exits 0,
* ``import polyecho.core`` succeeds.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from polyecho.core.detection import (
    ChainCrossCheckListener,
    DetectedTrade,
    TradeDeduplicator,
    UserChannelListener,
    synthetic_tx_hash,
)
from polyecho.core.evaluator import TemplateConfig, TraderCopyEvaluator
from polyecho.core.execution import BuyLot, FifoLedger, OrderExecutor
from polyecho.core.reconcile import Reconciler
from polyecho.core.redeem import RedeemCandidate, RedeemWorker
from polyecho.core.risk import LayeredRiskGate, RiskContext
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    SkipReason,
    TradeSide,
    WalletBinding,
)
from polyecho.shared.datasource.dto import ActivityRecord

LEADER = "0xswing1000000000000000000000000000000001"
ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.acct")
BINDING = WalletBinding(signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False)


def _tpl(**kw: object) -> TemplateConfig:
    base: dict[str, object] = dict(
        copy_ratio=Decimal("0.1"),
        max_position_usd=Decimal("1000"),
        min_trade_size_usd=Decimal("1"),
        max_concurrent_positions=10,
        min_price=Decimal("0.02"),
        max_price=Decimal("0.98"),
    )
    base.update(kw)
    return TemplateConfig(**base)  # type: ignore[arg-type]


def _follow(tpl: TemplateConfig | None = None) -> FollowSpec:
    return FollowSpec(
        account_id=ACCT,
        leader_address=LEADER,
        template=tpl if tpl is not None else _tpl(),
        binding=BINDING,
    )


def _trade(**kw: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        leader_address=LEADER,
        tx_hash="0xdeadbeef",
        condition_id="0xcond_fed_june_2026",
        market_id="0xcond_fed_june_2026",
        side="BUY",
        size=100.0,
        price=0.5,
        outcome="Yes",
        token_id="tok-fed-cut-yes",
    )
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# import / package smoke (CI imports `polyecho.core`)
# ---------------------------------------------------------------------------


def test_package_imports() -> None:
    pkg = importlib.import_module("polyecho.core")
    assert hasattr(pkg, "TraderCore")
    assert pkg.Decision is Decision  # re-exported verbatim from the frozen contract
    assert pkg.DecisionOutcome is DecisionOutcome


# ---------------------------------------------------------------------------
# Dedup (brief §3) — same tx_hash twice ⇒ exactly one process
# ---------------------------------------------------------------------------


def test_deduplicator_claim_once() -> None:
    d = TradeDeduplicator()
    assert d.claim(LEADER, "0xabc") is True
    assert d.claim(LEADER, "0xabc") is False
    assert d.seen(LEADER, "0xabc") is True


@pytest.mark.asyncio
async def test_same_tx_processed_once() -> None:
    core = TraderCore(follows=[_follow()])
    t = _trade()
    await core.process_detected(dict(t))
    await core.process_detected(dict(t))  # same tx again
    assert core.stats.detected == 2
    assert core.stats.deduped == 1
    # Exactly one decision acted on (one dry-run skip recorded).
    assert len(core.skips) == 1


@pytest.mark.asyncio
async def test_synthetic_tx_hash_is_stable_cross_source() -> None:
    a = synthetic_tx_hash(leader=LEADER, condition_id="c", ts=1, side="BUY", size=1.0)
    b = synthetic_tx_hash(leader=LEADER, condition_id="c", ts=1, side="BUY", size=1.0)
    assert a == b and a.startswith("0x")


# ---------------------------------------------------------------------------
# Evaluator + sizing (brief §3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluator_copy_ratio_sizing() -> None:
    ev = TraderCopyEvaluator(template_for=lambda a, ldr: _tpl(copy_ratio=Decimal("0.5")))
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(size=100.0, price=0.4),
    )
    assert out.decision.outcome is DecisionOutcome.COPY
    assert out.decision.size == Decimal("50")  # 100 * 0.5
    assert out.decision.order_type == "FOK"


@pytest.mark.asyncio
async def test_evaluator_fixed_size_sizing() -> None:
    ev = TraderCopyEvaluator(
        template_for=lambda a, ldr: _tpl(copy_ratio=None, fixed_size_usd=Decimal("20"))
    )
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(price=0.5),
    )
    assert out.decision.size == Decimal("40")  # 20 / 0.5


@pytest.mark.asyncio
async def test_evaluator_below_min_size_skips() -> None:
    ev = TraderCopyEvaluator(
        template_for=lambda a, ldr: _tpl(copy_ratio=Decimal("0.001"))
    )
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(size=100.0, price=0.5),  # 0.1 share < 1
    )
    assert out.decision.skip_reason is SkipReason.BELOW_MIN_SIZE


@pytest.mark.asyncio
async def test_evaluator_not_following_skips() -> None:
    ev = TraderCopyEvaluator(
        template_for=lambda a, ldr: _tpl(), is_following=lambda a, ldr: False
    )
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(),
    )
    assert out.decision.skip_reason is SkipReason.NOT_FOLLOWING


@pytest.mark.asyncio
async def test_evaluator_keyword_filter_skips() -> None:
    ev = TraderCopyEvaluator(
        template_for=lambda a, ldr: _tpl(category_whitelist=("Politics",))
    )
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(title="Crypto market"),
    )
    assert out.decision.skip_reason is SkipReason.FILTER_KEYWORD


@pytest.mark.asyncio
async def test_evaluator_price_band_skips() -> None:
    ev = TraderCopyEvaluator(
        template_for=lambda a, ldr: _tpl(min_price=Decimal("0.2"), max_price=Decimal("0.8"))
    )
    out = await ev.evaluate_copy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        trade=_trade(price=0.05),
    )
    assert out.decision.skip_reason is SkipReason.FILTER_PRICE_BAND


# ---------------------------------------------------------------------------
# Risk gate — each layer + fail-closed (brief §4)
# ---------------------------------------------------------------------------


def _eval(decision: Decision, raw: dict[str, object] | None = None) -> EvaluatedCopy:
    return EvaluatedCopy(
        leader_address=LEADER,
        tx_hash="0x1",
        account_id=ACCT,
        condition_id="c",
        binding=BINDING,
        decision=decision,
        raw=raw or {},
    )


def _copy_decision(**kw: object) -> Decision:
    base: dict[str, object] = dict(
        outcome=DecisionOutcome.COPY,
        side=TradeSide.BUY,
        token_id="tok",
        price=Decimal("0.5"),
        size=Decimal("10"),
        order_type="FOK",
    )
    base.update(kw)
    return Decision(**base)  # type: ignore[arg-type]


async def _ctx(tpl: TemplateConfig, **kw: object) -> RiskContext:
    return RiskContext(template=tpl, **kw)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_risk_pass() -> None:
    gate = LayeredRiskGate(context_for=lambda e: _ctx(_tpl()))
    res = await gate.check(_eval(_copy_decision()))
    assert res.passed is True


@pytest.mark.asyncio
async def test_risk_fail_closed_on_context_error() -> None:
    async def boom(_e: EvaluatedCopy) -> RiskContext:
        raise RuntimeError("db down")

    gate = LayeredRiskGate(context_for=boom)
    res = await gate.check(_eval(_copy_decision()))
    assert res.passed is False
    assert res.skip_reason is SkipReason.DATA_UNAVAILABLE


@pytest.mark.asyncio
async def test_risk_fail_closed_on_orderbook_error() -> None:
    async def book_boom(_tid: str) -> dict[str, object]:
        raise RuntimeError("book unavailable")

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl()), orderbook_for=book_boom
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.passed is False
    assert res.skip_reason is SkipReason.DATA_UNAVAILABLE


@pytest.mark.asyncio
async def test_risk_dup_gate() -> None:
    gate = LayeredRiskGate(context_for=lambda e: _ctx(_tpl(), already_processed=True))
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.DUP


@pytest.mark.asyncio
async def test_risk_not_following_gate() -> None:
    gate = LayeredRiskGate(context_for=lambda e: _ctx(_tpl(), is_following=False))
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.NOT_FOLLOWING


@pytest.mark.asyncio
async def test_risk_keyword_gate() -> None:
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(category_whitelist=("Sports",)))
    )
    res = await gate.check(_eval(_copy_decision(), raw={"title": "Politics"}))
    assert res.skip_reason is SkipReason.FILTER_KEYWORD


@pytest.mark.asyncio
async def test_risk_market_end_gate() -> None:
    gate = LayeredRiskGate(context_for=lambda e: _ctx(_tpl()))
    res = await gate.check(_eval(_copy_decision(), raw={"market_ended": True}))
    assert res.skip_reason is SkipReason.FILTER_MARKET_END


@pytest.mark.asyncio
async def test_risk_price_band_gate() -> None:
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(min_price=Decimal("0.6")))
    )
    res = await gate.check(_eval(_copy_decision(price=Decimal("0.5"))))
    assert res.skip_reason is SkipReason.FILTER_PRICE_BAND


@pytest.mark.asyncio
async def test_risk_spread_gate_uses_single_book_fetch() -> None:
    calls = {"n": 0}

    async def book(_tid: str) -> dict[str, object]:
        calls["n"] += 1
        return {
            "bids": [{"price": 0.40, "size": 100}],
            "asks": [{"price": 0.60, "size": 100}],
        }

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(max_spread=Decimal("0.05"))),
        orderbook_for=book,
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.ORDERBOOK_SPREAD
    assert calls["n"] == 1  # ONE reused orderbook fetch (brief §4)


@pytest.mark.asyncio
async def test_risk_depth_gate() -> None:
    async def book(_tid: str) -> dict[str, object]:
        return {
            "bids": [{"price": 0.49, "size": 5}],
            "asks": [{"price": 0.51, "size": 5}],
        }

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(min_depth=Decimal("100"))),
        orderbook_for=book,
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.ORDERBOOK_DEPTH


@pytest.mark.asyncio
async def test_risk_position_value_normalize_then_pass() -> None:
    # max_position 5 USD @ price 0.5 ⇒ 10 shares cap; request 100 ⇒ normalized.
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(max_position_usd=Decimal("5")))
    )
    res = await gate.check(_eval(_copy_decision(size=Decimal("100"))))
    assert res.passed is True
    assert res.adjusted_size == Decimal("10.000000")


@pytest.mark.asyncio
async def test_risk_position_value_infeasible_skips() -> None:
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(max_position_usd=Decimal("0.10"))
        )
    )
    res = await gate.check(_eval(_copy_decision(size=Decimal("100"))))
    assert res.skip_reason is SkipReason.POSITION_CAP


@pytest.mark.asyncio
async def test_risk_daily_order_cap_gate() -> None:
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(daily_order_cap=20), daily_order_count=20
        )
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.DAILY_ORDER_CAP


@pytest.mark.asyncio
async def test_risk_daily_loss_cap_gate() -> None:
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(daily_loss_limit_usd=Decimal("10")),
            daily_realised_loss_usd=Decimal("12"),
        )
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.DAILY_LOSS_CAP


# --- the 7 PolyEcho-over-PolyHermes safety improvements (brief §7) ----------


class _OneTradeDS:
    """Minimal DataSource-shaped fake yielding one fixed TRADE record."""

    def __init__(self, records: list[ActivityRecord]) -> None:
        self._records = records

    async def get_user_activity(
        self, wallet: str, *, start: datetime | None = None, **kw: Any
    ) -> list[ActivityRecord]:
        return list(self._records)


@pytest.mark.asyncio
async def test_improvement1_n_confirmation_guard() -> None:
    """#1 N-confirmation guard on the on-chain cross-check path."""
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    ds = _OneTradeDS(
        [
            ActivityRecord(
                wallet=LEADER,
                activity_type="TRADE",
                condition_id="c",
                side="BUY",
                size=10.0,
                price=0.5,
                usdc_size=5.0,
                timestamp=datetime.now(tz=UTC),
            )
        ]
    )
    chain = ChainCrossCheckListener(
        [LEADER], q, datasource=ds, confirmations=2  # type: ignore[arg-type]
    )
    assert await chain.run_once() == 0  # 1st sighting < 2 confirmations
    assert q.empty()
    assert await chain.run_once() == 1  # 2nd sighting ⇒ confirmed, emitted
    assert not q.empty()


@pytest.mark.asyncio
async def test_improvement2_mtm_open_loss_breaker() -> None:
    """#2 mark-to-market / open-loss breaker (NOT realised-only)."""
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(open_loss_limit_usd=Decimal("10")),
            daily_realised_loss_usd=Decimal("4"),
            open_unrealised_loss_usd=Decimal("8"),  # 4+8 ≥ 10
        )
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.DAILY_LOSS_CAP
    assert "MtM" in (res.detail or "")


@pytest.mark.asyncio
async def test_improvement3_portfolio_cap() -> None:
    """#3 cross-leader/account portfolio-exposure cap."""
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(portfolio_max_usd=Decimal("100")),
            portfolio_exposure_usd=Decimal("98"),
        )
    )
    res = await gate.check(_eval(_copy_decision(size=Decimal("10"), price=Decimal("0.5"))))
    assert res.skip_reason is SkipReason.PORTFOLIO_CAP


@pytest.mark.asyncio
async def test_improvement4_position_count_cap_kept() -> None:
    """#4 KEEP a position-count / concurrency cap."""
    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(
            _tpl(max_concurrent_positions=3), open_position_count=3
        )
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.skip_reason is SkipReason.POSITION_CAP


@pytest.mark.asyncio
async def test_improvement6_skip_taxonomy_is_queryable() -> None:
    """#6 queryable skip/error taxonomy persisted per skip."""
    core = TraderCore(
        follows=[_follow(_tpl(category_whitelist=("Politics",)))],
    )
    await core.process_detected(_trade(title="Crypto only"))
    assert core.skips and core.skips[0][0] == SkipReason.FILTER_KEYWORD.value
    assert core.stats.skip_reasons.get(SkipReason.FILTER_KEYWORD.value) == 1


@pytest.mark.asyncio
async def test_risk_slippage_and_strategy_drift() -> None:
    async def book(_tid: str) -> dict[str, object]:
        return {
            "bids": [{"price": 0.50, "size": 100}],
            "asks": [{"price": 0.80, "size": 100}],  # ask >> our 0.5 limit
        }

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(max_slippage_pct=Decimal("3"), max_spread=Decimal("1"))),
        orderbook_for=book,
    )
    res = await gate.check(_eval(_copy_decision(price=Decimal("0.5"))))
    assert res.skip_reason is SkipReason.SLIPPAGE_REJECT

    gate2 = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(), strategy_drift=True)
    )
    res2 = await gate2.check(_eval(_copy_decision()))
    assert res2.skip_reason is SkipReason.STRATEGY_DRIFT


# ---------------------------------------------------------------------------
# FIFO sell-match (brief §3/§5)
# ---------------------------------------------------------------------------


def test_fifo_sell_match_oldest_first() -> None:
    led = FifoLedger()
    led.add_buy(
        BuyLot(
            trade_id="b1",
            account_id=ACCT,
            condition_id="c",
            outcome="Yes",
            size=Decimal("10"),
            price=Decimal("0.4"),
        )
    )
    led.add_buy(
        BuyLot(
            trade_id="b2",
            account_id=ACCT,
            condition_id="c",
            outcome="Yes",
            size=Decimal("10"),
            price=Decimal("0.5"),
        )
    )
    m = led.match_sell(
        account_id=ACCT,
        condition_id="c",
        outcome="Yes",
        sell_size=Decimal("15"),
        sell_price=Decimal("0.6"),
    )
    # 10 from b1 (oldest), 5 from b2.
    assert m.matched == [("b1", Decimal("10")), ("b2", Decimal("5"))]
    assert m.unmatched_qty == Decimal("0")
    # PnL: 10*(0.6-0.4) + 5*(0.6-0.5) = 2.0 + 0.5 = 2.5
    assert m.realised_pnl_usd == Decimal("2.5")
    assert len(led.open_lots(ACCT, "c", "Yes")) == 1  # b2 partially open


# ---------------------------------------------------------------------------
# HARD live lock — default OFF ⇒ ZERO placement (the central proof)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_lock_default_off_places_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (POLYECHO_LIVE_TRADING unset) ⇒ NO order placed, ever."""
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    # A client_factory that MUST never be called in dry-run.
    called = {"n": 0}

    async def forbidden_factory(_e: EvaluatedCopy):  # type: ignore[no-untyped-def]
        called["n"] += 1
        raise AssertionError("client_factory must not run when live lock is off")

    ev = _eval(_copy_decision(side=TradeSide.BUY))
    execu = OrderExecutor(client_factory=forbidden_factory)
    res = await execu.execute(ev)

    assert res.placed is False
    assert res.skip_reason is SkipReason.LIVE_LOCKED
    assert called["n"] == 0  # zero placement work


@pytest.mark.asyncio
async def test_engine_dry_run_records_live_locked_skip() -> None:
    core = TraderCore(follows=[_follow()])
    await core.process_detected(_trade(side="BUY", price=0.5, size=100.0))
    assert core.stats.placed == 0
    assert core.stats.copied == 1
    # The intended order is logged as a LIVE_LOCKED skip (taxonomy).
    assert any(r == SkipReason.LIVE_LOCKED.value for r, _ in core.skips)


@pytest.mark.asyncio
async def test_place_order_chokepoint_raises_when_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared chokepoint itself fails closed (defence in depth)."""
    from polyecho.shared.config import get_settings
    from polyecho.shared.copytrading import LiveTradingLockedError
    from polyecho.shared.polymarket.client import PolymarketClient

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()
    client = PolymarketClient(
        signature_type=0, funder=LEADER, creds=None, private_key=None
    )
    with pytest.raises(LiveTradingLockedError):
        await client.place_order(token_id="t", side="BUY", price=0.5, size=1.0)


# ---------------------------------------------------------------------------
# Reconcile backfill (brief §7 #5) — WS-gap closure, restart-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_backfills_after_cursor() -> None:
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 2, 1, tzinfo=UTC)
    ds = _OneTradeDS(
        [
            ActivityRecord(LEADER, "TRADE", "c", "BUY", 5.0, 0.5, 2.5, old),
            ActivityRecord(LEADER, "TRADE", "c", "BUY", 7.0, 0.5, 3.5, new),
        ]
    )

    enq: list[DetectedTrade] = []
    written: list[tuple[str, datetime]] = []

    async def _read() -> dict[str, datetime | None]:
        return {LEADER: old}  # cursor at `old` ⇒ only `new` backfilled

    async def _write(leader: str, ts: datetime) -> None:
        written.append((leader, ts))

    async def _enq(t: DetectedTrade) -> None:
        enq.append(t)

    rec = Reconciler(
        cursor_reader=_read,
        cursor_writer=_write,
        enqueue=_enq,
        datasource=ds,  # type: ignore[arg-type]
    )
    n = await rec.reconcile_once()
    assert n == 1
    assert enq[0]["timestamp"] == int(new.timestamp())
    assert written == [(LEADER, new)]


# ---------------------------------------------------------------------------
# Redeem worker — idempotency + live lock (brief §9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_idempotent_and_live_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    from polyecho.shared.config import get_settings
    from polyecho.shared.copytrading import RedeemJobStatus

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    seen: set[tuple[str, str, int]] = set()

    async def candidates() -> list[RedeemCandidate]:
        return [
            RedeemCandidate(ACCT, "0xcond", 0, False),
            RedeemCandidate(ACCT, "0xcond", 0, False),  # duplicate candidate
        ]

    async def upsert(spec: Any) -> bool:
        key = (str(spec.account_id), spec.condition_id, spec.outcome_index)
        if key in seen:
            return False  # idempotent: already exists
        seen.add(key)
        return True

    w = RedeemWorker(
        candidate_source=candidates, job_upserter=upsert, auto_redeem=True
    )
    outs = await w.poll_once()
    assert len(outs) == 2
    assert outs[0].created is True
    assert outs[1].created is False  # idempotent on (account, condition, outcome)
    # Live lock off ⇒ nothing submitted, all PENDING + LIVE_LOCKED.
    assert all(o.result.status is RedeemJobStatus.PENDING for o in outs)
    assert all(o.result.skip_reason is SkipReason.LIVE_LOCKED for o in outs)


# ---------------------------------------------------------------------------
# Detection replay producer + full --once pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_channel_listener_replays_fixture() -> None:
    q: asyncio.Queue[DetectedTrade] = asyncio.Queue()
    ws = UserChannelListener([LEADER], q, live=False)
    n = await ws.run_once()
    assert n > 0
    assert q.qsize() == n
    first = q.get_nowait()
    assert first["leader_address"] == LEADER
    assert first["source"] == "ws"


@pytest.mark.asyncio
async def test_run_once_dry_run_places_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    core = TraderCore(follows=[_follow(_tpl(copy_ratio=Decimal("0.05")))])
    stats = await core.run_once()
    assert stats.detected > 0
    assert stats.placed == 0  # HARD invariant: zero placement by default


@pytest.mark.asyncio
async def test_main_once_exits_zero() -> None:
    from polyecho.core.__main__ import main

    rc = await main(["--once"])
    assert rc == 0


# ===========================================================================
# Wave B-core P1 remediation — focused regression tests (one block per fix)
# ===========================================================================


# --- P1-1: the risk-adjusted size MUST reach the placed order --------------


@pytest.mark.asyncio
async def test_p1_1_adjusted_size_reaches_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normalize/clamp ⇒ the EXECUTED size == gate-approved adjusted_size,
    NOT the original (too-large) decision.size — verified on the LIVE path.
    """
    from polyecho.shared.config import get_settings

    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    get_settings.cache_clear()
    try:
        placed: dict[str, float] = {}

        class _Client:
            async def place_order(self, **kw: Any) -> dict[str, Any]:
                placed.update(kw)
                return {"status": "matched"}

        async def _factory(_e: EvaluatedCopy) -> Any:
            return _Client()

        execu = OrderExecutor(client_factory=_factory)
        # decision asks for 100 shares; gate approved only 10.
        ev = _eval(_copy_decision(size=Decimal("100"), price=Decimal("0.5")))
        res = await execu.execute(ev, size_override=Decimal("10.000000"))

        assert res.placed is True
        assert res.decision.size == Decimal("10.000000")  # rebuilt decision
        assert placed["size"] == 10.0  # the SDK got the clamped size
        assert placed["size"] != 100.0
    finally:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_p1_1_no_override_uses_decision_size() -> None:
    """No adjustment ⇒ decision size unchanged (no behavioural drift)."""
    ev = _eval(_copy_decision(size=Decimal("7"), side=TradeSide.BUY))
    res = await OrderExecutor().execute(ev, size_override=None)
    assert res.decision.size == Decimal("7")
    assert res.skip_reason is SkipReason.LIVE_LOCKED  # live-lock intact


# --- P1-2: malformed orderbook ⇒ DATA_UNAVAILABLE (fail-CLOSED) ------------


@pytest.mark.asyncio
async def test_p1_2_malformed_book_row_fails_closed_not_raises() -> None:
    """A real again-shaped book row ⇒ DATA_UNAVAILABLE reject, NOT an
    unhandled exception escaping the gate (which would be fail-OPEN).
    """

    async def book_missing_size(_tid: str) -> dict[str, object]:
        return {"bids": [{"price": 0.49}], "asks": [{"price": 0.51, "size": 5}]}

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl()), orderbook_for=book_missing_size
    )
    res = await gate.check(_eval(_copy_decision()))
    assert res.passed is False
    assert res.skip_reason is SkipReason.DATA_UNAVAILABLE


@pytest.mark.asyncio
async def test_p1_2_non_numeric_and_nonlist_book_fail_closed() -> None:
    async def book_garbage_price(_tid: str) -> dict[str, object]:
        return {
            "bids": [{"price": "n/a", "size": 5}],
            "asks": [{"price": 0.51, "size": 5}],
        }

    g1 = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl()), orderbook_for=book_garbage_price
    )
    r1 = await g1.check(_eval(_copy_decision()))
    assert r1.skip_reason is SkipReason.DATA_UNAVAILABLE

    async def book_not_list(_tid: str) -> dict[str, object]:
        return {"bids": "oops", "asks": []}

    g2 = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl()), orderbook_for=book_not_list
    )
    r2 = await g2.check(_eval(_copy_decision()))
    assert r2.skip_reason is SkipReason.DATA_UNAVAILABLE


@pytest.mark.asyncio
async def test_p1_2_well_formed_book_still_passes() -> None:
    """Regression guard: a valid book is unaffected by the new guard."""

    async def good_book(_tid: str) -> dict[str, object]:
        return {
            "bids": [{"price": 0.49, "size": 500}],
            "asks": [{"price": 0.50, "size": 500}],
        }

    gate = LayeredRiskGate(
        context_for=lambda e: _ctx(_tpl(min_depth=Decimal("100"))),
        orderbook_for=good_book,
    )
    res = await gate.check(_eval(_copy_decision(price=Decimal("0.5"))))
    assert res.passed is True


# --- P1-3: dry-run does NOT mutate the shared ledger; live does -----------


@pytest.mark.asyncio
async def test_p1_3_dry_run_does_not_pollute_ledger() -> None:
    core = TraderCore(follows=[_follow()])
    await core.process_detected(_trade(side="BUY", price=0.5, size=100.0))
    assert core.stats.copied == 1
    assert core.stats.placed == 0  # dry-run
    # No phantom BuyLot ⇒ open_position_count stays 0 (no phantom exposure).
    assert (
        core._executor.ledger.open_lots(
            ACCT, "0xcond_fed_june_2026", "Yes"
        )
        == []
    )


@pytest.mark.asyncio
async def test_p1_3_live_path_still_records_buy_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live path's FIFO accounting is unchanged (real fill ⇒ a lot)."""
    from polyecho.shared.config import get_settings

    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    get_settings.cache_clear()
    try:

        class _Client:
            async def place_order(self, **kw: Any) -> dict[str, Any]:
                return {"status": "matched"}

        async def _factory(_e: EvaluatedCopy) -> Any:
            return _Client()

        core = TraderCore(follows=[_follow()])
        core._executor = OrderExecutor(
            client_factory=_factory, ledger=core._ledger
        )
        await core.process_detected(_trade(side="BUY", price=0.5, size=100.0))
        assert core.stats.placed == 1
        lots = core._ledger.open_lots(ACCT, "0xcond_fed_june_2026", "Yes")
        assert len(lots) == 1  # real fill ⇒ ledger correct on the live path
    finally:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        get_settings.cache_clear()


# --- P1-4: cursor_writer persists + per-leader error isolation ------------


@pytest.mark.asyncio
async def test_p1_4_cursor_writer_persists_and_is_restart_safe() -> None:
    persisted: list[tuple[str, datetime]] = []

    async def _writer(leader: str, ts: datetime) -> None:
        persisted.append((leader, ts))

    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 2, 1, tzinfo=UTC)
    spec = FollowSpec(
        account_id=ACCT,
        leader_address=LEADER,
        template=_tpl(),
        binding=BINDING,
        last_seen_ts=old,
    )
    core = TraderCore(
        follows=[spec],
        datasource=_OneTradeDS(  # type: ignore[arg-type]
            [ActivityRecord(LEADER, "TRADE", "c", "BUY", 5.0, 0.5, 2.5, new)]
        ),
        cursor_writer=_writer,
    )
    n = await core._reconcile_once()
    assert n >= 1
    assert persisted and persisted[0][0] == LEADER
    assert persisted[0][1] == new
    # Restart-safe: the in-proc store advanced ⇒ a 2nd pass re-reads `new`.
    assert core._cursor_store[LEADER.lower()] == new


@pytest.mark.asyncio
async def test_p1_4_reconcile_isolates_per_leader_failure() -> None:
    from polyecho.core.reconcile import Reconciler

    L1 = "0xleadera00000000000000000000000000000000a"
    L2 = "0xleaderb00000000000000000000000000000000b"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    good = datetime(2026, 3, 1, tzinfo=UTC)

    class _SplitDS:
        async def get_user_activity(
            self, wallet: str, *, start: datetime | None = None, **kw: Any
        ) -> list[ActivityRecord]:
            if wallet == L1:
                raise RuntimeError("datasource down for L1")
            return [ActivityRecord(L2, "TRADE", "c", "BUY", 5.0, 0.5, 2.5, good)]

    enq: list[DetectedTrade] = []
    written: dict[str, datetime] = {}

    async def _read() -> dict[str, datetime | None]:
        return {L1: base, L2: base}

    async def _write(leader: str, ts: datetime) -> None:
        written[leader] = ts

    async def _enq(t: DetectedTrade) -> None:
        enq.append(t)

    rec = Reconciler(
        cursor_reader=_read,
        cursor_writer=_write,
        enqueue=_enq,
        datasource=_SplitDS(),  # type: ignore[arg-type]
    )
    n = await rec.reconcile_once()
    # L1 failed but L2 still backfilled + advanced; L1 cursor NOT moved.
    assert n == 1
    assert L1 not in written  # failed leader's cursor stays put (retry next)
    assert written.get(L2) == good


# --- P1-5: heartbeat row written on cycle --------------------------------


@pytest.mark.asyncio
async def test_p1_5_heartbeat_written_on_cycle() -> None:
    beats: list[dict[str, Any]] = []

    async def _hb(info: dict[str, Any]) -> None:
        beats.append(info)

    core = TraderCore(follows=[_follow()], heartbeat_writer=_hb)
    await core.run_once()
    # At least the start + complete heartbeat of one cycle.
    assert len(beats) >= 2
    assert {b["phase"] for b in beats} >= {"cycle_start", "cycle_complete"}
    assert core.last_heartbeat is not None
    assert "ts" in core.last_heartbeat
    assert core.last_heartbeat["detected"] >= 0


@pytest.mark.asyncio
async def test_p1_5_heartbeat_write_failure_never_crashes_loop() -> None:
    async def _bad_hb(_info: dict[str, Any]) -> None:
        raise RuntimeError("settings table unreachable")

    core = TraderCore(follows=[_follow()], heartbeat_writer=_bad_hb)
    stats = await core.run_once()  # must NOT raise
    assert stats.detected >= 0
    assert core.last_heartbeat is not None  # in-proc mirror still set


# --- P1-6: a failed pending row is RETRIED (not permanently abandoned) ----


def _pending_row(**kw: Any) -> Any:
    from polyecho.core.pending import PendingActionRow
    from polyecho.shared.pending_actions import (
        PendingActionStatus,
        PendingActionType,
    )

    base: dict[str, Any] = dict(
        id="act-1",
        action_type=PendingActionType.MANUAL_SELL,
        status=str(PendingActionStatus.CONFIRMED),
        params={"account_id": str(ACCT), "token_id": "t", "size": "5"},
    )
    base.update(kw)
    return PendingActionRow(**base)


@pytest.mark.asyncio
async def test_p1_6_failed_row_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import polyecho.core.pending as pending_mod
    from polyecho.core.pending import PendingActionExecutor

    monkeypatch.setattr(
        pending_mod, "is_danger_passphrase_provisioned", _true
    )
    row = _pending_row()
    marked: list[str] = []

    async def _rows() -> list[Any]:
        return [row]

    async def _positions(_a: uuid.UUID) -> list[Any]:
        return []

    async def _mark(action_id: str, _params: dict[str, Any]) -> None:
        marked.append(action_id)

    calls = {"n": 0}

    class _FlakyExecutor(OrderExecutor):
        async def execute(
            self,
            evaluated: EvaluatedCopy,
            *,
            size_override: Decimal | None = None,
        ) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient venue error")
            return await super().execute(
                evaluated, size_override=size_override
            )

    ex = PendingActionExecutor(
        rows_source=_rows,
        positions_for=_positions,
        mark_processed=_mark,
        executor=_FlakyExecutor(),
    )
    # 1st poll: execution raises ⇒ row left UN-seen, NOT marked.
    out1 = await ex.poll_once()
    assert out1 == []
    assert marked == []
    assert row.id not in ex._seen
    # 2nd poll (same row, web hasn't re-confirmed): retried and succeeds.
    out2 = await ex.poll_once()
    assert len(out2) == 1
    assert marked == [row.id]
    assert row.id in ex._seen


@pytest.mark.asyncio
async def test_p1_6_p0_guards_still_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 expired / not-provisioned refusals are untouched by P1-6."""
    import polyecho.core.pending as pending_mod
    from polyecho.core.pending import PendingActionExecutor

    async def _positions(_a: uuid.UUID) -> list[Any]:
        return []

    async def _mark(_id: str, _p: dict[str, Any]) -> None:
        raise AssertionError("a refused row must NOT be marked processed")

    # (a) danger passphrase NOT provisioned ⇒ refuse, claim, no execute.
    monkeypatch.setattr(
        pending_mod, "is_danger_passphrase_provisioned", _false
    )
    row = _pending_row()

    async def _rows() -> list[Any]:
        return [row]

    ex = PendingActionExecutor(
        rows_source=_rows, positions_for=_positions, mark_processed=_mark
    )
    assert await ex.poll_once() == []
    assert row.id in ex._seen  # P0 refusal still claims (no loop-spam)

    # (b) expired confirmed row ⇒ refused even with passphrase provisioned.
    monkeypatch.setattr(
        pending_mod, "is_danger_passphrase_provisioned", _true
    )
    expired = _pending_row(
        id="act-exp",
        expires_at=datetime(2000, 1, 1, tzinfo=UTC),
    )

    async def _rows2() -> list[Any]:
        return [expired]

    ex2 = PendingActionExecutor(
        rows_source=_rows2, positions_for=_positions, mark_processed=_mark
    )
    assert await ex2.poll_once() == []
    assert expired.id in ex2._seen


# --- P1-12: --once non-zero on a degenerate pass -------------------------


@pytest.mark.asyncio
async def test_p1_12_no_follows_is_idle_exit_zero() -> None:
    core = TraderCore(follows=[])
    await core.run_once()
    assert core.degenerate_once_pass() is None  # idle ⇒ exit 0


@pytest.mark.asyncio
async def test_p1_12_follows_but_zero_detected_is_degenerate() -> None:
    # A leader that is NOT in the bundled fixtures ⇒ 0 detections.
    ghost = "0xghostleader000000000000000000000000000099"
    spec = FollowSpec(
        account_id=ACCT,
        leader_address=ghost,
        template=_tpl(),
        binding=WalletBinding(
            signature_type=0, funder=ghost, wallet_mode="eoa", derived=False
        ),
    )
    core = TraderCore(follows=[spec])
    await core.run_once()
    assert core.stats.detected == 0
    assert core.degenerate_once_pass() is not None  # ⇒ --once exits non-zero


@pytest.mark.asyncio
async def test_p1_12_detected_all_skipped_is_healthy() -> None:
    """Detected + all-skipped (default LIVE_LOCKED) is HEALTHY (exit 0)."""
    core = TraderCore(follows=[_follow()])
    await core.run_once()
    assert core.stats.detected > 0
    assert core.stats.placed == 0  # all dry-run skips
    assert core.degenerate_once_pass() is None  # skips are NOT failure


@pytest.mark.asyncio
async def test_p1_12_main_once_degenerate_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import polyecho.core.__main__ as main_mod

    ghost = "0xghostleader000000000000000000000000000099"

    def _ghost_follows() -> list[FollowSpec]:
        return [
            FollowSpec(
                account_id=ACCT,
                leader_address=ghost,
                template=_tpl(),
                binding=WalletBinding(
                    signature_type=0,
                    funder=ghost,
                    wallet_mode="eoa",
                    derived=False,
                ),
            )
        ]

    monkeypatch.setattr(main_mod, "_demo_follows", _ghost_follows)
    rc = await main_mod.main(["--once"])
    assert rc == 2  # degenerate ⇒ documented non-zero exit


# --- P1-13: TradeDeduplicator._locks is bounded (no memory leak) ----------


def test_p1_13_locks_dict_bounded_to_capacity() -> None:
    d = TradeDeduplicator(capacity=8)
    for i in range(200):
        leader = f"0xleader{i:034d}"
        lock = d.lock_for(leader, "0xtx")
        assert lock is not None
        d.claim(leader, "0xtx")
    # _seen is LRU-capped at 8; _locks must be evicted in lock-step.
    assert len(d._locks) <= 8
    assert len(d._seen._data) <= 8


def test_p1_13_live_lock_for_active_key_survives() -> None:
    """A still-in-window key keeps its lock (no premature eviction)."""
    d = TradeDeduplicator(capacity=4)
    lk = d.lock_for(LEADER, "0xkeep")
    d.claim(LEADER, "0xkeep")
    # Push 3 more (still within capacity 4) — keep must remain.
    for i in range(3):
        d.claim(f"0xother{i:034d}", "0xtx")
    assert d._locks.get(d.key(LEADER, "0xkeep")) is lk


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


# ===========================================================================
# API-status A-core — Task 1 (canonical heartbeat) + Task 2 (integration
# probe).  The FROZEN F0 ``polyecho.shared.health`` module lives OUTSIDE
# this worktree, so these tests install a faithful in-memory stand-in into
# ``sys.modules`` for the duration of each test (the engine imports the
# helper lazily, so it picks up whichever module is registered).  This
# keeps the suite green both in the isolated base AND under the integrated
# verify (the stub is always installed/removed around the test, so it
# never shadows the real F0 outside it).
# ===========================================================================

import contextlib  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402


@contextlib.contextmanager
def _fake_shared_health(
    *,
    run_all_probes: Any,
    write_heartbeat: Any | None = None,
) -> Any:
    """Install a fake ``polyecho.shared.health`` for the test body.

    ``write_integration_status`` records every snapshot into the returned
    list.  ``write_heartbeat`` defaults to recording the canonical kwargs;
    callers can pass a custom (e.g. raising) impl.
    """
    integ_writes: list[Any] = []
    hb_writes: list[dict[str, Any]] = []

    async def _default_write_heartbeat(
        kind: str,
        *,
        phase: str,
        datasource: str,
        live_trading: bool,
        counters: dict[str, Any],
    ) -> None:
        # F0 stamps ``ts`` itself (keyword-only signature, no ``ts`` arg);
        # the caller passes none, so the recorder captures none.
        hb_writes.append(
            {
                "kind": kind,
                "phase": phase,
                "datasource": datasource,
                "live_trading": live_trading,
                "counters": counters,
            }
        )

    async def _write_integration_status(snapshot: Any) -> None:
        integ_writes.append(snapshot)

    mod = types.ModuleType("polyecho.shared.health")
    mod.run_all_probes = run_all_probes  # type: ignore[attr-defined]
    mod.write_integration_status = _write_integration_status  # type: ignore[attr-defined]
    mod.write_heartbeat = (  # type: ignore[attr-defined]
        write_heartbeat if write_heartbeat is not None else _default_write_heartbeat
    )

    saved = sys.modules.get("polyecho.shared.health")
    sys.modules["polyecho.shared.health"] = mod
    try:
        yield types.SimpleNamespace(
            integ_writes=integ_writes, hb_writes=hb_writes
        )
    finally:
        if saved is not None:
            sys.modules["polyecho.shared.health"] = saved
        else:
            sys.modules.pop("polyecho.shared.health", None)


# --- Task 1: canonical trader_heartbeat via the FROZEN shared helper -------


@pytest.mark.asyncio
async def test_task1_heartbeat_uses_canonical_shared_helper() -> None:
    """``run_once`` drives shared ``write_heartbeat("trader", ...)`` with the
    canonical nested-counters shape, while the legacy in-proc mirror / hook
    stay legacy-shaped (F0 reader folds them — no flag-day)."""

    async def _probes() -> dict[str, Any]:
        return {"ok": True}

    legacy_hook: list[dict[str, Any]] = []

    async def _hb(info: dict[str, Any]) -> None:
        legacy_hook.append(info)

    with _fake_shared_health(run_all_probes=_probes) as fake:
        core = TraderCore(follows=[_follow()], heartbeat_writer=_hb)
        await core.run_once()

    # Canonical writes: at least cycle_start + cycle_complete, kind="trader",
    # counters NESTED with the required keys.
    phases = {w["phase"] for w in fake.hb_writes}
    assert phases >= {"cycle_start", "cycle_complete"}
    w = fake.hb_writes[-1]
    assert w["kind"] == "trader"
    # F0 stamps ``ts`` itself (keyword-only signature, no ``ts`` arg) so the
    # caller passes none and the fake recorder captures none — ts is
    # asserted on the legacy ``last_heartbeat`` mirror below instead.
    assert isinstance(w["live_trading"], bool)
    assert set(w["counters"]) >= {"detected", "copied", "skipped", "placed"}
    # Semantically identical: canonical counters mirror the legacy mirror.
    assert core.last_heartbeat is not None
    assert w["counters"]["detected"] == core.last_heartbeat["detected"]
    # Back-compat preserved: legacy hook + mirror keep TOP-LEVEL counters
    # (the existing settings-row shape the F0 reader folds).
    assert legacy_hook and "detected" in legacy_hook[-1]
    assert "ts" in core.last_heartbeat and "detected" in core.last_heartbeat


@pytest.mark.asyncio
async def test_task1_canonical_write_failure_never_crashes_loop() -> None:
    """A raising shared ``write_heartbeat`` (or absent F0) degrades to
    legacy-only; the supervised pass still completes."""

    async def _probes() -> dict[str, Any]:
        return {"ok": True}

    async def _boom_hb(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("shared health down")

    with _fake_shared_health(run_all_probes=_probes, write_heartbeat=_boom_hb):
        core = TraderCore(follows=[_follow()])
        stats = await core.run_once()  # must NOT raise

    assert stats.detected >= 0
    assert core.last_heartbeat is not None  # legacy mirror still set


# --- Task 2: periodic integration probe (isolation guarantees) -------------


@pytest.mark.asyncio
async def test_task2_probe_drives_write_integration_status_periodically() -> None:
    """The supervised cadence calls run_all_probes → write_integration_status
    repeatedly (independent of the trade loop).

    Driven DETERMINISTICALLY (not by a wall-clock race): each
    ``run_all_probes`` bumps a counter and sets an Event, so the test
    advances the loop cycle-by-cycle and proves true periodicity without
    timing flakiness.
    """
    calls = {"n": 0}
    tick = asyncio.Event()

    async def _probes() -> dict[str, Any]:
        calls["n"] += 1
        tick.set()
        return {"snapshot": calls["n"]}

    import polyecho.core.trader as trader_mod

    with _fake_shared_health(run_all_probes=_probes) as fake:
        # Lower the safe FLOOR for this test only so the cadence ticks
        # fast (the floor itself is asserted by
        # ``test_task2_interval_clamped_to_safe_floor``); the ctor clamps
        # ``max(floor, interval)`` so we must move the floor, not just the
        # interval.
        prev_floor = trader_mod._MIN_INTEGRATION_PROBE_INTERVAL_SECONDS
        trader_mod._MIN_INTEGRATION_PROBE_INTERVAL_SECONDS = 0.0
        try:
            core = TraderCore(
                follows=[_follow()],
                integration_probe_interval_seconds=0.001,
            )
        finally:
            trader_mod._MIN_INTEGRATION_PROBE_INTERVAL_SECONDS = prev_floor
        assert core._integration_probe_interval_s == 0.001  # not clamped
        task = asyncio.create_task(core._integration_probe_loop())
        try:
            # Await three SUCCESSIVE cycles deterministically.
            for expected in (1, 2, 3):
                await asyncio.wait_for(tick.wait(), timeout=5.0)
                tick.clear()
                assert calls["n"] >= expected
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert len(fake.integ_writes) >= 3
    assert core.integration_probe_runs >= 3
    assert fake.integ_writes[0] == {"snapshot": 1}


@pytest.mark.asyncio
async def test_task2_probe_failure_does_not_affect_engine() -> None:
    """A raising probe is swallowed: the cadence keeps ticking and a
    concurrent ``run_once`` still completes a clean pass."""

    async def _bad_probes() -> dict[str, Any]:
        raise RuntimeError("datasource probe exploded")

    with _fake_shared_health(run_all_probes=_bad_probes) as fake:
        core = TraderCore(
            follows=[_follow()], integration_probe_interval_seconds=0.01
        )
        probe_task = asyncio.create_task(core._integration_probe_loop())
        # Engine pass runs CONCURRENTLY with the failing probe cadence.
        stats = await core.run_once()
        await asyncio.sleep(0.05)  # let the probe fail several times
        probe_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await probe_task

    # Engine unharmed: a normal fixture pass (detected + dry-run skips).
    assert stats.detected > 0
    assert stats.placed == 0
    assert core.degenerate_once_pass() is None
    # Probe failed every time ⇒ zero successful integration writes, but the
    # loop never raised out (no successful runs recorded either).
    assert fake.integ_writes == []
    assert core.integration_probe_runs == 0


@pytest.mark.asyncio
async def test_task2_slow_probe_is_timeout_bounded_not_blocking() -> None:
    """A probe that hangs is cancelled by the per-cycle timeout; it never
    delays/raises into the cadence (and thus never the trade loop)."""

    async def _hang_probes() -> dict[str, Any]:
        await asyncio.sleep(3600)  # would hang forever
        return {}

    import polyecho.core.trader as trader_mod

    with _fake_shared_health(run_all_probes=_hang_probes) as fake:
        core = TraderCore(follows=[_follow()])
        # Shrink the hard per-cycle bound so the test is fast.
        prev = trader_mod.INTEGRATION_PROBE_TIMEOUT_SECONDS
        trader_mod.INTEGRATION_PROBE_TIMEOUT_SECONDS = 0.05
        try:
            # One cycle must RETURN promptly (~timeout), never hang.
            await asyncio.wait_for(
                core._run_integration_probe_once(), timeout=2.0
            )
        finally:
            trader_mod.INTEGRATION_PROBE_TIMEOUT_SECONDS = prev

    assert fake.integ_writes == []  # hung ⇒ nothing written
    assert core.integration_probe_runs == 0  # cycle skipped, no crash


def test_task2_interval_clamped_to_safe_floor() -> None:
    """A mis-configured tiny/invalid interval is clamped (never a tight
    loop)."""
    from polyecho.core.trader import (
        _MIN_INTEGRATION_PROBE_INTERVAL_SECONDS as FLOOR,
    )
    from polyecho.core.trader import (
        INTEGRATION_PROBE_INTERVAL_SECONDS as DEFAULT,
    )

    assert (
        TraderCore(
            integration_probe_interval_seconds=0.0001
        )._integration_probe_interval_s
        == FLOOR
    )
    assert (
        TraderCore(
            integration_probe_interval_seconds=-5
        )._integration_probe_interval_s
        == DEFAULT
    )
    assert (
        TraderCore(
            integration_probe_interval_seconds=120
        )._integration_probe_interval_s
        == 120
    )


@pytest.mark.asyncio
async def test_task2_once_bypasses_probe_scheduler_exit_zero() -> None:
    """``--once`` must NOT start the probe scheduler and must keep the
    exit-code contract (0 on a healthy/idle pass)."""
    from polyecho.core.__main__ import main

    async def _probes() -> dict[str, Any]:
        return {"ok": True}

    with _fake_shared_health(run_all_probes=_probes) as fake:
        rc = await main(["--once"])

    assert rc == 0
    # run_once never starts _integration_probe_loop ⇒ no integration writes.
    assert fake.integ_writes == []


@pytest.mark.asyncio
async def test_task2_once_degenerate_still_exit_two() -> None:
    """The degenerate ``--once`` path (follows but 0 detected) is untouched
    by the probe work — still exit 2."""
    ghost = "0xghostprobeleader00000000000000000000000098"
    spec = FollowSpec(
        account_id=ACCT,
        leader_address=ghost,
        template=_tpl(),
        binding=WalletBinding(
            signature_type=0, funder=ghost, wallet_mode="eoa", derived=False
        ),
    )

    async def _probes() -> dict[str, Any]:
        return {"ok": True}

    with _fake_shared_health(run_all_probes=_probes):
        core = TraderCore(follows=[spec])
        await core.run_once()

    assert core.stats.detected == 0
    assert core.degenerate_once_pass() is not None  # ⇒ --once exits 2

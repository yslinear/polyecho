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

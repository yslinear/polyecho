"""
polyecho.core Phase-5 Wave-2 tests.

Covers the three Wave-2 deliverables (fixture-first, NO network / DB /
funds — every collaborator injected / monkeypatched):

* **Event emission** — each natural trigger fires the expected
  ``EventType`` + ``Severity`` with a sensible ``PushPayload``
  (url / tag), and emission is best-effort (a raising ``emit_event``
  never crashes the loop).
* **風控收斂** — with ``get_risk_mode → CONSERVATIVE`` the gate sees a
  tightened template (a trade that passes under NORMAL is rejected /
  clamped under CONSERVATIVE).
* **Confirmed-pending-action executor** — a ``confirmed`` MANUAL_SELL
  routes through the chokepoint and, with the live lock OFF (default),
  places nothing; idempotent (a row is never executed twice); ignores
  non-``confirmed`` rows.

No Phase-4 regression: these only ADD behaviour behind the same
fixture-backed, live-locked defaults.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from polyecho.core.evaluator import TemplateConfig
from polyecho.core.execution import FifoLedger, OrderExecutor
from polyecho.core.pending import (
    OpenPosition,
    PendingActionExecutor,
    PendingActionRow,
)
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.copytrading import WalletBinding
from polyecho.shared.events import EventType, PushPayload, Severity
from polyecho.shared.pending_actions import PendingActionStatus, PendingActionType
from polyecho.shared.risk_mode import RiskMode

LEADER = "0xswing1000000000000000000000000000000001"
ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.acct")
BINDING = WalletBinding(
    signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False
)


def _tpl(**kw: Any) -> TemplateConfig:
    base: dict[str, Any] = dict(
        copy_ratio=Decimal("0.1"),
        max_position_usd=Decimal("1000"),
        min_trade_size_usd=Decimal("1"),
        max_concurrent_positions=10,
        min_price=Decimal("0.02"),
        max_price=Decimal("0.98"),
    )
    base.update(kw)
    return TemplateConfig(**base)


def _follow(tpl: TemplateConfig | None = None) -> FollowSpec:
    return FollowSpec(
        account_id=ACCT,
        leader_address=LEADER,
        template=tpl if tpl is not None else _tpl(),
        binding=BINDING,
    )


def _trade(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        leader_address=LEADER,
        tx_hash="0xphase5beef",
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


class _EmitSpy:
    """Records every ``emit_event`` call (monkeypatched into core+shared)."""

    def __init__(self, *, raises: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises
        self._n = 0

    async def __call__(
        self,
        *,
        event_type: EventType,
        severity: Severity = Severity.INFO,
        payload: dict[str, Any],
        push: PushPayload | None = None,
        session: Any = None,
    ) -> int:
        self.calls.append(
            {
                "event_type": event_type,
                "severity": severity,
                "payload": payload,
                "push": push,
            }
        )
        if self._raises:
            raise RuntimeError("simulated NOTIFY/DB hiccup")
        self._n += 1
        return self._n

    def of(self, et: EventType) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["event_type"] is et]


@pytest.fixture
def emit_spy(monkeypatch: pytest.MonkeyPatch) -> _EmitSpy:
    spy = _EmitSpy()
    # safe_emit imports emit_event by name into polyecho.core.events.
    monkeypatch.setattr("polyecho.core.events.emit_event", spy)
    return spy


# ---------------------------------------------------------------------------
# A. Event emission
# ---------------------------------------------------------------------------


async def test_system_bot_restart_emitted_once(
    monkeypatch: pytest.MonkeyPatch, emit_spy: _EmitSpy
) -> None:
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    core = TraderCore(follows=[_follow()])
    await core.run_once()
    await core.run_once()  # second pass must NOT re-emit

    restarts = emit_spy.of(EventType.SYSTEM_BOT_RESTART)
    assert len(restarts) == 1
    ev = restarts[0]
    assert ev["severity"] is Severity.INFO
    assert ev["push"] is not None
    assert ev["push"].tag == "system_bot_restart"
    assert ev["push"].url == "/dashboard"


async def test_copy_fill_event_on_live_fill(
    monkeypatch: pytest.MonkeyPatch, emit_spy: _EmitSpy
) -> None:
    """A real (live-on) fill emits COPY_FILL with a zh-TW /trades push."""
    from polyecho.shared.config import get_settings

    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "1")
    get_settings.cache_clear()

    async def _client_factory(_ev: Any) -> Any:
        class _C:
            async def place_order(self, **_kw: Any) -> dict[str, Any]:
                return {"status": "matched"}

        return _C()

    core = TraderCore(follows=[_follow()])
    core._executor = OrderExecutor(
        client_factory=_client_factory, ledger=core._ledger
    )
    try:
        await core.process_detected(_trade(side="BUY", price=0.5, size=100.0))
    finally:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        get_settings.cache_clear()

    fills = emit_spy.of(EventType.COPY_FILL)
    assert len(fills) == 1
    push = fills[0]["push"]
    assert fills[0]["severity"] is Severity.INFO
    assert push is not None and push.url == "/trades"
    assert push.tag == "copy_fill:0xphase5beef"
    assert "買入" in push.title


async def test_risk_daily_loss_event(
    monkeypatch: pytest.MonkeyPatch, emit_spy: _EmitSpy
) -> None:
    """A daily-loss breaker reject emits RISK_DAILY_LOSS (WARNING)."""
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    core = TraderCore(
        follows=[_follow(_tpl(daily_loss_limit_usd=Decimal("1")))]
    )

    async def _ctx(ev: Any) -> Any:
        from polyecho.core.risk import RiskContext

        return RiskContext(
            template=_tpl(daily_loss_limit_usd=Decimal("1")),
            daily_realised_loss_usd=Decimal("999"),
        )

    core._risk._context_for = _ctx
    await core.process_detected(_trade(side="BUY", price=0.5, size=1000.0))

    rd = emit_spy.of(EventType.RISK_DAILY_LOSS)
    assert len(rd) == 1
    assert rd[0]["severity"] is Severity.WARNING
    assert rd[0]["push"].url == "/dashboard"


async def test_emit_is_best_effort_never_crashes_loop(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising emit_event must NOT crash / stall the trading loop."""
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()
    raising = _EmitSpy(raises=True)
    monkeypatch.setattr("polyecho.core.events.emit_event", raising)

    core = TraderCore(follows=[_follow()])
    stats = await core.run_once()  # must complete despite emit raising

    assert stats.detected > 0
    assert stats.placed == 0
    assert len(raising.calls) >= 1  # it WAS called (and swallowed)


# ---------------------------------------------------------------------------
# B. 風控收斂 (risk convergence)
# ---------------------------------------------------------------------------


async def test_conservative_mode_tightens_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONSERVATIVE clamps the template before the gate evaluates it.

    A trade that passes under NORMAL (price 0.85 inside the loose band
    0.02–0.98) is rejected under CONSERVATIVE (band clamped to
    0.10–0.80 by ``apply_conservative``).
    """
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    mode = {"v": RiskMode.NORMAL}

    async def _get_mode(session: Any = None) -> RiskMode:
        return mode["v"]

    monkeypatch.setattr("polyecho.core.trader.get_risk_mode", _get_mode)

    # NORMAL: price 0.85 is inside [0.02, 0.98] ⇒ copied.
    core_n = TraderCore(follows=[_follow()])
    await core_n.process_detected(_trade(price=0.85, size=1000.0))
    assert core_n.stats.copied == 1

    # CONSERVATIVE: band clamped to [0.10, 0.80] ⇒ price 0.85 is now
    # outside the (tightened) band, so the gate rejects it.  The evaluator
    # still uses the raw snapshot (so it COPYs); the gate sees the
    # conservative template via _default_risk_context and rejects.
    mode["v"] = RiskMode.CONSERVATIVE
    core_c = TraderCore(follows=[_follow()])
    await core_c.process_detected(_trade(price=0.85, size=1000.0))
    assert core_c.stats.copied == 0
    assert any(
        r == "filter_price_band" for r, _ in core_c.skips
    ), core_c.skips


async def test_conservative_mode_failsafe_normal_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising get_risk_mode degrades to NORMAL (never crashes)."""
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    async def _boom(session: Any = None) -> RiskMode:
        raise RuntimeError("settings DB down")

    monkeypatch.setattr("polyecho.core.trader.get_risk_mode", _boom)
    core = TraderCore(follows=[_follow()])
    await core.process_detected(_trade(price=0.5, size=1000.0))
    assert core.stats.copied == 1  # NORMAL path, not crashed


# ---------------------------------------------------------------------------
# C. Confirmed-pending-action executor
# ---------------------------------------------------------------------------


def _row(
    action_type: PendingActionType = PendingActionType.MANUAL_SELL,
    status: PendingActionStatus = PendingActionStatus.CONFIRMED,
    **params: Any,
) -> PendingActionRow:
    base: dict[str, Any] = {
        "account_id": str(ACCT),
        "token_id": "tok-fed-cut-yes",
        "condition_id": "0xcond",
        "outcome": "Yes",
        "size": "5",
        "price": "0.4",
    }
    base.update(params)
    return PendingActionRow(
        id=str(uuid.uuid4()),
        action_type=action_type,
        status=str(status),
        params=base,
    )


async def test_confirmed_manual_sell_routes_through_chokepoint_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed MANUAL_SELL places NOTHING with the live lock OFF."""
    from polyecho.shared.config import get_settings
    from polyecho.shared.copytrading import SkipReason

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    row = _row()
    marked: list[tuple[str, dict[str, Any]]] = []

    async def rows() -> list[PendingActionRow]:
        return [row]

    async def positions(_a: uuid.UUID) -> list[OpenPosition]:
        return []

    async def mark(aid: str, p: dict[str, Any]) -> None:
        marked.append((aid, p))

    ex = PendingActionExecutor(
        rows_source=rows,
        positions_for=positions,
        mark_processed=mark,
        executor=OrderExecutor(ledger=FifoLedger()),
    )
    out = await ex.poll_once()

    assert len(out) == 1
    _aid, results = out[0]
    assert len(results) == 1
    # Live lock OFF ⇒ placed=False + LIVE_LOCKED (the chokepoint gated it).
    assert results[0].placed is False
    assert results[0].skip_reason is SkipReason.LIVE_LOCKED
    assert len(marked) == 1  # idempotency marker persisted


async def test_pending_action_idempotent_not_executed_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    row = _row()

    async def rows() -> list[PendingActionRow]:
        return [row]  # SAME row object every poll

    async def positions(_a: uuid.UUID) -> list[OpenPosition]:
        return []

    calls: list[str] = []

    async def mark(aid: str, _p: dict[str, Any]) -> None:
        calls.append(aid)

    ex = PendingActionExecutor(
        rows_source=rows,
        positions_for=positions,
        mark_processed=mark,
        executor=OrderExecutor(ledger=FifoLedger()),
    )
    out1 = await ex.poll_once()
    out2 = await ex.poll_once()  # in-proc guard ⇒ no re-execution

    assert len(out1) == 1
    assert len(out2) == 0
    assert calls == [row.id]  # marked exactly once


async def test_pending_action_ignores_non_confirmed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    awaiting = _row(status=PendingActionStatus.AWAITING_CONFIRM)
    expired = _row(status=PendingActionStatus.EXPIRED)
    processed = _row()
    processed.params["_core_processed_at"] = "2026-01-01T00:00:00+00:00"

    async def rows() -> list[PendingActionRow]:
        return [awaiting, expired, processed]

    async def positions(_a: uuid.UUID) -> list[OpenPosition]:
        return []

    async def mark(_aid: str, _p: dict[str, Any]) -> None:
        return None

    ex = PendingActionExecutor(
        rows_source=rows, positions_for=positions, mark_processed=mark
    )
    out = await ex.poll_once()
    assert out == []  # nothing eligible


async def test_emergency_liquidate_sells_all_open_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from polyecho.shared.config import get_settings
    from polyecho.shared.copytrading import SkipReason

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()

    row = _row(action_type=PendingActionType.EMERGENCY_LIQUIDATE)
    pos = [
        OpenPosition(ACCT, "0xc1", "tok1", "Yes", Decimal("3"), Decimal("0.5"), BINDING),
        OpenPosition(ACCT, "0xc2", "tok2", "No", Decimal("7"), Decimal("0.3"), BINDING),
    ]
    events: list[tuple[str, dict[str, Any]]] = []

    async def rows() -> list[PendingActionRow]:
        return [row]

    async def positions(_a: uuid.UUID) -> list[OpenPosition]:
        return pos

    async def mark(_aid: str, _p: dict[str, Any]) -> None:
        return None

    async def hook(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    ex = PendingActionExecutor(
        rows_source=rows,
        positions_for=positions,
        mark_processed=mark,
        executor=OrderExecutor(ledger=FifoLedger()),
        event_hook=hook,
    )
    out = await ex.poll_once()

    assert len(out) == 1
    _aid, results = out[0]
    assert len(results) == 2  # one SELL per open position
    assert all(r.placed is False for r in results)
    assert all(r.skip_reason is SkipReason.LIVE_LOCKED for r in results)
    assert len(events) == 2  # a close event per position

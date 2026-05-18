"""
polyecho.core Phase-5 system-health push events.

Closes the two PRD §5.8 events that were DEFINED in the frozen
``polyecho.shared.events`` contract but never wired by the trader-core
loop:

* **SYSTEM_API_FAILURE** (WARNING) — a PolymarketClient call in the
  trading path fails (orderbook / risk-context / order placement).
  Debounced/coalesced per call-site: it fires once on the transition
  into a failing state, NOT once per hot-loop tick, and re-arms after a
  success so a fresh outage re-alerts.
* **SYSTEM_LOW_BALANCE** (WARNING) — the funder USDC balance falls below
  :data:`LOW_BALANCE_THRESHOLD_USD`.  Edge-triggered: emitted once on the
  downward crossing, NOT every poll, and NOT when balance ≥ threshold;
  re-arms only after the balance recovers.

Fixture-first, NO network / DB / funds — every collaborator injected /
monkeypatched.  These only ADD behaviour behind the same fixture-backed,
live-locked defaults (no Phase-4 regression).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from polyecho.core.evaluator import TemplateConfig
from polyecho.core.trader import (
    LOW_BALANCE_THRESHOLD_USD,
    FollowSpec,
    TraderCore,
)
from polyecho.shared.copytrading import WalletBinding
from polyecho.shared.events import EventType, PushPayload, Severity

LEADER = "0xswing1000000000000000000000000000000001"
ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.acct.health")
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
        tx_hash="0xhealthbeef",
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


@pytest.fixture(autouse=True)
def _live_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    from polyecho.shared.config import get_settings

    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A. SYSTEM_API_FAILURE — PolymarketClient failure in the trading path
# ---------------------------------------------------------------------------


async def test_system_api_failure_on_orderbook_fetch_failure(
    emit_spy: _EmitSpy,
) -> None:
    """A raising orderbook fetch ⇒ SYSTEM_API_FAILURE (WARNING)."""
    core = TraderCore(
        follows=[_follow(_tpl(min_depth=Decimal("1")))],
    )

    async def _boom_book(_token_id: str) -> dict[str, Any]:
        raise RuntimeError("data-api 503 from Polymarket CLOB")

    # The risk gate fetches the book once; make that PolymarketClient call
    # fail (fail-closed ⇒ DATA_UNAVAILABLE reject).
    core._risk._orderbook_for = _boom_book

    await core.process_detected(_trade())

    fails = emit_spy.of(EventType.SYSTEM_API_FAILURE)
    assert len(fails) == 1
    ev = fails[0]
    assert ev["severity"] is Severity.WARNING
    push = ev["push"]
    assert push is not None
    assert push.url == "/alerts"
    assert push.tag == "system_api_failure"
    assert push.title == "API 異常"
    assert ev["payload"]["call_site"] == "orderbook"
    assert "data-api 503" in ev["payload"]["error"]


async def test_system_api_failure_debounced_per_call_site(
    emit_spy: _EmitSpy,
) -> None:
    """A flapping API fires ONCE on the failing transition, not per tick.

    Three consecutive detected trades all hit the same raising orderbook
    fetch, yet only the first emits SYSTEM_API_FAILURE (coalesced); a
    later success re-arms so a fresh outage re-alerts exactly once.
    """
    core = TraderCore(follows=[_follow(_tpl(min_depth=Decimal("1")))])

    state = {"fail": True}

    async def _flaky_book(_token_id: str) -> dict[str, Any]:
        if state["fail"]:
            raise RuntimeError("Polymarket orderbook timeout")
        return {"bids": [{"price": "0.49", "size": "9999"}],
                "asks": [{"price": "0.51", "size": "9999"}]}

    core._risk._orderbook_for = _flaky_book

    # 3 ticks of an ongoing outage ⇒ exactly ONE push (debounced).
    for i in range(3):
        await core.process_detected(_trade(tx_hash=f"0xfail{i}"))
    assert len(emit_spy.of(EventType.SYSTEM_API_FAILURE)) == 1

    # API recovers (success clears the failing flag).
    state["fail"] = False
    await core.process_detected(_trade(tx_hash="0xok"))
    assert len(emit_spy.of(EventType.SYSTEM_API_FAILURE)) == 1  # still 1

    # Fresh outage ⇒ exactly one MORE push (re-armed transition).
    state["fail"] = True
    await core.process_detected(_trade(tx_hash="0xfail-again"))
    assert len(emit_spy.of(EventType.SYSTEM_API_FAILURE)) == 2


async def test_system_api_failure_best_effort_never_crashes_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising emit_event on the API-failure path never stalls the loop."""
    raising = _EmitSpy(raises=True)
    monkeypatch.setattr("polyecho.core.events.emit_event", raising)

    core = TraderCore(follows=[_follow(_tpl(min_depth=Decimal("1")))])

    async def _boom_book(_token_id: str) -> dict[str, Any]:
        raise RuntimeError("Polymarket down")

    core._risk._orderbook_for = _boom_book

    # Must complete (the skip is still recorded) despite emit raising.
    await core.process_detected(_trade())
    assert raising.calls  # it WAS attempted (and swallowed)
    assert any(r == "data_unavailable" for r, _ in core.skips), core.skips


async def test_no_api_failure_on_normal_risk_reject(
    emit_spy: _EmitSpy,
) -> None:
    """A normal risk reject (price band) is NOT an API outage."""
    # price 0.5 is fine; force a non-API reject via a tight price band.
    core = TraderCore(
        follows=[_follow(_tpl(min_price=Decimal("0.90")))]
    )
    await core.process_detected(_trade(price=0.5))

    assert emit_spy.of(EventType.SYSTEM_API_FAILURE) == []
    assert any(r == "filter_price_band" for r, _ in core.skips), core.skips


# ---------------------------------------------------------------------------
# B. SYSTEM_LOW_BALANCE — funder USDC balance below threshold
# ---------------------------------------------------------------------------


async def test_system_low_balance_emitted_once_below_threshold(
    emit_spy: _EmitSpy,
) -> None:
    """balance < threshold ⇒ SYSTEM_LOW_BALANCE (WARNING) exactly once."""
    low = LOW_BALANCE_THRESHOLD_USD - 1.0

    async def _balance(_account_id: uuid.UUID) -> float | None:
        return low

    core = TraderCore(follows=[_follow()], balance_for=_balance)
    await core.process_detected(_trade())

    lb = emit_spy.of(EventType.SYSTEM_LOW_BALANCE)
    assert len(lb) == 1
    ev = lb[0]
    assert ev["severity"] is Severity.WARNING
    push = ev["push"]
    assert push is not None
    assert push.url == "/accounts"
    assert push.tag == f"low_balance:{ACCT}"
    assert push.title == "餘額不足"
    assert ev["payload"]["account_id"] == str(ACCT)
    assert ev["payload"]["balance_usd"] == low
    assert ev["payload"]["threshold"] == LOW_BALANCE_THRESHOLD_USD


async def test_system_low_balance_not_emitted_at_or_above_threshold(
    emit_spy: _EmitSpy,
) -> None:
    """balance ≥ threshold ⇒ NO SYSTEM_LOW_BALANCE."""

    async def _ok_balance(_account_id: uuid.UUID) -> float | None:
        return LOW_BALANCE_THRESHOLD_USD  # exactly at threshold ⇒ fine

    core = TraderCore(follows=[_follow()], balance_for=_ok_balance)
    await core.process_detected(_trade())

    assert emit_spy.of(EventType.SYSTEM_LOW_BALANCE) == []


async def test_system_low_balance_not_repeated_every_poll(
    emit_spy: _EmitSpy,
) -> None:
    """Persistently-low balance alerts ONCE, not once per detected trade.

    Re-arms only after the balance recovers ≥ threshold; a fresh dip
    below then alerts exactly one more time (edge-triggered).
    """
    bal = {"v": LOW_BALANCE_THRESHOLD_USD - 2.0}

    async def _balance(_account_id: uuid.UUID) -> float | None:
        return bal["v"]

    core = TraderCore(follows=[_follow()], balance_for=_balance)

    # 4 polls while persistently low ⇒ exactly ONE push.
    for i in range(4):
        await core.process_detected(_trade(tx_hash=f"0xlow{i}"))
    assert len(emit_spy.of(EventType.SYSTEM_LOW_BALANCE)) == 1

    # Recovers (re-arms the edge trigger).
    bal["v"] = LOW_BALANCE_THRESHOLD_USD + 10.0
    await core.process_detected(_trade(tx_hash="0xrecovered"))
    assert len(emit_spy.of(EventType.SYSTEM_LOW_BALANCE)) == 1  # still 1

    # Dips below again ⇒ exactly one MORE push.
    bal["v"] = LOW_BALANCE_THRESHOLD_USD - 0.5
    await core.process_detected(_trade(tx_hash="0xlow-again"))
    assert len(emit_spy.of(EventType.SYSTEM_LOW_BALANCE)) == 2


async def test_system_low_balance_no_probe_is_noop(
    emit_spy: _EmitSpy,
) -> None:
    """Without an injected balance probe (default), nothing is emitted."""
    core = TraderCore(follows=[_follow()])  # no balance_for
    await core.process_detected(_trade())
    assert emit_spy.of(EventType.SYSTEM_LOW_BALANCE) == []


async def test_system_low_balance_probe_error_swallowed(
    emit_spy: _EmitSpy,
) -> None:
    """A raising balance probe never crashes the loop (fails open)."""

    async def _boom(_account_id: uuid.UUID) -> float | None:
        raise RuntimeError("RPC node unreachable")

    core = TraderCore(follows=[_follow()], balance_for=_boom)
    await core.process_detected(_trade())  # must not raise

    assert emit_spy.of(EventType.SYSTEM_LOW_BALANCE) == []
    assert core.stats.detected == 1  # loop completed normally


async def test_system_low_balance_unknown_balance_never_alarms(
    emit_spy: _EmitSpy,
) -> None:
    """A ``None`` balance (unknown) ⇒ never alarm."""

    async def _unknown(_account_id: uuid.UUID) -> float | None:
        return None

    core = TraderCore(follows=[_follow()], balance_for=_unknown)
    await core.process_detected(_trade())
    assert emit_spy.of(EventType.SYSTEM_LOW_BALANCE) == []

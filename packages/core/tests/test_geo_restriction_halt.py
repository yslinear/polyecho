"""
polyecho.core C5 geo-restriction emergency-stop tests.

C5 (PolyHermes deep-dive): Polymarket geo-restricts trading.  When a CLOB
``place_order`` is rejected because trading is region-blocked, trader-core
must auto-trip the documented emergency-stop posture (the R24 runbook does
this by hand):

* set ``settings.global_paused = true`` (via the injected writer),
* emit a CRITICAL :class:`EventType.SYSTEM_API_FAILURE` event so /alerts +
  Web Push surface it loudly, and
* stop submitting any further order for the rest of the pass.

It MUST be fail-safe but NOT false-positive: a transient network error
(re-sign/retry territory) must run the ordinary retry path and NEVER trip
the halt.

Fixture-first: no network / DB / funds — the executor's client_factory
and the global-pause writer are injected.  pytest-asyncio ``asyncio_mode
= "auto"`` ⇒ bare ``async def test_*`` run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from polyecho.core.evaluator import TemplateConfig
from polyecho.core.execution import OrderExecutor
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.copytrading import WalletBinding
from polyecho.shared.events import EventType, Severity
from polyecho.shared.polymarket.client import GeoRestrictionError

LEADER = "0xswing1000000000000000000000000000000001"
ACCT = uuid.uuid5(uuid.NAMESPACE_DNS, "test.geo.acct")
BINDING = WalletBinding(signature_type=0, funder=LEADER, wallet_mode="eoa", derived=False)


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


def _follow() -> FollowSpec:
    return FollowSpec(
        account_id=ACCT,
        leader_address=LEADER,
        template=_tpl(),
        binding=BINDING,
    )


def _trade(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        leader_address=LEADER,
        tx_hash="0xgeobeef",
        condition_id="0xcond_geo",
        market_id="0xcond_geo",
        side="BUY",
        size=100.0,
        price=0.5,
        outcome="Yes",
        token_id="tok-geo-yes",
    )
    base.update(kw)
    return base


class _PauseSpy:
    """Records every ``global_pause_writer`` call."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def __call__(self, paused: bool) -> None:
        self.calls.append(paused)


def _client_factory_raising(exc: Exception) -> Any:
    """A client_factory whose ``place_order`` always raises *exc*."""

    async def _factory(_ev: Any) -> Any:
        class _C:
            async def place_order(self, **_kw: Any) -> dict[str, Any]:
                raise exc

        return _C()

    return _factory


@pytest.fixture(autouse=True)
def _live_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Live trading on so the executor reaches the real place_order path."""
    from polyecho.shared.config import get_settings

    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "1")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A geo-restriction CLOB error trips the emergency-stop
# ---------------------------------------------------------------------------


async def test_geo_restriction_triggers_global_pause() -> None:
    """A CLOB geo-restriction sets ``global_paused=true`` via the writer."""
    pause_spy = _PauseSpy()
    core = TraderCore(follows=[_follow()], global_pause_writer=pause_spy)
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(
            GeoRestrictionError("CLOB place_order rejected: geo-restricted")
        ),
        ledger=core._ledger,
    )

    await core.process_detected(_trade())

    # The documented soft-pause kill-switch was tripped.
    assert pause_spy.calls == [True]
    assert core._geo_halted is True


async def test_geo_restriction_emits_critical_event() -> None:
    """The halt emits a CRITICAL SYSTEM_API_FAILURE event for /alerts + push."""
    core = TraderCore(follows=[_follow()], global_pause_writer=_PauseSpy())
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(
            GeoRestrictionError("trading restricted in your region")
        ),
        ledger=core._ledger,
    )

    await core.process_detected(_trade())

    geo_events = [
        (et, payload)
        for et, payload in core.events
        if et == str(EventType.SYSTEM_API_FAILURE) and payload.get("reason") == "geo_restriction"
    ]
    assert len(geo_events) == 1
    _et, payload = geo_events[0]
    assert payload["global_paused"] is True
    # The event carries CRITICAL severity (logged on the push-event mirror).


async def test_geo_restriction_places_no_order_and_halts_later_follows() -> None:
    """Once halted, NO order is placed and later trades are skipped."""
    core = TraderCore(follows=[_follow()], global_pause_writer=_PauseSpy())
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(
            GeoRestrictionError("trading restricted in your region")
        ),
        ledger=core._ledger,
    )

    await core.process_detected(_trade(tx_hash="0xgeo1"))
    assert core.stats.placed == 0
    assert core._geo_halted is True

    # A SECOND detected trade after the halt must be skipped without ever
    # reaching the executor (the in-proc latch short-circuits it).
    placed_before = core.stats.placed
    await core.process_detected(_trade(tx_hash="0xgeo2"))
    assert core.stats.placed == placed_before  # still nothing placed
    assert any("geo-restriction" in detail for _reason, detail in core.skips)


async def test_geo_halt_writer_failure_still_latches() -> None:
    """Even if the global_paused write fails, the in-proc latch still halts."""

    async def _failing_writer(_paused: bool) -> None:
        raise RuntimeError("simulated DB outage")

    core = TraderCore(follows=[_follow()], global_pause_writer=_failing_writer)
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(
            GeoRestrictionError("trading restricted in your region")
        ),
        ledger=core._ledger,
    )

    # The writer raising must NOT crash the loop.
    await core.process_detected(_trade())
    assert core._geo_halted is True  # latched despite the write failure
    assert core.stats.placed == 0


# ---------------------------------------------------------------------------
# A transient / ordinary CLOB error must NOT trip the halt
# ---------------------------------------------------------------------------


async def test_transient_error_does_not_trigger_halt() -> None:
    """A plain transient network error runs the retry path, never halts.

    A geo-halt is fail-safe, but a FALSE halt on routine flakiness is an
    availability bug — so a non-geo error must leave ``global_paused``
    untouched and ``_geo_halted`` False.
    """
    pause_spy = _PauseSpy()
    core = TraderCore(follows=[_follow()], global_pause_writer=pause_spy)
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(RuntimeError("Connection reset by peer")),
        ledger=core._ledger,
    )

    await core.process_detected(_trade())

    # No emergency-stop: the writer was never called, no latch.
    assert pause_spy.calls == []
    assert core._geo_halted is False
    assert core.stats.placed == 0  # the order still failed — just not a halt
    # No geo-restriction event was emitted.
    assert not [
        1
        for et, payload in core.events
        if et == str(EventType.SYSTEM_API_FAILURE) and payload.get("reason") == "geo_restriction"
    ]


async def test_geo_halt_event_severity_is_critical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The geo-halt event is emitted at CRITICAL severity (not WARNING)."""
    captured: list[tuple[Any, Any]] = []

    async def _spy(
        *,
        event_type: Any,
        severity: Any = Severity.INFO,
        payload: dict[str, Any],
        push: Any = None,
        session: Any = None,
    ) -> int:
        captured.append((event_type, severity))
        return 1

    monkeypatch.setattr("polyecho.core.events.emit_event", _spy)

    core = TraderCore(follows=[_follow()], global_pause_writer=_PauseSpy())
    core._executor = OrderExecutor(
        client_factory=_client_factory_raising(
            GeoRestrictionError("trading restricted in your region")
        ),
        ledger=core._ledger,
    )
    await core.process_detected(_trade())

    geo = [sev for et, sev in captured if et is EventType.SYSTEM_API_FAILURE]
    assert Severity.CRITICAL in geo

"""
test_onchain_reconcile
======================
Unit tests for :mod:`polyecho.core.onchain_reconcile` — the on-chain ↔ DB
position / balance reconcile (PRD §7.3/§7.4; Wave C C-observability).

No network, no Postgres, no node: the on-chain reader and the DB-state
reader are both injected, so every case is a pure in-memory comparison.
``asyncio_mode = "auto"`` (root pyproject) ⇒ bare ``async def test_*``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from polyecho.core.onchain_reconcile import (
    OnChainReader,
    ReconcileReport,
    TrackedPosition,
    main,
    run_reconcile,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakeReader(OnChainReader):
    """In-memory on-chain truth keyed by funder address."""

    def __init__(
        self,
        balances: dict[str, Decimal],
        positions: dict[str, dict[int, Decimal]] | None = None,
        *,
        fail_for: set[str] | None = None,
    ) -> None:
        self._balances = balances
        self._positions = positions or {}
        self._fail_for = fail_for or set()

    async def read(
        self, funder: str, ctf_token_ids: Any
    ) -> tuple[Decimal, dict[int, Decimal]]:
        if funder in self._fail_for:
            raise RuntimeError(f"simulated RPC error for {funder}")
        return (
            self._balances.get(funder, Decimal(0)),
            dict(self._positions.get(funder, {})),
        )


def _db_state_factory(
    state: dict[str, tuple[str, Decimal, list[TrackedPosition]]],
) -> Any:
    async def _reader(_session: Any) -> Any:
        return state

    return _reader


class _EmitSpy:
    """Records every ``emit_event`` call (monkeypatched into shared)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return len(self.calls)


@pytest.fixture
def emit_spy(monkeypatch: pytest.MonkeyPatch) -> _EmitSpy:
    spy = _EmitSpy()
    # safe_emit (core.events) delegates to this symbol; patch at source.
    monkeypatch.setattr("polyecho.core.events.emit_event", spy)
    return spy


def _pos(account: str, market: str, token_id: int | None, shares: str) -> TrackedPosition:
    return TrackedPosition(
        account_id=account,
        market_id=market,
        condition_id=f"cond-{market}",
        outcome="YES",
        ctf_token_id=token_id,
        db_shares=Decimal(shares),
    )


# ---------------------------------------------------------------------------
# Matched case — no drift ⇒ empty drift, NO event
# ---------------------------------------------------------------------------
async def test_matched_no_drift_emits_no_event(emit_spy: _EmitSpy) -> None:
    state = {
        "acct-1": (
            "0xfund1",
            Decimal("100.00"),
            [_pos("acct-1", "mkt-a", 111, "5")],
        )
    }
    reader = _FakeReader(
        balances={"0xfund1": Decimal("100.00")},
        positions={"0xfund1": {111: Decimal("5")}},
    )

    report = await run_reconcile(
        session=object(),
        reader=reader,
        db_state=_db_state_factory(state),
    )

    assert isinstance(report, ReconcileReport)
    assert report.has_drift is False
    assert report.drift_count == 0
    assert report.accounts_checked == 1
    assert report.matched_balances == 1
    assert report.matched_positions == 1
    assert emit_spy.calls == []  # no event on a clean pass


# ---------------------------------------------------------------------------
# Drift case — on-chain != DB ⇒ report lists it + WARNING event
# ---------------------------------------------------------------------------
async def test_balance_and_position_drift_reported_and_event(
    emit_spy: _EmitSpy,
) -> None:
    state = {
        "acct-1": (
            "0xfund1",
            Decimal("100.00"),
            [_pos("acct-1", "mkt-a", 111, "5")],
        )
    }
    reader = _FakeReader(
        balances={"0xfund1": Decimal("42.00")},          # collateral drift
        positions={"0xfund1": {111: Decimal("3")}},      # position drift
    )

    report = await run_reconcile(
        session=object(),
        reader=reader,
        db_state=_db_state_factory(state),
    )

    assert report.has_drift is True
    assert len(report.balance_drift) == 1
    assert len(report.position_drift) == 1
    bd = report.balance_drift[0]
    assert bd.db_balance == Decimal("100.00")
    assert bd.onchain_balance == Decimal("42.00")
    assert bd.delta == Decimal("-58.00")
    pd = report.position_drift[0]
    assert pd.db_shares == Decimal("5")
    assert pd.onchain_shares == Decimal("3")
    assert pd.delta == Decimal("-2")

    # Exactly one WARNING event emitted on drift.
    assert len(emit_spy.calls) == 1
    call = emit_spy.calls[0]
    assert str(call["severity"]) == "warning"
    assert call["payload"]["kind"] == "onchain_db_reconcile"


async def test_within_tolerance_is_matched_not_drift(emit_spy: _EmitSpy) -> None:
    state = {
        "a": ("0xf", Decimal("100.000"), [_pos("a", "m", 1, "5.0000001")]),
    }
    reader = _FakeReader(
        balances={"0xf": Decimal("100.005")},          # < 0.01 tolerance
        positions={"0xf": {1: Decimal("5.0000002")}},  # < 1e-6 tolerance
    )
    report = await run_reconcile(
        session=object(), reader=reader, db_state=_db_state_factory(state)
    )
    assert report.has_drift is False
    assert report.matched_balances == 1
    assert report.matched_positions == 1
    assert emit_spy.calls == []


async def test_unverifiable_position_no_token_id_is_drift(
    emit_spy: _EmitSpy,
) -> None:
    """A DB-open position with no ERC-1155 token id is flagged, not matched."""
    state = {
        "a": ("0xf", Decimal("0"), [_pos("a", "m", None, "7")]),
    }
    reader = _FakeReader(balances={"0xf": Decimal("0")})
    report = await run_reconcile(
        session=object(), reader=reader, db_state=_db_state_factory(state)
    )
    assert report.has_drift is True
    assert len(report.position_drift) == 1
    assert report.position_drift[0].ctf_token_id is None
    assert report.matched_positions == 0


# ---------------------------------------------------------------------------
# Per-account RPC error is isolated — one bad account never aborts the pass
# ---------------------------------------------------------------------------
async def test_per_account_rpc_error_isolated(emit_spy: _EmitSpy) -> None:
    state = {
        "bad": ("0xbad", Decimal("10"), [_pos("bad", "m1", 1, "1")]),
        "good": ("0xgood", Decimal("50"), [_pos("good", "m2", 2, "4")]),
    }
    reader = _FakeReader(
        balances={"0xgood": Decimal("50")},
        positions={"0xgood": {2: Decimal("4")}},
        fail_for={"0xbad"},
    )

    report = await run_reconcile(
        session=object(),
        reader=reader,
        db_state=_db_state_factory(state),
    )

    # The bad account is recorded as failed, NOT as drift, and the good
    # account is still fully reconciled (matched) — the pass did not abort.
    assert report.accounts_failed == ["bad"]
    assert report.accounts_checked == 1
    assert report.matched_balances == 1
    assert report.matched_positions == 1
    assert report.has_drift is False
    # accounts_failed alone is not "drift" ⇒ no event.
    assert emit_spy.calls == []


# ---------------------------------------------------------------------------
# run_reconcile returns the dataclass; summary() is JSON-friendly
# ---------------------------------------------------------------------------
async def test_run_reconcile_returns_dataclass_with_summary(
    emit_spy: _EmitSpy,
) -> None:
    state: dict[str, tuple[str, Decimal, list[TrackedPosition]]] = {
        "a": ("0xf", Decimal("1"), [])
    }
    reader = _FakeReader(balances={"0xf": Decimal("1")})
    report = await run_reconcile(
        session=object(), reader=reader, db_state=_db_state_factory(state)
    )
    assert isinstance(report, ReconcileReport)
    summary = report.summary()
    assert summary["accounts_checked"] == 1
    assert summary["balance_drift"] == []
    assert summary["position_drift"] == []


# ---------------------------------------------------------------------------
# Module entrypoint: ``python -m polyecho.core.onchain_reconcile`` exit 0
# ---------------------------------------------------------------------------
def test_main_once_exits_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(**_kw: Any) -> ReconcileReport:
        from datetime import UTC, datetime

        return ReconcileReport(started_at=datetime.now(tz=UTC))

    monkeypatch.setattr(
        "polyecho.core.onchain_reconcile.run_reconcile", _fake_run
    )
    assert main(["--once"]) == 0


def test_main_once_exits_zero_even_with_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detected drift is a successful observation ⇒ still exit 0."""
    from datetime import UTC, datetime

    from polyecho.core.onchain_reconcile import BalanceDrift

    async def _fake_run(**_kw: Any) -> ReconcileReport:
        rep = ReconcileReport(started_at=datetime.now(tz=UTC))
        rep.balance_drift.append(
            BalanceDrift(
                account_id="a",
                funder="0xf",
                db_balance=Decimal("1"),
                onchain_balance=Decimal("2"),
            )
        )
        return rep

    monkeypatch.setattr(
        "polyecho.core.onchain_reconcile.run_reconcile", _fake_run
    )
    assert main(["--once"]) == 0


def test_main_nonzero_on_real_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(**_kw: Any) -> ReconcileReport:
        raise RuntimeError("DB unreachable")

    monkeypatch.setattr(
        "polyecho.core.onchain_reconcile.run_reconcile", _boom
    )
    assert main(["--once"]) == 1


# ---------------------------------------------------------------------------
# run_forever: a failing iteration is swallowed; the loop bound terminates
# ---------------------------------------------------------------------------
async def test_run_forever_bounded_and_iteration_error_swallowed() -> None:
    from polyecho.core.onchain_reconcile import run_forever

    calls = {"n": 0}

    async def _flaky_run(**_kw: Any) -> ReconcileReport:
        from datetime import UTC, datetime

        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return ReconcileReport(started_at=datetime.now(tz=UTC))

    import polyecho.core.onchain_reconcile as mod

    orig = mod.run_reconcile
    mod.run_reconcile = _flaky_run
    try:
        # interval 0 ⇒ no real sleep; 2 iterations then return.
        await run_forever(interval_seconds=0, _max_iterations=2)
    finally:
        mod.run_reconcile = orig
    assert calls["n"] == 2  # first raised+swallowed, second succeeded

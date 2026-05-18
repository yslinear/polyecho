"""
tests/test_risk_mode.py
=======================
Phase-5 Wave-1 — :mod:`polyecho.shared.risk_mode` binding contract.

Covers: enum identity; :func:`get_risk_mode` fail-safe default + the
bare-string / ``{"value": ...}`` coercion; and table-driven
:func:`apply_conservative` clamps proving *tighten-only, never loosen*
plus PURE (no mutation, returns a new object).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from polyecho.shared.risk_mode import (
    RISK_MODE_SETTINGS_KEY,
    ConservativeTemplate,
    RiskMode,
    apply_conservative,
    get_risk_mode,
)

from .conftest import run


@dataclass
class _Tpl:
    """Structural stand-in for core's ``TemplateConfig`` (same field names)."""

    copy_ratio: Decimal | None = None
    max_position_usd: Decimal | None = None
    daily_loss_limit_usd: Decimal | None = None
    daily_order_cap: int | None = None
    min_price: Decimal = Decimal("0.02")
    max_price: Decimal = Decimal("0.98")
    min_depth: Decimal = Decimal("0")
    max_spread: Decimal = Decimal("0.10")
    price_tolerance_pct: Decimal = Decimal("3")


def test_risk_mode_enum_and_key() -> None:
    assert RiskMode.NORMAL.value == "normal"
    assert RiskMode.CONSERVATIVE.value == "conservative"
    assert {m.value for m in RiskMode} == {"normal", "conservative"}
    assert RISK_MODE_SETTINGS_KEY == "risk_mode"


def test_tpl_satisfies_protocol() -> None:
    assert isinstance(_Tpl(), ConservativeTemplate)


# --- get_risk_mode --------------------------------------------------------


class _Row:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Sess:
    """Async session double whose execute() returns a stored Setting row."""

    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, _stmt: Any) -> Any:
        class _R:
            def __init__(self, row: Any) -> None:
                self._row = row

            def scalar_one_or_none(self) -> Any:
                return self._row

        return _R(self._row)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("conservative", RiskMode.CONSERVATIVE),
        ("normal", RiskMode.NORMAL),
        ("CONSERVATIVE", RiskMode.CONSERVATIVE),  # case-insensitive
        ({"value": "conservative"}, RiskMode.CONSERVATIVE),  # dict shape
        ("garbage", RiskMode.NORMAL),  # invalid → fail-safe NORMAL
        (None, RiskMode.NORMAL),  # null value → NORMAL
        (123, RiskMode.NORMAL),  # wrong type → NORMAL
    ],
)
def test_get_risk_mode_coercion(stored: Any, expected: RiskMode) -> None:
    mode = run(get_risk_mode(session=_Sess(_Row(stored))))
    assert mode is expected


def test_get_risk_mode_absent_row_defaults_normal() -> None:
    assert run(get_risk_mode(session=_Sess(None))) is RiskMode.NORMAL


def test_get_risk_mode_db_error_defaults_normal() -> None:
    class _Boom:
        async def execute(self, _s: Any) -> Any:
            raise RuntimeError("db down")

    assert run(get_risk_mode(session=_Boom())) is RiskMode.NORMAL


# --- apply_conservative: tighten-only, never loosen, pure -----------------


def test_apply_conservative_clamps_loose_values_down() -> None:
    loose = _Tpl(
        copy_ratio=Decimal("0.01"),  # 1% → must drop to 0.0005
        max_position_usd=Decimal("500"),  # → 10
        daily_loss_limit_usd=Decimal("300"),  # → 10
        daily_order_cap=200,  # → 20
        min_price=Decimal("0.02"),  # → raised to 0.10
        max_price=Decimal("0.98"),  # → lowered to 0.80
        min_depth=Decimal("0"),  # → raised to 100
        max_spread=Decimal("0.10"),  # → 0.03
        price_tolerance_pct=Decimal("3"),  # already at ceiling
    )
    out = apply_conservative(loose)
    assert out.copy_ratio == Decimal("0.0005")
    assert out.max_position_usd == Decimal("10")
    assert out.daily_loss_limit_usd == Decimal("10")
    assert out.daily_order_cap == 20
    assert out.min_price == Decimal("0.10")
    assert out.max_price == Decimal("0.80")
    assert out.min_depth == Decimal("100")
    assert out.max_spread == Decimal("0.03")
    assert out.price_tolerance_pct == Decimal("3")


def test_apply_conservative_never_loosens_stricter_values() -> None:
    strict = _Tpl(
        copy_ratio=Decimal("0.0001"),  # already < ceiling → keep
        max_position_usd=Decimal("5"),  # < 10 → keep
        daily_loss_limit_usd=Decimal("3"),  # keep
        daily_order_cap=5,  # keep
        min_price=Decimal("0.20"),  # > 0.10 → keep (don't lower floor)
        max_price=Decimal("0.60"),  # < 0.80 → keep (don't raise cap)
        min_depth=Decimal("500"),  # > 100 → keep
        max_spread=Decimal("0.01"),  # keep
        price_tolerance_pct=Decimal("1"),  # keep
    )
    out = apply_conservative(strict)
    assert out.copy_ratio == Decimal("0.0001")
    assert out.max_position_usd == Decimal("5")
    assert out.daily_loss_limit_usd == Decimal("3")
    assert out.daily_order_cap == 5
    assert out.min_price == Decimal("0.20")
    assert out.max_price == Decimal("0.60")
    assert out.min_depth == Decimal("500")
    assert out.max_spread == Decimal("0.01")
    assert out.price_tolerance_pct == Decimal("1")


def test_apply_conservative_none_optionals_get_ceilings_but_ratio_stays() -> None:
    out = apply_conservative(_Tpl())  # all optionals None
    # Unset caps must be imposed (Conservative MUST have a cap)…
    assert out.max_position_usd == Decimal("10")
    assert out.daily_loss_limit_usd == Decimal("10")
    assert out.daily_order_cap == 20
    # …but copy_ratio None = fixed-size mode → must remain None.
    assert out.copy_ratio is None


def test_apply_conservative_is_pure_no_mutation() -> None:
    src = _Tpl(copy_ratio=Decimal("0.5"), max_position_usd=Decimal("999"))
    out = apply_conservative(src)
    assert out is not src
    # Original untouched.
    assert src.copy_ratio == Decimal("0.5")
    assert src.max_position_usd == Decimal("999")


def test_apply_conservative_resolves_contradictory_price_band(
    caplog: Any,
) -> None:
    """R2: a template ``min_price`` (0.85) above the Conservative
    ``max_price`` ceiling (0.80) would, with independent per-field clamps,
    produce ``min_price (0.85) > max_price (0.80)`` — an
    internally-contradictory band that rejects EVERY price.  The fix clamps
    ``min_price`` DOWN to ``max_price`` (still tighten-only) and logs."""
    import logging

    contradictory = _Tpl(
        min_price=Decimal("0.85"),  # > Conservative max ceiling 0.80
        max_price=Decimal("0.95"),  # → lowered to 0.80
    )
    with caplog.at_level(
        logging.WARNING, logger="polyecho.shared.risk_mode"
    ):
        out = apply_conservative(contradictory)

    assert out.max_price == Decimal("0.80")
    assert out.min_price == Decimal("0.80")  # clamped down to max
    assert out.min_price <= out.max_price  # band is now self-consistent
    # Still strictly tighten-only: the resulting min is not above the
    # caller's input min (0.85) nor outside the Conservative band.
    assert out.min_price <= contradictory.min_price
    assert any(
        "conservative_band_contradiction_clamped" in r.message
        for r in caplog.records
    )


def test_apply_conservative_non_contradictory_band_unaffected() -> None:
    """The contradiction guard must not perturb a normal band."""
    ok = _Tpl(min_price=Decimal("0.30"), max_price=Decimal("0.70"))
    out = apply_conservative(ok)
    assert out.min_price == Decimal("0.30")  # > 0.10 floor → kept
    assert out.max_price == Decimal("0.70")  # < 0.80 ceiling → kept

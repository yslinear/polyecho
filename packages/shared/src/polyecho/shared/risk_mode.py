"""
polyecho.shared.risk_mode
=========================
**FROZEN Phase-5 Wave-1 contract module.**

風控收斂 / "travel-conservative" contract (PRD §5.6 Conservative preset,
``docs/reference/polyhermes-analysis.md`` R4 ceilings).  Wave-2 (core
gate, cli, web /settings) imports :class:`RiskMode`, the settings key and
:func:`get_risk_mode` / :func:`apply_conservative` *verbatim*.

What lives here (and only here)
-------------------------------
* :class:`RiskMode`               — ``NORMAL`` | ``CONSERVATIVE``.
* :data:`RISK_MODE_SETTINGS_KEY`  — the ``settings`` table row key.
* :func:`get_risk_mode`           — read the row, fail-safe to ``NORMAL``.
* :func:`apply_conservative`      — PURE: tighten a template-config object
                                    to the Conservative ceilings.
* :class:`ConservativeTemplate`   — the structural :class:`typing.Protocol`
                                    describing only the fields we clamp
                                    (so ``shared`` has **no** core import).

No network / DB / SDK I/O on import.  Pure types + one DB reader.

Storage convention
-------------------
``RISK_MODE_SETTINGS_KEY`` is a single ``settings`` row whose jsonb
``value`` is the **bare string** ``"normal"`` / ``"conservative"`` —
mirroring exactly how the web layer stores ``polymarket_proxy_url``
(``packages/web/app/settings/actions.ts`` upserts a bare string; the
shared resolver in ``http.py`` also tolerates ``{"value": "..."}``).
:func:`get_risk_mode` tolerates the same two shapes and ALWAYS
fail-safes to :attr:`RiskMode.NORMAL` (absent / invalid / DB error) so a
misconfigured row can never crash a trading path.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "RISK_MODE_SETTINGS_KEY",
    "ConservativeTemplate",
    "RiskMode",
    "apply_conservative",
    "get_risk_mode",
]


class RiskMode(StrEnum):
    """The dashboard-controlled global risk posture (PRD §5.6, §8.1).

    ``NORMAL`` runs the applied per-(account, leader) template as-is;
    ``CONSERVATIVE`` makes Wave-2's gate run every template through
    :func:`apply_conservative` first (tighten-only, never loosen).
    """

    NORMAL = "normal"
    CONSERVATIVE = "conservative"


#: ``settings`` table row key (bare-string-jsonb convention — see module
#: docstring).  Frozen: the web /settings page writes exactly this key.
RISK_MODE_SETTINGS_KEY: Final = "risk_mode"


@runtime_checkable
class ConservativeTemplate(Protocol):
    """Structural view of *only* the template-config fields we tighten.

    Defined here as a :class:`typing.Protocol` so ``packages/shared`` has
    **no dependency on ``packages/core``** — the Phase-4 evaluator's
    ``TemplateConfig`` (a frozen dataclass) satisfies this structurally.
    Field names mirror ``polyecho.core.evaluator.TemplateConfig`` exactly:

    * ``copy_ratio``           — sizing ratio (``None`` = fixed-size mode).
    * ``max_position_usd``     — per-position value cap.
    * ``daily_loss_limit_usd`` — daily realised-loss circuit breaker.
    * ``daily_order_cap``      — per-day order count cap.
    * ``min_price`` / ``max_price`` — tradable price band.
    * ``min_depth``            — minimum top-of-book depth required.
    * ``max_spread``           — maximum tolerated bid/ask spread.
    * ``price_tolerance_pct``  — slippage-vs-best-ask tolerance (%).
    """

    copy_ratio: Decimal | None
    max_position_usd: Decimal | None
    daily_loss_limit_usd: Decimal | None
    daily_order_cap: int | None
    min_price: Decimal
    max_price: Decimal
    min_depth: Decimal
    max_spread: Decimal
    price_tolerance_pct: Decimal


# --- PRD §5.6 / polyhermes-analysis.md R4 Conservative ceilings ------------
# "maxDailyOrders ≤20, maxDailyLoss ≤$10, minPrice 0.10, maxPrice 0.80,
#  maxPositionValue ≤$10, minOrderDepth ≥100, maxSpread ≤0.03,
#  priceTolerance ≤3%"  (R4) +  PRD §5.6 "Conservative: 0.05% ratio".
_C_COPY_RATIO_MAX: Final = Decimal("0.0005")  # §5.6: 0.05% ratio ceiling
_C_MAX_POSITION_USD: Final = Decimal("10")  # R4: maxPositionValue ≤ $10
_C_DAILY_LOSS_USD: Final = Decimal("10")  # R4: maxDailyLoss ≤ $10
_C_DAILY_ORDER_CAP: Final = 20  # R4: maxDailyOrders ≤ 20
_C_MIN_PRICE: Final = Decimal("0.10")  # R4: minPrice 0.10 (band floor)
_C_MAX_PRICE: Final = Decimal("0.80")  # R4: maxPrice 0.80 (band ceiling)
_C_MIN_DEPTH: Final = Decimal("100")  # R4: minOrderDepth ≥ 100
_C_MAX_SPREAD: Final = Decimal("0.03")  # R4: maxSpread ≤ 0.03
_C_PRICE_TOL_PCT: Final = Decimal("3")  # R4: priceTolerance ≤ 3%


def _coerce_mode(value: Any) -> RiskMode | None:
    """Coerce a stored ``settings.value`` into a :class:`RiskMode`.

    Tolerates a bare string or ``{"value": "..."}`` (same dual shape the
    proxy resolver accepts).  Returns ``None`` on anything unrecognised so
    the caller can fail-safe.
    """
    raw: Any = value
    if isinstance(value, dict):
        raw = value.get("value")
    if not isinstance(raw, str):
        return None
    try:
        return RiskMode(raw.strip().lower())
    except ValueError:
        return None


async def get_risk_mode(session: Any = None) -> RiskMode:
    """Return the active :class:`RiskMode`, fail-safe to :attr:`NORMAL`.

    Reads the ``settings`` row keyed :data:`RISK_MODE_SETTINGS_KEY`.  Any
    problem — row absent, value unparseable, import/DB failure — degrades
    silently to :attr:`RiskMode.NORMAL` (never raises): a misconfigured
    settings row must not be able to crash a trading path.

    Args:
        session: Optional caller :class:`AsyncSession`; if ``None`` a new
            :func:`~polyecho.shared.db.session_scope` is opened.
    """
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Setting

        async def _read(sess: Any) -> RiskMode | None:
            row = (
                await sess.execute(
                    select(Setting).where(Setting.key == RISK_MODE_SETTINGS_KEY)
                )
            ).scalar_one_or_none()
            return _coerce_mode(row.value) if row is not None else None

        if session is not None:
            mode = await _read(session)
        else:
            async with session_scope() as sess:
                mode = await _read(sess)
        if mode is not None:
            return mode
    except Exception:  # noqa: BLE001 - fail-safe: never crash a trading path
        logger.warning("risk_mode_read_failed_defaulting_normal")
    return RiskMode.NORMAL


def _min_opt(current: Decimal | None, ceiling: Decimal) -> Decimal:
    """Tighten an *optional* upper-bounded field: ``min(current, ceiling)``.

    ``None`` (unset) is treated as "no caller cap" → clamp *to* the
    ceiling (Conservative must impose one).  Never loosens.
    """
    return ceiling if current is None else min(current, ceiling)


def _min_int_opt(current: int | None, ceiling: int) -> int:
    """Integer analogue of :func:`_min_opt` for count caps."""
    return ceiling if current is None else min(current, ceiling)


def apply_conservative[T: ConservativeTemplate](template: T) -> T:
    """Return a NEW template tightened to the Conservative ceilings.

    PURE function — *never* mutates *template*; uses
    :func:`dataclasses.replace` to build a copy with only-tighter values.
    Every clamp takes ``min``/``max`` against the existing value so an
    already-stricter caller setting is preserved (tighten-only, never
    loosen — R4 / PRD §5.6):

    * ``copy_ratio``           ≤ 0.0005   (PRD §5.6: 0.05% ratio)
    * ``max_position_usd``     ≤ 10       (R4: maxPositionValue ≤ $10)
    * ``daily_loss_limit_usd`` ≤ 10       (R4: maxDailyLoss ≤ $10)
    * ``daily_order_cap``      ≤ 20       (R4: maxDailyOrders ≤ 20)
    * ``min_price``            ≥ 0.10     (R4: minPrice 0.10) — raise floor
    * ``max_price``            ≤ 0.80     (R4: maxPrice 0.80) — lower cap
    * ``min_depth``            ≥ 100      (R4: minOrderDepth ≥ 100)
    * ``max_spread``           ≤ 0.03     (R4: maxSpread ≤ 0.03)
    * ``price_tolerance_pct``  ≤ 3        (R4: priceTolerance ≤ 3%)

    Args:
        template: Any object satisfying :class:`ConservativeTemplate`
            (the Phase-4 ``TemplateConfig`` dataclass does, structurally).

    Returns:
        A new instance of the same type with the clamps applied.
    """
    from dataclasses import replace

    # Price band: raise the floor, lower the ceiling (tighten inward).
    new_min_price = max(template.min_price, _C_MIN_PRICE)
    new_max_price = min(template.max_price, _C_MAX_PRICE)
    # R2: the per-field clamps are independent, so a template with a high
    # min_price (e.g. 0.85) against the Conservative max ceiling (0.80) can
    # yield ``min_price > max_price`` — an internally-contradictory band
    # that rejects EVERY price.  Resolve by clamping ``min_price`` DOWN to
    # ``max_price`` (still strictly tighten-only: the resulting min is
    # never above either input min, and the band stays inside both the
    # caller's and the Conservative bounds).  Log so the misconfigured
    # template is visible.
    if new_min_price > new_max_price:
        logger.warning(
            "conservative_band_contradiction_clamped",
            extra={
                "template_min_price": str(template.min_price),
                "template_max_price": str(template.max_price),
                "clamped_min_price": str(new_max_price),
                "clamped_max_price": str(new_max_price),
            },
        )
        new_min_price = new_max_price

    return replace(  # type: ignore[type-var]
        template,
        # copy_ratio None ⇒ fixed-size mode: leave None (don't invent a
        # ratio); only clamp a too-loose explicit ratio downward.
        copy_ratio=(
            template.copy_ratio
            if template.copy_ratio is None
            else min(template.copy_ratio, _C_COPY_RATIO_MAX)
        ),
        max_position_usd=_min_opt(template.max_position_usd, _C_MAX_POSITION_USD),
        daily_loss_limit_usd=_min_opt(
            template.daily_loss_limit_usd, _C_DAILY_LOSS_USD
        ),
        daily_order_cap=_min_int_opt(template.daily_order_cap, _C_DAILY_ORDER_CAP),
        min_price=new_min_price,
        max_price=new_max_price,
        # Depth is a *minimum* requirement → raise it (max).
        min_depth=max(template.min_depth, _C_MIN_DEPTH),
        max_spread=min(template.max_spread, _C_MAX_SPREAD),
        price_tolerance_pct=min(
            template.price_tolerance_pct, _C_PRICE_TOL_PCT
        ),
    )

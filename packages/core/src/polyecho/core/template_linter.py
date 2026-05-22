"""
polyecho.core.template_linter
=============================
Pre-flight :class:`TemplateConfig` risk linter (PolyHermes deep-dive B3).

PolyEcho's 16-step :class:`~polyecho.core.risk.LayeredRiskGate` enforces
caps at *trade time*.  PolyHermes additionally ships ``inspectRiskConfig``,
a *pre-flight advisor* that flags a copy-trade template whose risk caps are
missing or dangerously loose **before** an operator enables a live follow.

This module is that advisor for PolyEcho.  It is a pure, side-effect-free
function: it reads an existing :class:`~polyecho.core.evaluator.TemplateConfig`
(no schema change, no DB, no network) and returns an ordered list of
:class:`TemplateWarning` records.  A well-configured template yields an
empty list.

Two warning families:

* **missing** — a protective cap is ``None``/unset, so that gate can never
  fire (the gate treats ``None`` as "no cap").
* **loose** — a cap is set but so far above a sane ceiling that it offers
  little real protection.

Each warning carries a :class:`Severity` (``HIGH``/``MEDIUM``), a stable
machine ``code``, the offending ``field`` name, a human ``message``, and a
``suggested`` value the operator can adopt.  The web layer renders these as
a calm advisory; the ``code`` is the i18n key seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from polyecho.core.evaluator import TemplateConfig

# ---------------------------------------------------------------------------
# Sane ceilings — a cap ABOVE these is "loose" (still set, but weak).
#
# These are deliberately generous: the linter is an advisor, not a second
# gate.  The intent is to catch a template that is technically configured
# but offers little real protection (e.g. a $1,000,000 per-position cap on
# a hobby account).  Values are conservative defaults an operator can tune.
# ---------------------------------------------------------------------------
LOOSE_MAX_POSITION_USD = Decimal("5000")
LOOSE_DAILY_LOSS_LIMIT_USD = Decimal("2000")
LOOSE_OPEN_LOSS_LIMIT_USD = Decimal("2000")
LOOSE_PORTFOLIO_MAX_USD = Decimal("20000")
LOOSE_MAX_CONCURRENT_POSITIONS = 25

# Suggested values when a cap is missing entirely — modest, safe-by-default
# starting points the operator can raise deliberately.
SUGGEST_MAX_POSITION_USD = Decimal("500")
SUGGEST_DAILY_LOSS_LIMIT_USD = Decimal("200")
SUGGEST_OPEN_LOSS_LIMIT_USD = Decimal("300")
SUGGEST_PORTFOLIO_MAX_USD = Decimal("2000")
SUGGEST_MAX_CONCURRENT_POSITIONS = 5


class Severity(StrEnum):
    """Warning severity.  ``HIGH`` = a money-loss cap can never fire."""

    HIGH = "high"
    MEDIUM = "medium"


@dataclass(frozen=True, slots=True)
class TemplateWarning:
    """One advisory finding about a :class:`TemplateConfig`.

    ``code`` is a stable identifier (the web i18n-key seam); ``field`` is the
    offending ``TemplateConfig`` attribute; ``suggested`` is a string-rendered
    value the operator can adopt (string keeps Decimal/int uniform + JSON-safe).
    """

    code: str
    field: str
    severity: Severity
    message: str
    suggested: str


# Caps whose absence means a *money-loss* gate can never fire ⇒ HIGH.
# A missing concurrency cap is undesirable but not directly loss-bounding
# ⇒ MEDIUM.  A loose (set-but-weak) cap is always MEDIUM: a gate still fires.


def _lint_missing(tpl: TemplateConfig) -> list[TemplateWarning]:
    """Flag protective caps left ``None``/unset (the gate treats None as off)."""
    out: list[TemplateWarning] = []

    if tpl.max_position_usd is None:
        out.append(
            TemplateWarning(
                code="missing.max_position_usd",
                field="max_position_usd",
                severity=Severity.HIGH,
                message=(
                    "No per-position value cap is set, so a single copied "
                    "trade can take an unbounded position."
                ),
                suggested=str(SUGGEST_MAX_POSITION_USD),
            )
        )

    if tpl.daily_loss_limit_usd is None:
        out.append(
            TemplateWarning(
                code="missing.daily_loss_limit_usd",
                field="daily_loss_limit_usd",
                severity=Severity.HIGH,
                message=(
                    "No daily loss limit is set, so a bad day cannot trip "
                    "the daily-loss circuit breaker."
                ),
                suggested=str(SUGGEST_DAILY_LOSS_LIMIT_USD),
            )
        )

    if tpl.open_loss_limit_usd is None:
        out.append(
            TemplateWarning(
                code="missing.open_loss_limit_usd",
                field="open_loss_limit_usd",
                severity=Severity.HIGH,
                message=(
                    "No open-loss limit is set, so the mark-to-market "
                    "circuit breaker on unrealised losses can never fire."
                ),
                suggested=str(SUGGEST_OPEN_LOSS_LIMIT_USD),
            )
        )

    if tpl.portfolio_max_usd is None:
        out.append(
            TemplateWarning(
                code="missing.portfolio_max_usd",
                field="portfolio_max_usd",
                severity=Severity.HIGH,
                message=(
                    "No portfolio exposure cap is set, so total exposure "
                    "across leaders and accounts is unbounded."
                ),
                suggested=str(SUGGEST_PORTFOLIO_MAX_USD),
            )
        )

    if tpl.max_concurrent_positions is None:
        out.append(
            TemplateWarning(
                code="missing.max_concurrent_positions",
                field="max_concurrent_positions",
                severity=Severity.MEDIUM,
                message=(
                    "No concurrency cap is set, so the number of open "
                    "copied positions is unbounded."
                ),
                suggested=str(SUGGEST_MAX_CONCURRENT_POSITIONS),
            )
        )

    return out


def _lint_loose(tpl: TemplateConfig) -> list[TemplateWarning]:
    """Flag caps that ARE set but sit above a sane ceiling (weak protection)."""
    out: list[TemplateWarning] = []

    if tpl.max_position_usd is not None and tpl.max_position_usd > LOOSE_MAX_POSITION_USD:
        out.append(
            TemplateWarning(
                code="loose.max_position_usd",
                field="max_position_usd",
                severity=Severity.MEDIUM,
                message=(
                    f"Per-position cap of ${tpl.max_position_usd} is above "
                    f"the suggested ceiling of ${LOOSE_MAX_POSITION_USD}."
                ),
                suggested=str(LOOSE_MAX_POSITION_USD),
            )
        )

    if (
        tpl.daily_loss_limit_usd is not None
        and tpl.daily_loss_limit_usd > LOOSE_DAILY_LOSS_LIMIT_USD
    ):
        out.append(
            TemplateWarning(
                code="loose.daily_loss_limit_usd",
                field="daily_loss_limit_usd",
                severity=Severity.MEDIUM,
                message=(
                    f"Daily loss limit of ${tpl.daily_loss_limit_usd} is "
                    f"above the suggested ceiling of "
                    f"${LOOSE_DAILY_LOSS_LIMIT_USD}."
                ),
                suggested=str(LOOSE_DAILY_LOSS_LIMIT_USD),
            )
        )

    if tpl.open_loss_limit_usd is not None and tpl.open_loss_limit_usd > LOOSE_OPEN_LOSS_LIMIT_USD:
        out.append(
            TemplateWarning(
                code="loose.open_loss_limit_usd",
                field="open_loss_limit_usd",
                severity=Severity.MEDIUM,
                message=(
                    f"Open-loss limit of ${tpl.open_loss_limit_usd} is "
                    f"above the suggested ceiling of "
                    f"${LOOSE_OPEN_LOSS_LIMIT_USD}."
                ),
                suggested=str(LOOSE_OPEN_LOSS_LIMIT_USD),
            )
        )

    if tpl.portfolio_max_usd is not None and tpl.portfolio_max_usd > LOOSE_PORTFOLIO_MAX_USD:
        out.append(
            TemplateWarning(
                code="loose.portfolio_max_usd",
                field="portfolio_max_usd",
                severity=Severity.MEDIUM,
                message=(
                    f"Portfolio cap of ${tpl.portfolio_max_usd} is above "
                    f"the suggested ceiling of ${LOOSE_PORTFOLIO_MAX_USD}."
                ),
                suggested=str(LOOSE_PORTFOLIO_MAX_USD),
            )
        )

    if (
        tpl.max_concurrent_positions is not None
        and tpl.max_concurrent_positions > LOOSE_MAX_CONCURRENT_POSITIONS
    ):
        out.append(
            TemplateWarning(
                code="loose.max_concurrent_positions",
                field="max_concurrent_positions",
                severity=Severity.MEDIUM,
                message=(
                    f"Concurrency cap of {tpl.max_concurrent_positions} "
                    f"open positions is above the suggested ceiling of "
                    f"{LOOSE_MAX_CONCURRENT_POSITIONS}."
                ),
                suggested=str(LOOSE_MAX_CONCURRENT_POSITIONS),
            )
        )

    return out


def lint_template_config(tpl: TemplateConfig) -> list[TemplateWarning]:
    """Inspect one :class:`TemplateConfig` and return advisory warnings.

    Pure: no side effects, no I/O.  A well-configured template yields ``[]``.
    Warnings are ordered ``HIGH`` before ``MEDIUM`` so the highest-severity
    finding renders first; within a severity, missing caps precede loose ones.
    """
    missing = _lint_missing(tpl)
    loose = _lint_loose(tpl)
    combined = missing + loose
    # Stable sort: HIGH (0) before MEDIUM (1); preserve insertion order within.
    rank = {Severity.HIGH: 0, Severity.MEDIUM: 1}
    return sorted(combined, key=lambda w: rank[w.severity])


def has_high_severity(warnings: list[TemplateWarning]) -> bool:
    """True if any warning is :attr:`Severity.HIGH` (a money-loss cap is off)."""
    return any(w.severity is Severity.HIGH for w in warnings)

"""Unit tests for :mod:`polyecho.core.template_linter` (PolyHermes B3).

Covers: a fully-configured template yields no warnings; each missing cap is
flagged HIGH/MEDIUM with a suggested value; each loose cap is flagged MEDIUM;
HIGH warnings sort ahead of MEDIUM; ``has_high_severity`` reports correctly.
"""

from __future__ import annotations

from decimal import Decimal

from polyecho.core.evaluator import TemplateConfig
from polyecho.core.template_linter import (
    LOOSE_DAILY_LOSS_LIMIT_USD,
    LOOSE_MAX_CONCURRENT_POSITIONS,
    LOOSE_MAX_POSITION_USD,
    LOOSE_OPEN_LOSS_LIMIT_USD,
    LOOSE_PORTFOLIO_MAX_USD,
    Severity,
    TemplateWarning,
    has_high_severity,
    lint_template_config,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _clean_template() -> TemplateConfig:
    """A template with every protective cap set to a sane (tight) value."""
    return TemplateConfig(
        copy_ratio=Decimal("0.1"),
        max_position_usd=Decimal("500"),
        min_trade_size_usd=Decimal("5"),
        max_concurrent_positions=5,
        daily_loss_limit_usd=Decimal("200"),
        portfolio_max_usd=Decimal("2000"),
        open_loss_limit_usd=Decimal("300"),
    )


def _codes(warnings: list[TemplateWarning]) -> set[str]:
    return {w.code for w in warnings}


# ---------------------------------------------------------------------------
# Clean config — no warnings
# ---------------------------------------------------------------------------


def test_clean_config_yields_no_warnings() -> None:
    assert lint_template_config(_clean_template()) == []


def test_clean_config_has_no_high_severity() -> None:
    assert has_high_severity(lint_template_config(_clean_template())) is False


def test_caps_exactly_at_loose_ceiling_are_not_flagged() -> None:
    """Boundary: a cap == the ceiling is acceptable (only `>` is loose)."""
    tpl = TemplateConfig(
        copy_ratio=Decimal("0.1"),
        max_position_usd=LOOSE_MAX_POSITION_USD,
        daily_loss_limit_usd=LOOSE_DAILY_LOSS_LIMIT_USD,
        open_loss_limit_usd=LOOSE_OPEN_LOSS_LIMIT_USD,
        portfolio_max_usd=LOOSE_PORTFOLIO_MAX_USD,
        max_concurrent_positions=LOOSE_MAX_CONCURRENT_POSITIONS,
    )
    assert lint_template_config(tpl) == []


# ---------------------------------------------------------------------------
# Missing caps — each is flagged
# ---------------------------------------------------------------------------


def test_all_caps_missing_flags_every_field() -> None:
    """The default TemplateConfig leaves every B3 cap as None."""
    warnings = lint_template_config(TemplateConfig(copy_ratio=Decimal("0.1")))
    assert _codes(warnings) == {
        "missing.max_position_usd",
        "missing.daily_loss_limit_usd",
        "missing.open_loss_limit_usd",
        "missing.portfolio_max_usd",
        "missing.max_concurrent_positions",
    }


def test_missing_max_position_is_high_with_suggestion() -> None:
    tpl = _clean_template_with(max_position_usd=None)
    warnings = lint_template_config(tpl)
    w = _only(warnings, "missing.max_position_usd")
    assert w.severity is Severity.HIGH
    assert w.field == "max_position_usd"
    assert Decimal(w.suggested) > 0


def test_missing_daily_loss_limit_is_high() -> None:
    w = _only(
        lint_template_config(_clean_template_with(daily_loss_limit_usd=None)),
        "missing.daily_loss_limit_usd",
    )
    assert w.severity is Severity.HIGH


def test_missing_open_loss_limit_is_high() -> None:
    w = _only(
        lint_template_config(_clean_template_with(open_loss_limit_usd=None)),
        "missing.open_loss_limit_usd",
    )
    assert w.severity is Severity.HIGH


def test_missing_portfolio_max_is_high() -> None:
    w = _only(
        lint_template_config(_clean_template_with(portfolio_max_usd=None)),
        "missing.portfolio_max_usd",
    )
    assert w.severity is Severity.HIGH


def test_missing_concurrency_cap_is_medium() -> None:
    """A missing concurrency cap is undesirable but not loss-bounding."""
    w = _only(
        lint_template_config(_clean_template_with(max_concurrent_positions=None)),
        "missing.max_concurrent_positions",
    )
    assert w.severity is Severity.MEDIUM


# ---------------------------------------------------------------------------
# Loose caps — each is flagged MEDIUM
# ---------------------------------------------------------------------------


def test_loose_max_position_is_flagged_medium() -> None:
    tpl = _clean_template_with(max_position_usd=LOOSE_MAX_POSITION_USD + Decimal("1"))
    w = _only(lint_template_config(tpl), "loose.max_position_usd")
    assert w.severity is Severity.MEDIUM
    assert Decimal(w.suggested) == LOOSE_MAX_POSITION_USD


def test_loose_daily_loss_limit_is_flagged() -> None:
    tpl = _clean_template_with(daily_loss_limit_usd=LOOSE_DAILY_LOSS_LIMIT_USD + Decimal("1"))
    assert "loose.daily_loss_limit_usd" in _codes(lint_template_config(tpl))


def test_loose_open_loss_limit_is_flagged() -> None:
    tpl = _clean_template_with(open_loss_limit_usd=LOOSE_OPEN_LOSS_LIMIT_USD + Decimal("1"))
    assert "loose.open_loss_limit_usd" in _codes(lint_template_config(tpl))


def test_loose_portfolio_max_is_flagged() -> None:
    tpl = _clean_template_with(portfolio_max_usd=LOOSE_PORTFOLIO_MAX_USD + Decimal("1"))
    assert "loose.portfolio_max_usd" in _codes(lint_template_config(tpl))


def test_loose_concurrency_cap_is_flagged() -> None:
    tpl = _clean_template_with(max_concurrent_positions=LOOSE_MAX_CONCURRENT_POSITIONS + 1)
    assert "loose.max_concurrent_positions" in _codes(lint_template_config(tpl))


# ---------------------------------------------------------------------------
# Ordering + aggregate helpers
# ---------------------------------------------------------------------------


def test_high_severity_warnings_sort_before_medium() -> None:
    """A template with both a missing money cap and a loose cap orders HIGH first."""
    tpl = _clean_template_with(
        max_position_usd=None,  # HIGH (missing)
        max_concurrent_positions=LOOSE_MAX_CONCURRENT_POSITIONS + 10,  # MEDIUM
    )
    warnings = lint_template_config(tpl)
    severities = [w.severity for w in warnings]
    assert severities == [Severity.HIGH, Severity.MEDIUM]


def test_has_high_severity_true_when_money_cap_missing() -> None:
    warnings = lint_template_config(_clean_template_with(daily_loss_limit_usd=None))
    assert has_high_severity(warnings) is True


def test_has_high_severity_false_for_only_loose_caps() -> None:
    tpl = _clean_template_with(max_position_usd=LOOSE_MAX_POSITION_USD + Decimal("1"))
    assert has_high_severity(lint_template_config(tpl)) is False


def test_warnings_are_immutable() -> None:
    """TemplateWarning is a frozen dataclass — defensive contract check."""
    w = lint_template_config(TemplateConfig(copy_ratio=Decimal("0.1")))[0]
    try:
        w.severity = Severity.MEDIUM  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - guards against accidental mutability
        raise AssertionError("TemplateWarning should be frozen")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_template_with(**overrides: object) -> TemplateConfig:
    """A clean template with the given fields overridden."""
    base: dict[str, object] = {
        "copy_ratio": Decimal("0.1"),
        "max_position_usd": Decimal("500"),
        "min_trade_size_usd": Decimal("5"),
        "max_concurrent_positions": 5,
        "daily_loss_limit_usd": Decimal("200"),
        "portfolio_max_usd": Decimal("2000"),
        "open_loss_limit_usd": Decimal("300"),
    }
    base.update(overrides)
    return TemplateConfig(**base)  # type: ignore[arg-type]


def _only(warnings: list[TemplateWarning], code: str) -> TemplateWarning:
    """Assert exactly one warning has `code` and return it."""
    matches = [w for w in warnings if w.code == code]
    assert len(matches) == 1, f"expected exactly one {code}, got {warnings}"
    return matches[0]

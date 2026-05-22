"""Paper-session summary recomputation (C1-P3).

After each ~15-min cycle the paper worker re-simulates the leader's new
trades and must recompute the denormalised ``paper_sessions`` summary
columns the state machine gates on:

* ``trade_count`` -- executed (non-skipped) simulated fills.
* ``copyable_pnl`` -- realised + mark-to-market PnL of the paper run.
* ``max_drawdown`` -- worst peak-to-trough decline of the balance curve,
  expressed as a NEGATIVE fraction (e.g. ``-0.12`` == 12% drawdown) so it
  is directly comparable with the state-machine thresholds.
* ``unknown_exposure_ratio`` -- fraction of simulated trades whose market
  could not be classified (an ``unknown`` / unmapped condition_id).
* ``filtered_ratio`` -- fraction of *observed* leader trades skipped by a
  filter / risk gate rather than executed.

This module is pure (no DB): it folds over the persisted per-fill rows
the worker already holds, so each metric is independently testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PaperFill:
    """A minimal view of one persisted ``paper_trades`` row for summarising.

    A ``PaperFill`` always represents an EXECUTED simulated fill -- a
    ``paper_trades`` row is only ever written for a real BUY / SELL / MTM.
    Filtered (skipped) leader trades are NOT persisted as fills; their
    count flows into ``filtered_ratio`` via ``observed_trade_count`` minus
    the executed ``trade_count`` (see :func:`recompute_summary`).

    Attributes:
        side: ``BUY`` / ``SELL`` / ``MTM``.
        pnl: Realised (or MTM) PnL of this fill in USD.
        balance_after: Paper balance after the fill (the curve point).
        condition_id: Market condition id, or ``None`` when unknown.
        market_known: True when the fill's market was classifiable; a
            ``False`` here counts toward ``unknown_exposure_ratio``.
    """

    side: str
    pnl: float
    balance_after: float | None = None
    condition_id: str | None = None
    market_known: bool = True


@dataclass(frozen=True)
class SessionSummary:
    """The recomputed ``paper_sessions`` summary block.

    All fields map 1:1 onto the ``paper_sessions`` denormalised columns
    and onto the :class:`~polyecho.paper.state_machine.LadderCandidate`
    fields the gates read.
    """

    trade_count: int
    copyable_pnl: float
    max_drawdown: float
    unknown_exposure_ratio: float
    filtered_ratio: float


def _max_drawdown_fraction(curve: Sequence[float]) -> float:
    """Worst peak-to-trough decline of a balance curve, as a NEG fraction.

    Returns ``0.0`` when the curve never declines (or is empty / has a
    non-positive running peak -- a drawdown fraction is only defined
    against a strictly-positive high-water mark, mirroring the backtest
    engine's :func:`compute_max_drawdown` discipline).  The result is
    negative (e.g. ``-0.15``) so it compares directly with the
    state-machine drawdown thresholds.
    """
    if not curve:
        return 0.0
    peak = curve[0]
    worst = 0.0
    for value in curve:
        if value > peak:
            peak = value
        if peak > 0.0:
            decline = (value - peak) / peak  # <= 0.0
            if decline < worst:
                worst = decline
    return worst


def recompute_summary(
    fills: Sequence[PaperFill],
    *,
    capital_usd: float,
    cycle_observed: int,
    cycle_executed: int,
) -> SessionSummary:
    """Fold the executed paper fills into a fresh :class:`SessionSummary`.

    ``trade_count`` / ``copyable_pnl`` / ``max_drawdown`` /
    ``unknown_exposure_ratio`` are computed over the WHOLE session's
    persisted ``fills`` (a full recompute each cycle, so a worker restart
    is self-correcting).  ``filtered_ratio`` is a rolling per-cycle
    estimate -- the ``paper_sessions`` schema carries no cumulative
    observed-trade counter, so it is recomputed from THIS cycle's observed
    vs executed window each cycle.

    Args:
        fills: Every persisted ``paper_trades`` fill for the session
            (prior cycles + this cycle), in chronological order.  Each is
            an EXECUTED fill -- a skipped leader trade is never a fill.
        capital_usd: The session's starting paper capital -- the curve
            origin used when a fill carries no ``balance_after``.
        cycle_observed: Leader trades observed THIS cycle (executed +
            filtered).
        cycle_executed: Leader trades EXECUTED THIS cycle.  ``filtered_ratio``
            is ``(cycle_observed - cycle_executed) / cycle_observed``; a 0
            observed count yields a 0 ratio.

    Returns:
        A :class:`SessionSummary` ready to write onto ``paper_sessions``.
    """
    trade_count = len(fills)

    copyable_pnl = float(sum(f.pnl for f in fills))

    # Balance curve: prefer the persisted balance_after; otherwise rebuild
    # it cumulatively from capital + running PnL so the drawdown math is
    # still well-defined for fills written without an explicit curve point.
    curve: list[float] = [capital_usd]
    running = capital_usd
    for fill in fills:
        if fill.balance_after is not None:
            running = fill.balance_after
        else:
            running += fill.pnl
        curve.append(running)
    max_drawdown = _max_drawdown_fraction(curve)

    unknown = sum(1 for f in fills if not f.market_known)
    unknown_exposure_ratio = (unknown / trade_count) if trade_count > 0 else 0.0

    # Filtered trades are observed-but-not-executed; not persisted as fills.
    filtered = max(0, cycle_observed - cycle_executed)
    filtered_ratio = (filtered / cycle_observed) if cycle_observed > 0 else 0.0

    return SessionSummary(
        trade_count=trade_count,
        copyable_pnl=copyable_pnl,
        max_drawdown=max_drawdown,
        unknown_exposure_ratio=unknown_exposure_ratio,
        filtered_ratio=filtered_ratio,
    )

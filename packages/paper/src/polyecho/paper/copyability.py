"""Paper-trade copyability scoring (C1, PolyHermes ``research-copyability-v1``).

This module ports PolyHermes' ``research-copyability-v1`` algorithm into
PolyEcho's C1 paper-trading subsystem.  It is a wholly different signal
from the discovery ``total_score``:

* Discovery ``total_score`` (``packages/discovery/scoring.py``) is a
  MULTIPLICATIVE *leaderboard rank* -- it answers "how good does this
  leader look on the public leaderboard".  Its real-pool maximum is ~51,
  so it can NEVER reach the old 60 paper-ladder gate; the ladder no longer
  uses it.
* This module's copyability score is an ADDITIVE *paper-trade copyability*
  score -- it answers "how copyable did this leader prove to be once we
  actually shadow-copied them in paper".  Eleven components each contribute
  an additive sub-score; they sum (capped at 100) into the final score.

Because a paper-trade copyability score only becomes meaningful AFTER
paper data accrues, it CANNOT gate entry to paper-trading -- a brand-new
leader has no session and so scores at most 20.  PolyHermes resolves this
with a bootstrap clause in the ladder (see
:func:`~polyecho.paper.state_machine.advance`); this module just computes
the score.

The function is pure (no DB I/O): it folds a single optional paper-session
snapshot into a :class:`CopyabilityResult` exposing the total, the
per-component breakdown, and :data:`SCORE_VERSION` so the score is fully
auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Version + sample-size constants (PolyHermes research-copyability-v1)
# ---------------------------------------------------------------------------

#: The algorithm version stamped onto every :class:`CopyabilityResult` and
#: persisted alongside the score so a stored score is always auditable.
SCORE_VERSION = "research-copyability-v1"

#: Minimum simulated paper trades for the score to be trusted at full
#: range.  Below this the sample is too small to claim a high score.
MIN_SAMPLE_TRADES = 10

#: When ``trade_count < MIN_SAMPLE_TRADES`` an otherwise-high raw total is
#: capped at this value: a thin sample cannot reach the 60 promote gate.
SAMPLE_INSUFFICIENT_CAP = 59

#: Overall additive score ceiling (sum of every component's max).
TOTAL_SCORE_CAP = 100.0

#: PolyHermes "data freshness" window: a discovery source seen within this
#: window scores the full freshness sub-score.
DATA_FRESHNESS_MAX_AGE = timedelta(hours=48)

#: PolyHermes "holding period fit" target: a paper session at least this
#: old scores the full holding-period sub-score.
HOLDING_PERIOD_TARGET = timedelta(days=7)


# ---------------------------------------------------------------------------
# Pure inputs + result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperSessionStats:
    """The paper-session metrics the copyability score reads (no DB handle).

    The C1 paper worker builds one of these from a leader's open
    ``paper_session`` row.  Keeping it a plain frozen dataclass makes every
    component independently unit-testable with no database.

    Attributes:
        copyable_pnl: Paper copyable PnL (USD) from the session summary.
        trade_count: Executed simulated paper trade count.
        unknown_exposure_ratio: Fraction of trades whose market could not
            be classified, in ``[0, 1]``.  When the session column is
            ``None`` this defaults to ``1.0`` (maximally unknown) -- an
            unmeasured exposure must never be assumed clean.
        filtered_ratio: Fraction of observed leader trades skipped by a
            filter / risk gate, in ``[0, 1]``.  ``None`` -> ``1.0``.
        max_drawdown: Worst peak-to-trough decline as a NEGATIVE FRACTION
            (e.g. ``-0.15`` == a 15% drawdown), or ``None`` when the
            session has not measured a drawdown yet.
        started_at: When the paper session opened (UTC-aware), used for the
            holding-period-fit component.
    """

    copyable_pnl: float = 0.0
    trade_count: int = 0
    unknown_exposure_ratio: float = 1.0
    filtered_ratio: float = 1.0
    max_drawdown: float | None = None
    started_at: datetime | None = None


@dataclass(frozen=True)
class CopyabilityResult:
    """The outcome of one :func:`score_copyability` call (a pure result).

    Attributes:
        total: The final copyability score in ``[0, 100]`` (after the
            sample-size cap).
        raw_total: The uncapped sum of every component sub-score.
        components: The per-component sub-score breakdown, keyed by the
            PolyHermes component name -- surfaced for audit / UI.
        sample_capped: True when the ``< MIN_SAMPLE_TRADES`` cap lowered
            ``total`` below ``raw_total``.
        score_version: :data:`SCORE_VERSION`, stamped for auditability.
    """

    total: float
    raw_total: float
    components: dict[str, float] = field(default_factory=dict)
    sample_capped: bool = False
    score_version: str = SCORE_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive ``[lo, hi]`` range."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# The eleven additive components (PolyHermes research-copyability-v1)
# ---------------------------------------------------------------------------
#
# Each component is one named clause of the algorithm.  Every component is
# defined for the no-session case too (it takes its conservative no-data
# value) so a leader with no paper session still gets a well-defined score.


def _profit_signal(stats: PaperSessionStats | None) -> float:
    """Component 1 (max 20): reward demonstrated paper copyable PnL.

    ``copyable_pnl > 10`` -> 20; ``> 0`` -> ``clamp(pnl * 2, 0, 20)``;
    otherwise (a loss / break-even, or no session) 0.
    """
    if stats is None:
        return 0.0
    pnl = stats.copyable_pnl
    if pnl > 10.0:
        return 20.0
    if pnl > 0.0:
        return _clamp(pnl * 2.0, 0.0, 20.0)
    return 0.0


def _repeatability(stats: PaperSessionStats | None) -> float:
    """Component 2 (max 15): reward a repeatable, non-thin trade history.

    ``clamp(trade_count * 1.5, 0, 15)`` -- 0 with no session.
    """
    if stats is None:
        return 0.0
    return _clamp(stats.trade_count * 1.5, 0.0, 15.0)


def _liquidity_fit(stats: PaperSessionStats | None) -> float:
    """Component 3 (max 10): penalise unknown-market (illiquid) exposure.

    ``clamp(10 - unknown_ratio * 10, 0, 10)`` -- 0 with no session (the
    no-session unknown ratio is the maximal ``1.0``).
    """
    if stats is None:
        return 0.0
    return _clamp(10.0 - stats.unknown_exposure_ratio * 10.0, 0.0, 10.0)


def _entry_price_fit(stats: PaperSessionStats | None) -> float:
    """Component 4 (max 10): reward a low filter / risk-gate skip rate.

    ``clamp(10 - filtered_ratio * 10, 0, 10)`` -- 0 with no session.
    """
    if stats is None:
        return 0.0
    return _clamp(10.0 - stats.filtered_ratio * 10.0, 0.0, 10.0)


def _slippage_risk(stats: PaperSessionStats | None) -> float:
    """Component 5 (max 10): a step function on unknown-market exposure.

    ``unknown_ratio <= 0.20`` -> 10, else 4.  With no session the unknown
    ratio is the maximal ``1.0`` -> the low value 4.
    """
    if stats is None:
        return 4.0
    return 10.0 if stats.unknown_exposure_ratio <= 0.20 else 4.0


def _holding_period_fit(stats: PaperSessionStats | None, now: datetime) -> float:
    """Component 6 (max 5): reward a paper session that has run a full week.

    ``paper_age >= 7d`` -> 5, else a linear ``(paper_age / 7d) * 5``.  0
    with no session (or no ``started_at``).
    """
    if stats is None or stats.started_at is None:
        return 0.0
    age = now - stats.started_at
    if age >= HOLDING_PERIOD_TARGET:
        return 5.0
    fraction = age / HOLDING_PERIOD_TARGET
    return _clamp(fraction * 5.0, 0.0, 5.0)


def _market_type_risk() -> float:
    """Component 7 (max 5): a constant 5.

    PolyHermes treats market-type risk as a fixed allowance in this
    version; it does not vary with the paper session.
    """
    return 5.0


def _drawdown_risk(stats: PaperSessionStats | None) -> float:
    """Component 8 (max 10): bucket the paper max drawdown.

    ``max_drawdown`` here is a NEGATIVE FRACTION (PolyEcho units), so the
    PolyHermes percentage buckets convert to fractions:

    * ``None`` (unmeasured) -> 5 (a neutral middle value)
    * ``>= -0.05`` -> 10
    * ``>= -0.15`` -> 7
    * ``>= -0.20`` -> 3
    * otherwise (worse than a 20% drawdown) -> 0

    With no session the drawdown is unmeasured -> the neutral 5.
    """
    drawdown = stats.max_drawdown if stats is not None else None
    if drawdown is None:
        return 5.0
    if drawdown >= -0.05:
        return 10.0
    if drawdown >= -0.15:
        return 7.0
    if drawdown >= -0.20:
        return 3.0
    return 0.0


def _exit_liquidity_risk(stats: PaperSessionStats | None) -> float:
    """Component 9 (max 5): a step function on unknown-market exposure.

    ``unknown_ratio <= 0.20`` -> 5, else 1.  With no session the unknown
    ratio is the maximal ``1.0`` -> the low value 1.
    """
    if stats is None:
        return 1.0
    return 5.0 if stats.unknown_exposure_ratio <= 0.20 else 1.0


def _data_freshness(source_last_seen: datetime | None, now: datetime) -> float:
    """Component 10 (max 5): reward a discovery source seen within 48h.

    A source seen ``<= 48h`` ago -> 5, else 0 (a never-seen source -> 0).
    This component is the only one independent of the paper session.
    """
    if source_last_seen is None:
        return 0.0
    return 5.0 if (now - source_last_seen) <= DATA_FRESHNESS_MAX_AGE else 0.0


def _filter_pass_rate(stats: PaperSessionStats | None) -> float:
    """Component 11 (max 5): reward a low filter / risk-gate skip rate.

    ``clamp(5 - filtered_ratio * 5, 0, 5)`` -- 0 with no session (the
    no-session filtered ratio is the maximal ``1.0``).
    """
    if stats is None:
        return 0.0
    return _clamp(5.0 - stats.filtered_ratio * 5.0, 0.0, 5.0)


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


def score_copyability(
    stats: PaperSessionStats | None,
    *,
    source_last_seen: datetime | None = None,
    now: datetime | None = None,
) -> CopyabilityResult:
    """Compute the PolyHermes ``research-copyability-v1`` score (pure).

    Sums the eleven additive components, applies the overall 100 cap, then
    the sample-size cap: if ``trade_count < MIN_SAMPLE_TRADES`` and the raw
    total exceeds :data:`SAMPLE_INSUFFICIENT_CAP`, the total is held at
    :data:`SAMPLE_INSUFFICIENT_CAP` (a thin sample cannot reach the 60
    promote gate).

    A ``stats`` of ``None`` (no paper session yet) is fully supported:
    every paper-derived component takes its conservative no-data value, so
    a leader with no session scores at most 20 (market-type 5 + drawdown 5
    + slippage 4 + exit-liquidity 1 + data-freshness 5 when the source is
    fresh).  This is by design -- a copyability score only becomes real
    once paper data accrues, which is why the ladder bootstraps entry.

    Args:
        stats: The leader's open-paper-session metrics, or ``None`` when no
            session exists yet.
        source_last_seen: When the discovery source last saw the leader,
            for the data-freshness component; ``None`` -> never seen.
        now: Reference "current time" (UTC-aware); defaults to
            :func:`datetime.now` in UTC.  Injected in tests.

    Returns:
        A :class:`CopyabilityResult` carrying the capped total, the raw
        total, the per-component breakdown, and :data:`SCORE_VERSION`.
    """
    if now is None:
        from datetime import UTC

        now = datetime.now(UTC)

    components: dict[str, float] = {
        "profit_signal": _profit_signal(stats),
        "repeatability": _repeatability(stats),
        "liquidity_fit": _liquidity_fit(stats),
        "entry_price_fit": _entry_price_fit(stats),
        "slippage_risk": _slippage_risk(stats),
        "holding_period_fit": _holding_period_fit(stats, now),
        "market_type_risk": _market_type_risk(),
        "drawdown_risk": _drawdown_risk(stats),
        "exit_liquidity_risk": _exit_liquidity_risk(stats),
        "data_freshness": _data_freshness(source_last_seen, now),
        "filter_pass_rate": _filter_pass_rate(stats),
    }

    raw_total = _clamp(sum(components.values()), 0.0, TOTAL_SCORE_CAP)

    # Sample-size cap: a thin paper sample (< 10 trades) cannot claim a
    # score that would clear the 60 promote gate -- hold it at 59.
    trade_count = stats.trade_count if stats is not None else 0
    total = raw_total
    sample_capped = False
    if trade_count < MIN_SAMPLE_TRADES and raw_total > SAMPLE_INSUFFICIENT_CAP:
        total = float(SAMPLE_INSUFFICIENT_CAP)
        sample_capped = True

    return CopyabilityResult(
        total=total,
        raw_total=raw_total,
        components=components,
        sample_capped=sample_capped,
        score_version=SCORE_VERSION,
    )

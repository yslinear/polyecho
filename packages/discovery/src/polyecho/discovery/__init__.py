"""polyecho.discovery — Discovery-worker service.

Discovers, scores, and tracks leader candidates on Polymarket (PRD §5.2, §5.3).
This process holds no private keys and opens no network ports.

Public API surface
------------------
* :class:`DiscoveryWorker`       — orchestrates all four discovery sources.
* :class:`FrequencyStats`        — PRD §5.3 frequency metric container.
* :class:`StrategyType`          — enum for strategy classification.
* :func:`compute_frequency_stats` — produce FrequencyStats from ActivityRecords.
* :func:`classify_strategy`      — map FrequencyStats → StrategyType.
* :func:`detect_drift`           — compare 30d vs 7d strategy type.
* :func:`profit_score`           — 0–100 profit dimension score.
* :func:`replicability_score`    — 0–100 replicability dimension score.
* :func:`final_score`            — multiplicative combination.
* :func:`detect_risk_flags`      — list of risk flag strings.
"""

from polyecho.discovery.frequency import (
    FrequencyStats,
    StrategyType,
    classify_strategy,
    compute_frequency_stats,
    detect_drift,
)
from polyecho.discovery.riskflags import detect_risk_flags
from polyecho.discovery.scoring import final_score, profit_score, replicability_score
from polyecho.discovery.worker import DiscoveryWorker

__all__ = [
    "DiscoveryWorker",
    "FrequencyStats",
    "StrategyType",
    "classify_strategy",
    "compute_frequency_stats",
    "detect_drift",
    "detect_risk_flags",
    "final_score",
    "profit_score",
    "replicability_score",
]

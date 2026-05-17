"""polyecho.core — Trader-core copy-trade engine (PRD §4.2, §5.5).

Holds private keys and executes copy-trades on Polymarket.  This process
opens NO network ports; all control flows through PostgreSQL.  The engine
NEVER places a real order/redeem unless ``is_live_enabled()`` is True
(default OFF — the Phase-4 hard money lock; brief §9).

``Decision`` / ``DecisionOutcome`` are re-exported verbatim from the
frozen Wave-1 contract (:mod:`polyecho.shared.copytrading`).
"""

from polyecho.core.trader import Decision, DecisionOutcome, FollowSpec, TraderCore

__all__ = ["TraderCore", "Decision", "DecisionOutcome", "FollowSpec"]

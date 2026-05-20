"""polyecho.core — Trader-core copy-trade engine (PRD §4.2, §5.5).

Holds private keys and executes copy-trades on Polymarket.  This process
opens NO network ports; all control flows through PostgreSQL.  Per-op
operator confirmation lives at the web layer (``dangerous-action`` +
account-import passphrase).

``Decision`` / ``DecisionOutcome`` are re-exported verbatim from the
frozen Wave-1 contract (:mod:`polyecho.shared.copytrading`).
"""

from polyecho.core.trader import Decision, DecisionOutcome, FollowSpec, TraderCore

__all__ = ["TraderCore", "Decision", "DecisionOutcome", "FollowSpec"]

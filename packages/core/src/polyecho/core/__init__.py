"""polyecho.core — Trader-core service.

Holds private keys and executes copy-trades on Polymarket (PRD §4.2, §5.5).
This process opens NO network ports; all control flows through PostgreSQL.
"""

from polyecho.core.trader import Decision, TraderCore

__all__ = ["TraderCore", "Decision"]

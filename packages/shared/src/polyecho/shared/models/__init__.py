"""
polyecho.shared.models
======================
SQLAlchemy 2.0 ORM models for all tables defined in PRD §6.1.

All models inherit from :class:`~polyecho.shared.db.Base` and use the
typed ``Mapped[...]`` / ``mapped_column`` API introduced in SQLAlchemy 2.0.

Table → PRD purpose summary
----------------------------
* :class:`Account`                — your own Polymarket wallets (PRD §5.1)
* :class:`CandidateLeader`        — discovered leader pool (PRD §5.2)
* :class:`CandidateScoreHistory`  — daily score snapshots for trending (PRD §5.2)
* :class:`CopyTemplate`           — risk / copy-ratio presets (PRD §5.6)
* :class:`FollowRelation`         — active leader → account → template links (PRD §5.5)
* :class:`LeaderTrade`            — cached leader on-chain trades (PRD §5.5)
* :class:`MyTrade`                — your own copy-trades (PRD §5.5)
* :class:`Setting`                — key/value store for runtime config (PRD §5.7)
* :class:`BacktestJob`            — backtest task queue (PRD §5.4)
* :class:`PendingAction`          — dangerous operations awaiting confirmation (PRD §5.7)
* :class:`PushSubscription`       — Web Push endpoint registrations (PRD §5.8)
* :class:`Event`                  — system events for the push-sender (PRD §5.8)
"""

from polyecho.shared.models.account import Account
from polyecho.shared.models.backtest_job import BacktestJob
from polyecho.shared.models.candidate_leader import CandidateLeader
from polyecho.shared.models.candidate_score_history import CandidateScoreHistory
from polyecho.shared.models.copy_template import CopyTemplate
from polyecho.shared.models.event import Event
from polyecho.shared.models.follow_relation import FollowRelation
from polyecho.shared.models.leader_trade import LeaderTrade
from polyecho.shared.models.my_trade import MyTrade
from polyecho.shared.models.pending_action import PendingAction
from polyecho.shared.models.push_subscription import PushSubscription
from polyecho.shared.models.setting import Setting

__all__ = [
    "Account",
    "BacktestJob",
    "CandidateLeader",
    "CandidateScoreHistory",
    "CopyTemplate",
    "Event",
    "FollowRelation",
    "LeaderTrade",
    "MyTrade",
    "PendingAction",
    "PushSubscription",
    "Setting",
]

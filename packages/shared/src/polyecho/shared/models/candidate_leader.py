"""ORM model for the ``candidate_leaders`` table (PRD §5.2, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class CandidateLeader(Base):
    """A Polymarket trader discovered as a potential leader to copy-trade.

    Scores are recomputed daily by the ``discovery-worker`` service.

    PRD §5.2 scoring formula::

        final_score = profit_score × (replicability_score / 100)

    ``user_status`` lifecycle: new -> watching -> following -> rejected --
    the operator-review intent, set only by a human.

    ``lifecycle_state`` (C1) is a SEPARATE automated paper-trading-ladder
    state: discovered -> candidate -> paper -> trial_ready, with cooldown /
    retired branches.  It is driven by the C1-P3 state machine and never
    touches ``user_status``.  This C1-P1 phase only declares the columns.
    """

    __tablename__ = "candidate_leaders"
    __table_args__ = (
        # 3-way mirror of the SQL ck_candidate_leaders_lifecycle_state: the
        # automated ladder state is one of the six known values only.
        CheckConstraint(
            "lifecycle_state IN ('discovered', 'candidate', 'paper', "
            "'trial_ready', 'cooldown', 'retired')",
            name="ck_candidate_leaders_lifecycle_state",
        ),
    )

    wallet_address: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Discovery provenance.
    # SQL/Drizzle declare these arrays NULLable (no NOT NULL); the Python
    # ``default=list`` still gives new rows ``[]`` without forcing a DB-level
    # NOT NULL the migration does not have (3-way alignment, SQL = truth).
    sources: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True, default=list)
    first_discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Phase-3 discovery cursor: high-water mark of the latest Data-API
    # TRADE timestamp processed for this wallet (anti-duplication —
    # drives the hourly windowed re-scan + the daily staleness gate).
    last_seen_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Rolling stats snapshots (JSONB allows schema-free evolution)
    stats_7d: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stats_30d: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stats_90d: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    frequency_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Strategy classification (scalper / day_trader / swing / position / hold_to_resolve)
    strategy_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Default backtest params snapshot
    backtest_default: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Scoring (0–100 each) — NUMERIC(6,2) in all 3 sources (SQL = truth).
    profit_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    replicability_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    total_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)

    # Qualitative tagging — NULLable in SQL/Drizzle; ``default=list`` keeps
    # the Python-side ergonomics without a DB NOT NULL the migration omits.
    risk_flags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True, default=list)
    specialization: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True, default=list
    )

    # User review status
    user_status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    user_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- C1 paper-trading ladder (automated, SEPARATE from user_status) ---
    # Automated ladder state -- discovered | candidate | paper | trial_ready
    # | cooldown | retired (CHECK in __table_args__). Driven by the C1-P3
    # state machine; this phase only stores it.
    lifecycle_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="discovered",
        server_default=text("'discovered'"),
    )
    # When a cooldown ends -- a cooldown leader re-enters the ladder once
    # this elapses. NULLable: a non-cooldown leader has no cooldown end.
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How many times this leader has been put on cooldown -- 3 retires it.
    cooldown_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Operator override: a locked leader is skipped by the ladder entirely.
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

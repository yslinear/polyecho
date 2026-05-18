"""ORM model for the ``candidate_leaders`` table (PRD §5.2, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Numeric, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class CandidateLeader(Base):
    """A Polymarket trader discovered as a potential leader to copy-trade.

    Scores are recomputed daily by the ``discovery-worker`` service.

    PRD §5.2 scoring formula::

        final_score = profit_score × (replicability_score / 100)

    ``user_status`` lifecycle: new → watching → following → rejected.
    """

    __tablename__ = "candidate_leaders"

    wallet_address: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Discovery provenance.
    # SQL/Drizzle declare these arrays NULLable (no NOT NULL); the Python
    # ``default=list`` still gives new rows ``[]`` without forcing a DB-level
    # NOT NULL the migration does not have (3-way alignment, SQL = truth).
    sources: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True, default=list
    )
    first_discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    risk_flags: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True, default=list
    )
    specialization: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True, default=list
    )

    # User review status
    user_status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    user_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

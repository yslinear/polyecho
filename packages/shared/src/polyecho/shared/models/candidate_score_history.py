"""ORM model for the ``candidate_score_history`` table (PRD §6.1)."""

from __future__ import annotations

from datetime import date as date_type
from typing import Any

from sqlalchemy import Date, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class CandidateScoreHistory(Base):
    """Daily score snapshot for each candidate leader.

    Used to detect trending leaders and score regressions.
    Compound primary key ``(wallet_address, date)`` ensures one row per
    leader per day.
    """

    __tablename__ = "candidate_score_history"

    wallet_address: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[date_type] = mapped_column(Date, primary_key=True)
    total_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    rank: Mapped[int | None] = mapped_column(nullable=True)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

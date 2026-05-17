"""ORM model for the ``backtest_jobs`` table (PRD §5.4, §6.1)."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime
from typing import Any

from sqlalchemy import Date, DateTime, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class BacktestJob(Base):
    """A backtest simulation task enqueued via the Dashboard or CLI.

    ``status`` lifecycle: pending → running → completed | failed.
    ``progress`` is a 0–100 percentage updated by the backtest worker.
    ``result`` stores the full output JSON once completed (PnL curve, stats,
    skip analysis, three-scenario comparison).

    PRD §5.4: each job simulates one or more leaders over a historical period
    using the given template parameters and slippage / delay scenario.
    """

    __tablename__ = "backtest_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    leader_addresses: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False
    )
    template_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    start_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    end_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    initial_capital_usd: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    slippage_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=150)
    execution_delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # pending | running | completed | failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    progress: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

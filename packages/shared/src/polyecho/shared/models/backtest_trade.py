"""ORM model for the ``backtest_trades`` table (B1, PolyHermes deep-dive)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class BacktestTrade(Base):
    """One per-fill row of a completed backtest job (B1).

    PolyHermes' ``backtest_trade`` equivalent: each executed fill (BUY /
    SELL / MTM) of a backtest scenario is persisted as its own row, carrying
    the equity curve (``equity_after``) so the ``/backtest`` detail + list
    pages can render and sort without unpacking the ``backtest_jobs.result``
    JSONB.  A backtest job runs N leaders x up to 3 scenarios, so rows are
    keyed by ``(backtest_job_id, leader_address, scenario, order_index)``.

    ``order_index`` is the monotonic per-(job, leader, scenario) fill index,
    preserving the engine ``trade_log`` ordering for the equity curve
    independently of timestamp ties.  The ``result`` JSONB still carries the
    full ``trade_log`` — this table is an additive read-optimised mirror.
    """

    __tablename__ = "backtest_trades"
    __table_args__ = (
        # The detail page loads every fill for one job; the SQL migration
        # creates the same index (3-way aligned with 0000_init.sql).
        Index("idx_backtest_trades_job", "backtest_job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    backtest_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_jobs.id"), nullable=False
    )
    # Which leader + scenario this fill belongs to. scenario is one of
    # optimistic | neutral | pessimistic.
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    scenario: Mapped[str] = mapped_column(Text, nullable=False)
    # Monotonic per-(job, leader, scenario) fill index.
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # BUY | SELL | MTM.
    side: Mapped[str] = mapped_column(Text, nullable=False)
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    shares: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    fee: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False, default=0)
    pnl: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False, default=0)
    # Equity (initial_capital + cumulative PnL) AFTER this fill — the curve.
    equity_after: Mapped[float | None] = mapped_column(Numeric(16, 4), nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

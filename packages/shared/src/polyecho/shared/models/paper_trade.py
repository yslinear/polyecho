"""ORM model for the ``paper_trades`` table (C1, PolyHermes paper ladder).

C1-P1 schema only.  Mirrors the B1 ``backtest_trades`` per-fill shape /
column types — the paper engine reuses the same slippage / fee / FIFO /
PnL math, so the persisted fill row is intentionally the same shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class PaperTrade(Base):
    """One simulated per-fill row of a ``paper_session`` (C1).

    The paper-trading analogue of ``backtest_trades``: each simulated fill
    (BUY / SELL / MTM) is persisted as its own row carrying the running
    paper balance (``balance_after``).  The column shape/types mirror
    ``backtest_trades`` because the per-trade simulation math (slippage,
    fee, FIFO PnL) is reused verbatim — the only difference is incremental
    vs batch.  ``balance_after`` is PolyHermes' ``backtest_trade``
    balance-after slot.
    """

    __tablename__ = "paper_trades"
    __table_args__ = (
        # The /research detail page loads every fill for one session; the
        # SQL migration creates the same named index (3-way aligned).
        Index("idx_paper_trades_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_sessions.id"), nullable=False
    )
    # BUY | SELL | MTM (mark-to-market settlement row).
    side: Mapped[str] = mapped_column(Text, nullable=False)
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    shares: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    fee: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False, default=0)
    pnl: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False, default=0)
    # Paper balance (capital + cumulative PnL) AFTER this fill — the curve.
    balance_after: Mapped[float | None] = mapped_column(Numeric(16, 4), nullable=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

"""ORM model for the ``my_trades`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class MyTrade(Base):
    """A copy-trade placed by ``trader-core`` on your behalf.

    Links back to the ``accounts`` table (which account placed the order) and
    to the ``leader_trades`` table (which leader trade triggered it).

    ``fill_status`` lifecycle: pending → filled | rejected | partial.
    ``pnl_usd`` is populated when the position is closed or at resolution.
    """

    __tablename__ = "my_trades"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    copy_of_trade_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("leader_trades.id", ondelete="SET NULL"),
        nullable=True,
    )

    market_id: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)   # BUY | SELL
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)

    # pending | filled | rejected | partial
    fill_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    pnl_usd: Mapped[Optional[float]] = mapped_column(Numeric(14, 4), nullable=True)

    opened_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

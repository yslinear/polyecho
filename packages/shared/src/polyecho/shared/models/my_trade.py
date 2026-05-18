"""ORM model for the ``my_trades`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

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
    # SQL/Drizzle: plain ``REFERENCES accounts(id)`` (no ON DELETE).
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id"),
        nullable=False,
    )
    # SQL/Drizzle: leader_address/market_id/side/size/price are NULLable
    # and ``copy_of_trade_id`` is a plain TEXT column (NOT a BigInteger FK
    # to leader_trades.id) — align the ORM to the migration (SQL = truth).
    leader_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    copy_of_trade_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    market_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    side: Mapped[str | None] = mapped_column(Text, nullable=True)   # BUY | SELL
    size: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)

    # pending | filled | rejected | partial
    fill_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    pnl_usd: Mapped[float | None] = mapped_column(Numeric(14, 4), nullable=True)

    # FIFO sell-match accounting (brief §3/§5): on a SELL, shares are
    # matched against unmatched BUY rows keyed (account, condition,
    # outcome). ``matched_qty`` = qty already consumed by later sells;
    # ``remaining_qty`` = unmatched BUY qty still available for FIFO.
    matched_qty: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    remaining_qty: Mapped[float | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )

    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

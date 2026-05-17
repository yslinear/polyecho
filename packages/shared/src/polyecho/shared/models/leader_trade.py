"""ORM model for the ``leader_trades`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class LeaderTrade(Base):
    """Cached copy of a leader's on-chain trade event.

    Written by ``trader-core`` when a new fill is detected via WebSocket or
    Polygon RPC.  Used as the trigger for copy-trade evaluation and as a
    historical record for the backtest engine.

    ``trade_id`` is the Polymarket trade identifier (unique across all markets).
    ``raw`` stores the full API/WebSocket payload for debugging and future
    schema evolution.
    """

    __tablename__ = "leader_trades"
    __table_args__ = (
        # Cross-source dedup key (brief §3): txHash is THE idempotency
        # anchor across the WS-user-channel path and the on-chain RPC
        # cross-check path.
        UniqueConstraint(
            "leader_address", "tx_hash", name="uq_leader_trades_leader_tx"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    trade_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    leader_address: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market_id: Mapped[str] = mapped_column(Text, nullable=False)
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cross-source dedup key (brief §3); nullable until the fill is
    # attributed to an on-chain tx (some WS events arrive pre-settlement).
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    side: Mapped[str] = mapped_column(Text, nullable=False)  # BUY | SELL
    size: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

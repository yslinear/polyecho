"""ORM model for the ``leader_trades`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Numeric,
    Text,
    UniqueConstraint,
)
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
        UniqueConstraint("leader_address", "tx_hash", name="uq_leader_trades_leader_tx"),
        # A3 (PolyHermes deep-dive): 3-way mirror of the SQL
        # ck_leader_trades_status — only the two known copy-attempt
        # outcomes (or NULL) are storable.
        CheckConstraint(
            "status IS NULL OR status IN ('success', 'failed')",
            name="ck_leader_trades_status",
        ),
    )

    # NULLability + column set aligned to 0000_init.sql (SQL = source of
    # truth): market_id/side/size/price/timestamp are NULLable there, and
    # the leader_address/timestamp indexes are SQL-managed
    # (idx_leader_trades_leader_timestamp) — the ORM-level ``index=True``
    # generated DUPLICATE ix_* indexes not in the migration, so they are
    # removed. ``condition_id`` exists in SQL/Drizzle and was missing here.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    trade_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    market_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Cross-source dedup key (brief §3); nullable until the fill is
    # attributed to an on-chain tx (some WS events arrive pre-settlement).
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    side: Mapped[str | None] = mapped_column(Text, nullable=True)  # BUY | SELL
    size: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # A3 (PolyHermes deep-dive): durable per-trade copy-attempt outcome
    # (PolyHermes' processed_trade.status slot). 'success' = the copy order
    # was placed; 'failed' = a copy attempt was made but did not place
    # (retries exhausted / executor reject). Persisted on BOTH paths so a
    # WS/poll redelivery of a known-bad trade is not re-attempted in a
    # loop. NULLable: a pure-cache leader-trade row (no copy attempt) has
    # no outcome yet. CHECK-constrained to the two known values above.
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

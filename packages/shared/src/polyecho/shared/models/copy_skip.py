"""ORM model for the ``copy_skips`` table (PolyHermes brief §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class CopySkip(Base):
    """A queryable skip / error log entry (brief §7).

    PolyHermes only surfaces failures via Telegram (ephemeral, not
    queryable).  PolyEcho persists every skip/reject with a stable
    reason taxonomy (``skip_reason`` ∈
    :class:`polyecho.shared.copytrading.SkipReason`) so the dashboard /
    CLI can answer "why was this trade not copied?" historically.

    Append-only; ``payload`` keeps the original detected-trade context for
    forensic replay.
    """

    __tablename__ = "copy_skips"
    # NAMED index in SQL/Drizzle (idx_copy_skips_created_at). The prior
    # ORM ``created_at … index=True`` emitted an unmanaged
    # ix_copy_skips_created_at — replace with the migration's named index.
    __table_args__ = (
        Index("idx_copy_skips_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    leader_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # One of polyecho.shared.copytrading.SkipReason (the StrEnum value is
    # the stored string — stable + queryable).
    skip_reason: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

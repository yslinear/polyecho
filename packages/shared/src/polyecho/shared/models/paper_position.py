"""ORM model for the ``paper_positions`` table (C1, PolyHermes paper ladder).

C1-P1 schema only.  Holds the open FIFO lots of a ``paper_session`` so the
paper worker can rebuild open-position state after a restart.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class PaperPosition(Base):
    """The open FIFO lots for one (session, condition_id) of a paper run.

    One row per open position.  ``lots`` is a JSONB array of open FIFO
    lots, each ``{"shares": ..., "fill_price": ..., "opened_at": ...}``.
    A single-row-per-position JSONB array (vs one DB row per lot) is the
    cleaner shape here: the FIFO discipline is purely positional, the worker
    rewrites the whole lot list atomically when a fill consumes / appends
    lots, and rebuilding all open state after a restart is one indexed
    ``SELECT ... WHERE session_id = ?`` — no per-lot ordering column or
    join needed.  A fully-closed position is represented by an empty
    ``lots`` array (or the row deleted) — the C1-P3 worker decides.
    """

    __tablename__ = "paper_positions"
    __table_args__ = (
        # The paper worker rebuilds open state by session on restart; the
        # SQL migration creates the same named index (3-way aligned).
        Index("idx_paper_positions_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_sessions.id"), nullable=False
    )
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Open FIFO lots — JSONB array of {shares, fill_price, opened_at}.
    # NOT NULL DEFAULT '[]' so a freshly inserted position is a valid empty
    # lot list without a NULL special-case in the worker.
    lots: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

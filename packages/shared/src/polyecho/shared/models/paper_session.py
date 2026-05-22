"""ORM model for the ``paper_sessions`` table (C1, PolyHermes paper ladder).

C1-P1 schema only — this module declares the data model for the forward
paper-trading ladder.  No worker / state-machine / gate logic lives here
(that arrives in C1-P3); this is the persistence shape a future paper
worker reads and writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class PaperSession(Base):
    """One forward shadow-copy run of a single leader (C1).

    A leader at ``candidate_leaders.lifecycle_state = 'paper'`` has exactly
    one open ``paper_session``.  Each ~15-min paper-worker cycle fetches the
    leader's new trades since ``cursor_ts``, simulates each, persists a
    ``paper_trade`` row, rebuilds open lots in ``paper_positions``, and
    recomputes the denormalised summary columns (``score`` / ``trade_count``
    / ``copyable_pnl`` / ``max_drawdown`` / ``unknown_exposure_ratio`` /
    ``filtered_ratio``) the state machine gates on.

    ``cursor_ts`` is the high-water mark of the last simulated leader-trade
    timestamp — the anti-duplication anchor so a worker restart resumes
    without re-simulating.  All summary columns are NULLable: a freshly
    opened session has no stats yet.
    """

    __tablename__ = "paper_sessions"
    __table_args__ = (
        # The paper worker scans sessions by leader on every cycle; the SQL
        # migration creates the same named index (3-way aligned).
        Index("idx_paper_sessions_leader", "leader_address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    # open | trial_ready | cooldown | retired (session lifecycle, set by the
    # C1-P3 state machine — this phase only stores it).
    state: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Simulated starting paper capital for this session.
    capital_usd: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)

    # Denormalised summary — recomputed each cycle, gated on by the ladder.
    score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    copyable_pnl: Mapped[float | None] = mapped_column(Numeric(16, 4), nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    unknown_exposure_ratio: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    filtered_ratio: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    # High-water mark of the last simulated leader-trade timestamp — the
    # anti-duplication cursor a restart resumes from. NULLable: a session
    # that has simulated nothing yet has no cursor.
    cursor_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

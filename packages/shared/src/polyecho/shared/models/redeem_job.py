"""ORM model for the ``redeem_jobs`` table (PolyHermes brief §9)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class RedeemJob(Base):
    """A durable claim-winnings / redeem intent (brief §9).

    Improves on PolyHermes' in-memory-only redeem dedup: this table
    survives restart and is idempotent on
    ``(account_id, condition_id, outcome_index)`` so a position is never
    double-redeemed.  Modelled after ``backtest_jobs`` (worker-polled
    queue with a ``status`` lifecycle).

    ``status`` lifecycle: pending → submitted → confirmed | failed.
    Collateral (pUSD vs WrappedCollateral) is derived from
    ``is_neg_risk`` at execution time — the brief's highest-risk detail.
    """

    __tablename__ = "redeem_jobs"
    # SQL/Drizzle: plain ``REFERENCES`` + NAMED index idx_redeem_jobs_status.
    # The prior ORM ``status … index=True`` emitted an unmanaged
    # ix_redeem_jobs_status; replace with the migration's named index.
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "condition_id",
            "outcome_index",
            name="uq_redeem_jobs_account_condition_outcome",
        ),
        # 3-way mirror of the SQL ck_redeem_jobs_index_set: a wrong bitmask
        # must not silently redeem the wrong outcome. NULL-tolerant for
        # not-yet-resolved jobs. Defence-in-depth — index_set_for() already
        # computes 1 << outcome_index in Python.
        CheckConstraint(
            "index_set IS NULL OR index_set = (1::BIGINT << outcome_index)",
            name="ck_redeem_jobs_index_set",
        ),
        Index("idx_redeem_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id"),
        nullable=False,
    )
    condition_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1 << outcome_index bitmask passed to CTF.redeemPositions (brief §9).
    # BigInteger (not Integer): 1 << outcome_index overflows INT4 for
    # outcome_index >= 31 (NegRisk multi-outcome) → silent insert failure.
    index_set: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_neg_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # pending | submitted | confirmed | failed
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bounded redeem retry counter. E-core wires the real max_attempts cap
    # (currently _attempt_count is hardcoded to 1 → infinite redeem retry);
    # this column only durably stores the count.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

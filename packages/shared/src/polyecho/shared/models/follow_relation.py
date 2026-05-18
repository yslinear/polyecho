"""ORM model for the ``follow_relations`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class FollowRelation(Base):
    """Binding between one of your accounts, a leader wallet, and a copy template.

    Represents an active (or paused) copy-trade relationship.
    ``status`` transitions: active ↔ paused.

    PRD §4.2: control is exerted by changing this row's ``status`` via
    the Dashboard — ``trader-core`` polls / receives NOTIFY and acts
    accordingly without any direct network exposure.
    """

    __tablename__ = "follow_relations"
    # SQL/Drizzle declare plain ``REFERENCES`` (no ON DELETE) and a NAMED
    # index ``idx_follow_relations_status``. The prior ORM used
    # ondelete=CASCADE/RESTRICT + ``index=True`` (which emits an unmanaged
    # ``ix_follow_relations_status``) — both diverged from the migration.
    # Align to SQL (source of truth): no ondelete, named index.
    __table_args__ = (
        Index("idx_follow_relations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id"),
        nullable=False,
    )
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("copy_templates.id"),
        nullable=False,
    )
    # active | paused
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Per-leader reconcile cursor (brief §7): the last activity timestamp
    # reconciled from the Data API, so a WS gap / restart can backfill
    # missed trades instead of losing them. NULL = never reconciled yet.
    last_seen_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

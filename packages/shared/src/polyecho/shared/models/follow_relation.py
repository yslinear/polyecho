"""ORM model for the ``follow_relations`` table (PRD §5.5, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
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

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    leader_address: Mapped[str] = mapped_column(Text, nullable=False)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("copy_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # active | paused
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

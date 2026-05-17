"""ORM model for the ``pending_actions`` table (PRD §5.7, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class PendingAction(Base):
    """A dangerous operation awaiting two-factor confirmation via the Dashboard.

    PRD §5.7 security model: high-risk operations (manual sell, emergency
    liquidation) require a 5-second countdown + passphrase re-entry in the UI
    before ``trader-core`` executes them.

    ``status`` lifecycle: awaiting_confirm → confirmed | expired | rejected.
    ``confirm_passphrase_hash`` stores a salted hash of the passphrase used to
    authorise the action — **never** the plaintext passphrase.
    ``expires_at`` allows the UI to enforce a time-bound confirmation window.
    """

    __tablename__ = "pending_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # awaiting_confirm | confirmed | expired | rejected
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="awaiting_confirm"
    )

    # Salted hash of the passphrase supplied for authorisation (NOT plaintext)
    confirm_passphrase_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""ORM model for the ``push_subscriptions`` table (PRD §5.8, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class PushSubscription(Base):
    """A registered Web Push endpoint (one per browser/device).

    Created when the user grants notification permission in the Dashboard and
    the Service Worker registers a ``PushSubscription``.  The ``endpoint``,
    ``p256dh_key``, and ``auth_key`` are delivered by the browser and used by
    ``web-push-sender`` to dispatch VAPID-authenticated pushes.

    PRD §5.8: iOS Safari 16.4+ only supports Web Push when the Dashboard is
    added to the Home Screen as a PWA.
    """

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    p256dh_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    device_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

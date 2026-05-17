"""ORM model for the ``events`` table (PRD §5.8, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, Identity, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class Event(Base):
    """A system event written by any service and consumed by ``web-push-sender``.

    All services (``trader-core``, ``discovery-worker``, etc.) append rows here
    when something noteworthy happens.  ``web-push-sender`` polls (or receives
    a NOTIFY) for new rows and dispatches them to all registered
    ``PushSubscription`` endpoints.

    PRD §5.8 event types include: ``trade_opened``, ``trade_closed``,
    ``pnl_daily_summary``, ``risk_triggered``, ``system_error``,
    ``new_leader_candidate``, ``action_pending_confirm``.

    ``pushed_at`` is set by ``web-push-sender`` after successful delivery.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    pushed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

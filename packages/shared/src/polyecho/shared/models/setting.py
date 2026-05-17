"""ORM model for the ``settings`` table (PRD §5.7, §6.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class Setting(Base):
    """Key / value store for runtime configuration.

    Uses a single-table, multi-key design (one row per setting key) to allow
    the Dashboard to update individual settings without touching others.
    ``value`` is typed as JSONB to support scalars, lists, and objects.

    Common keys: ``global_pause``, ``conservative_mode``, ``default_template_id``.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

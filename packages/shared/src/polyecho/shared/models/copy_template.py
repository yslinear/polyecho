"""ORM model for the ``copy_templates`` table (PRD §5.6, §6.1)."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import Boolean, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class CopyTemplate(Base):
    """Risk / position-sizing preset applied to one or more follow relations.

    PRD §5.6 defines three built-in presets (Conservative / Balanced /
    Aggressive).  Users may create additional custom templates via the
    Dashboard settings page.

    ``copy_ratio`` and ``fixed_size_usd`` are mutually exclusive:
    use ``copy_ratio`` to scale with the leader's size, or
    ``fixed_size_usd`` to trade a fixed USDC amount per order.
    """

    __tablename__ = "copy_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Sizing (mutually exclusive)
    copy_ratio: Mapped[Optional[float]] = mapped_column(Numeric(10, 6), nullable=True)
    fixed_size_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    # Risk limits
    max_position_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    min_trade_size_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    max_concurrent_positions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    daily_loss_limit_usd: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    # Market filters
    category_whitelist: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    exclude_short_markets: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    use_fok_orders: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

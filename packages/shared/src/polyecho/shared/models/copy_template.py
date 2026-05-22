"""ORM model for the ``copy_templates`` table (PRD §5.6, §6.1)."""

from __future__ import annotations

import uuid

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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SQL/Drizzle do NOT declare ``name`` UNIQUE — drop the ORM-only unique
    # so Base.metadata matches the migration (3-way alignment, SQL = truth).
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Sizing (mutually exclusive). NUMERIC(8,5) in all 3 sources.
    copy_ratio: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    fixed_size_usd: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Risk limits
    max_position_usd: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    min_trade_size_usd: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    max_concurrent_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_loss_limit_usd: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Market filters. SQL/Drizzle declare these NULLable (no NOT NULL) and
    # the booleans default false / true respectively (SQL = source of
    # truth — fixes the prior ORM ``exclude_short_markets default=True``
    # inversion and the NOT NULL the migration never had).
    category_whitelist: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True, default=list
    )
    exclude_short_markets: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=False
    )
    use_fok_orders: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)

    # Tri-mode keyword filter + upper market-end bound (PolyHermes deep-dive
    # B4). ``keyword_filter_mode`` is ``disabled`` | ``whitelist`` |
    # ``blacklist`` (SQL default ``disabled`` so existing templates are
    # unchanged); ``keywords`` is the operator term list matched against a
    # market title/category. ``market_max_end_seconds`` is the upper end
    # bound mirroring the lower ``market_min_end_seconds`` guard.
    keyword_filter_mode: Mapped[str | None] = mapped_column(Text, nullable=True, default="disabled")
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True, default=list)
    market_max_end_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

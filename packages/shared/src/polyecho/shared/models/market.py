"""ORM model for the ``markets`` table (C4, PolyHermes deep-dive)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class Market(Base):
    """Persisted market-metadata cache row (C4, PolyHermes deep-dive).

    PolyHermes' ``markets`` table equivalent: the live datasource hits the
    Polymarket data-api / Gamma every cycle for the same market metadata
    (question, slug, ``neg_risk``, ``tick_size``, ``end_date``, outcomes)
    with no persistence.  This table backs a cache -> DB -> remote read
    layering (see :mod:`polyecho.shared.datasource.market_cache`).

    Immutability tier
    -----------------
    A market that is RESOLVED/settled is immutable: its metadata and
    ``settled_price`` never change again, so a cached row is served forever
    with no remote re-fetch.  An unresolved market uses a short freshness
    window keyed on ``updated_at``.

    ``condition_id`` is the natural primary key — it is THE market
    identifier shared across the activity / positions / holders APIs.
    ``raw`` keeps the full upstream payload for forward-compat / debugging.
    """

    __tablename__ = "markets"
    __table_args__ = (
        # The cache layer's opportunistic-refresh sweep selects unresolved
        # rows by staleness; a partial index keeps it to the live subset
        # (3-way aligned with idx_markets_unresolved_updated_at in
        # 0000_init.sql).
        Index(
            "idx_markets_unresolved_updated_at",
            "updated_at",
            postgresql_where="is_resolved = false",
        ),
    )

    # condition_id is the natural PK (the 0x... 64-hex market id).
    condition_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Polymarket internal market id (the gamma-api "id" / data-api
    # "market_id"); NULLable since not every payload carries it.
    market_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-readable market question/title.
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    # URL slug.
    slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NegRisk flag — selects the NegRisk Exchange contract for orders.
    neg_risk: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # CLOB price tick size (e.g. 0.01); stored as text to preserve the
    # exact decimal string the API returns.
    tick_size: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Market resolution date/time.
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Outcome token list (token_id / outcome / price dicts).
    outcomes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Resolved/settled flag — once True the row is immutable (cached
    # forever, no remote re-fetch).
    is_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Settled winning-outcome price (0.0 / 1.0 for binary), NULL until the
    # market resolves.
    settled_price: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    # Full upstream payload (forward-compat / debugging).
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # When this row was first cached.
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # When this row was last refreshed from a remote read — the freshness
    # window for an unresolved market is measured against this.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

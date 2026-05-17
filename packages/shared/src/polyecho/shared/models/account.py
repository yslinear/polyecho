"""ORM model for the ``accounts`` table (PRD §5.1, §6.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from polyecho.shared.db import Base


class Account(Base):
    """Represents one of your own Polymarket wallets.

    A single user may have multiple accounts (e.g. one per wallet type).
    The ``encrypted_private_key`` column is written **only** by the CLI
    import command (``polyecho account import``); the web process must
    never access it.

    PRD §5.1 — four signature types:
        * 0 = EOA (plain MetaMask)
        * 1 = POLY_PROXY (Magic.link / email)
        * 2 = POLY_GNOSIS_SAFE (MetaMask via polymarket.com)
        * 3 = POLY_1271 (Deposit Wallet)
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    eoa_address: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    funder_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signature_type: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_private_key: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    # pending_key_import | ready | disabled
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_key_import")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

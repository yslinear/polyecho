"""
polyecho.shared.config
======================
Application settings backed by pydantic-settings.

All fields are read from environment variables with the ``POLYECHO_`` prefix.
The canonical variable names mirror the repo ``.env.example``.

Usage::

    from polyecho.shared.config import get_settings

    settings = get_settings()          # cached singleton
    engine = create_async_engine(settings.db_url)
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for all PolyEcho services.

    All values are read from environment variables prefixed with
    ``POLYECHO_``.  Sensitive fields (private keys, passphrases) are typed
    as ``SecretStr`` so they are never accidentally logged or repr'd.
    """

    model_config = SettingsConfigDict(
        env_prefix="POLYECHO_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    db_url: str = Field(
        default="postgresql+asyncpg://polyecho:polyecho@localhost:5432/polyecho",
        description="Async SQLAlchemy DB URL (asyncpg driver).",
    )
    db_url_drizzle: str = Field(
        default="postgresql://polyecho:polyecho@localhost:5432/polyecho",
        description="Synchronous Postgres URL used by Drizzle ORM in the Next.js web package.",
    )

    # ------------------------------------------------------------------
    # Polymarket / Polygon
    # ------------------------------------------------------------------
    polymarket_clob_url: str = Field(
        default="https://clob.polymarket.com",
        description="Base URL for the Polymarket CLOB (Central Limit Order Book) API.",
    )
    polymarket_data_api_url: str = Field(
        default="https://data-api.polymarket.com",
        description="Base URL for the Polymarket Data API (leaderboard, activity, positions).",
    )
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        description="Polygon JSON-RPC endpoint for on-chain event monitoring.",
    )
    polymarket_chain_id: int = Field(
        default=137,
        description="EVM chain ID. 137 = Polygon mainnet; 80002 = Amoy testnet.",
    )

    # ------------------------------------------------------------------
    # Key encryption  (CLI only — never read by the web process)
    # ------------------------------------------------------------------
    key_kdf_iterations: int = Field(
        default=600_000,
        description=(
            "PBKDF2-HMAC-SHA256 iteration count for private-key encryption. "
            "600 000 matches the OWASP 2023 recommendation."
        ),
    )

    # ------------------------------------------------------------------
    # Web Push / VAPID  (RFC 8292)
    # ------------------------------------------------------------------
    vapid_public_key: str = Field(
        default="",
        description="VAPID public key (base64url-encoded uncompressed EC point).",
    )
    vapid_private_key: SecretStr = Field(
        default=SecretStr(""),
        description="VAPID private key — treated as a secret, never logged.",
    )
    vapid_subject: str = Field(
        default="mailto:yslinear@gmail.com",
        description="VAPID contact URI (mailto: or https:) sent in the Authorization header.",
    )

    # ------------------------------------------------------------------
    # Web dashboard
    # ------------------------------------------------------------------
    web_port: int = Field(
        default=3000,
        description="TCP port on which the Next.js dashboard listens.",
    )

    # ------------------------------------------------------------------
    # Datasource
    # ------------------------------------------------------------------
    datasource: Literal["fixture", "live"] = Field(
        default="fixture",
        description=(
            "Selects the DataSource implementation.  "
            "'fixture' (default) uses bundled JSON fixtures — no network required.  "
            "'live' calls the Polymarket Data API at polymarket_data_api_url."
        ),
    )

    # ------------------------------------------------------------------
    # Ops
    # ------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Minimum log level emitted by structlog.",
    )
    tz: str = Field(
        default="Asia/Taipei",
        description="IANA timezone name used for display and scheduled tasks.",
    )


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application-wide :class:`Settings` singleton.

    The result is cached via :func:`functools.lru_cache` so environment
    variables are parsed only once per process.  In tests, clear the cache
    with ``get_settings.cache_clear()`` after monkeypatching env vars.
    """
    return Settings()

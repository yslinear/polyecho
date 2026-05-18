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

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Hard OWASP-floor minimum for PBKDF2-HMAC-SHA256 iterations (P0-7).
#:
#: ``Settings.key_kdf_iterations`` is clamped UP to this value: anything
#: lower (a typo, a too-fast test override, a hostile env) is silently
#: raised to ``MIN_KDF_ITERATIONS``.  This is the SINGLE source of truth for
#: the project's KDF iteration floor and is exported so the TypeScript web
#: mirror (Node ``crypto.pbkdf2``) can match it byte-for-byte.  100 000 is
#: the OWASP 2023 minimum for PBKDF2-HMAC-SHA256; the default
#: (``key_kdf_iterations`` = 600 000) stays well above it.
MIN_KDF_ITERATIONS: int = 100_000


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
    polymarket_proxy_url: str | None = Field(
        default=None,
        description=(
            "Optional outbound proxy for ALL Polymarket API calls "
            "(data-api httpx, CLOB client, balance probe, doctor). "
            "Format: http://[user:pass@]host:port (https:// also fine; "
            "socks5:// needs httpx[socks], not bundled). Needed where "
            "Polymarket is geo-blocked (PRD §8.1). None = direct connection. "
            "Env-level default; the `settings` DB table key "
            "'polymarket_proxy_url' overrides this at runtime (set via the "
            "web /settings page — PRD §5.7 安全寫入 tier)."
        ),
    )

    # ------------------------------------------------------------------
    # Key encryption  (CLI only — never read by the web process)
    # ------------------------------------------------------------------
    key_kdf_iterations: int = Field(
        default=600_000,
        description=(
            "PBKDF2-HMAC-SHA256 iteration count for private-key encryption. "
            "600 000 matches the OWASP 2023 recommendation.  Clamped UP to "
            f"a hard floor of {MIN_KDF_ITERATIONS} (MIN_KDF_ITERATIONS): a "
            "lower value (typo / hostile env) is silently raised, never "
            "honoured.  Everything that derives a key reads this clamped "
            "value (config is the single chokepoint)."
        ),
    )

    @field_validator("key_kdf_iterations")
    @classmethod
    def _clamp_kdf_iterations(cls, v: int) -> int:
        """Fail-closed clamp: ``max(v, MIN_KDF_ITERATIONS)`` (P0-7).

        Runs on every load so *any* reader of ``settings.key_kdf_iterations``
        (``crypto._derive_key``, the danger-passphrase hash) always gets a
        value at or above the OWASP floor — no caller can opt below it.
        """
        return max(int(v), MIN_KDF_ITERATIONS)

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
    # Live trading — Phase-4 HARD safety lock (brief §9; default OFF)
    # ------------------------------------------------------------------
    live_trading: bool = Field(
        default=False,
        description=(
            "MASTER live-money lock (env POLYECHO_LIVE_TRADING). When "
            "False (the default), polyecho.shared.copytrading."
            "assert_live_allowed() RAISES before any CLOB order / on-chain "
            "redeem — the single chokepoint Wave-2's order & redeem "
            "placement MUST call. Never enable unless you intend to move "
            "real funds. DANGER: enabling this places live orders."
        ),
    )
    auto_redeem: bool = Field(
        default=False,
        description=(
            "Enable automatic claim-winnings / redeem (env "
            "POLYECHO_AUTO_REDEEM). When True AND live_trading is True, "
            "the redeem worker acts on the data-api `redeemable` flag "
            "(brief §9). Default OFF; still gated by the live lock."
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

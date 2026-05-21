"""
polyecho.shared.config
======================
Application settings backed by pydantic-settings.

All fields are read from environment variables with the ``POLYECHO_`` prefix.
The canonical variable names mirror the repo ``.env.example``.

Usage::

    from polyecho.shared.config import get_settings

    settings = get_settings()          # cached singleton
    engine = create_async_engine(settings.db_url.get_secret_value())
"""

from __future__ import annotations

import functools
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel DSNs used as the *fixture-mode-only* defaults for ``db_url`` and
#: ``db_url_drizzle``.  They are intentionally pointed at ``localhost`` so the
#: test/CI suite (which runs with ``POLYECHO_DATASOURCE=fixture``) does not
#: need to set DB URLs.  In ``live`` mode the model validator below RAISES
#: when the resolved URL still equals one of these sentinels — production
#: must export ``POLYECHO_DB_URL`` / ``POLYECHO_DB_URL_DRIZZLE`` explicitly
#: (P1-3: no silent localhost fallback in live).
_FIXTURE_DEFAULT_DB_URL: str = "postgresql+asyncpg://polyecho:polyecho@localhost:5432/polyecho"
_FIXTURE_DEFAULT_DB_URL_DRIZZLE: str = "postgresql://polyecho:polyecho@localhost:5432/polyecho"

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
    # SecretStr: the DSN embeds the DB password, so it must never be logged
    # or repr'd in clear. Read the value via ``.get_secret_value()``.
    #
    # P1-3 fail-fast: the localhost default below is allowed ONLY in fixture
    # mode (tests / CI).  When ``datasource == "live"`` the model validator
    # ``_require_explicit_db_url_in_live_mode`` RAISES ``RuntimeError`` if the
    # URL still equals the bundled localhost sentinel — production must set
    # ``POLYECHO_DB_URL`` explicitly (no silent localhost fallback).
    db_url: SecretStr = Field(
        default=SecretStr(_FIXTURE_DEFAULT_DB_URL),
        description="Async SQLAlchemy DB URL (asyncpg driver). Secret (DSN holds the password).",
    )
    db_url_drizzle: SecretStr = Field(
        default=SecretStr(_FIXTURE_DEFAULT_DB_URL_DRIZZLE),
        description=(
            "Synchronous Postgres URL used by Drizzle ORM in the Next.js web "
            "package. Secret (DSN holds the password)."
        ),
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
        description=(
            "Polygon JSON-RPC endpoint for on-chain event monitoring.  "
            "ADVANCED: may also be a comma-separated list of endpoints "
            "(e.g. 'https://polygon.drpc.org,https://polygon-bor-rpc.publicnode.com'); "
            "in that case the integration probe and SAFE-proxy resolver iterate "
            "through them in order and report success on the first that works. "
            "The single-URL form remains fully supported for backward compatibility. "
            "Back-compat: this attribute always exposes the FIRST URL as a plain "
            "string (so legacy single-URL callers in core.wiring / "
            "core.onchain_reconcile keep working unchanged). Read the full list "
            "via :meth:`Settings.polygon_rpc_urls`."
        ),
    )

    @field_validator("polygon_rpc_url", mode="before")
    @classmethod
    def _normalize_polygon_rpc_url(cls, v: Any) -> Any:
        """Accept a single URL **or** a CSV; store the first URL as the str.

        Back-compat: ``polygon_rpc_url`` stays typed as ``str`` so every legacy
        caller (``core.wiring`` / ``core.onchain_reconcile``) keeps working
        unchanged.  The full list is exposed via
        :meth:`Settings.polygon_rpc_urls` (parsed from this same env value).
        Empty entries and surrounding whitespace are dropped; an empty / blank
        env value falls back to pydantic's field default.

        ``mode="before"`` so we receive the raw env string; non-``str`` /
        ``None`` inputs are returned untouched so the field's own coercion
        path (or pydantic's TypeError) runs unchanged.
        """
        if v is None or not isinstance(v, str):
            return v
        first = next(
            (part.strip() for part in v.split(",") if part.strip()),
            "",
        )
        return first or v.strip()

    @property
    def polygon_rpc_urls(self) -> list[str]:
        """Ordered list of Polygon RPC endpoints (1 or more).

        Parses ``POLYECHO_POLYGON_RPC_URL`` as a comma-separated list,
        trims whitespace, and drops empty entries.  The single-URL case
        returns a one-element list — the integration probe and SAFE-proxy
        resolver iterate this list in order on each call, advancing on any
        timeout / transport / HTTP error, and report success on the first
        endpoint that works.

        Reads the raw env directly (not the normalized ``polygon_rpc_url``
        field, which holds only the FIRST URL for back-compat).  Falls back
        to ``[polygon_rpc_url]`` when the env var is absent.
        """
        import os

        raw = os.environ.get("POLYECHO_POLYGON_RPC_URL", "")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            return parts
        # Env unset / blank → use whatever the field resolved to (its default
        # or a model-construction override).  Always at least one entry.
        return [self.polygon_rpc_url]

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
        default="mailto:admin@example.com",
        description=(
            "VAPID contact URI (mailto: or https:) sent in the "
            "Authorization header of every push request (RFC 8292). "
            "R13: defaults to a NON-personal placeholder — set "
            "POLYECHO_VAPID_SUBJECT to the operator's own contact before "
            "enabling web push, or this placeholder is sent to push "
            "services. Never ship a real personal address as the default."
        ),
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
    # NOTE: there is intentionally NO opt-in to persist fixture/synthetic
    # data.  Fixture mode is UNCONDITIONALLY non-persisting (the discovery
    # entrypoint closes the worker persistence gate on the FIXTURE
    # datasource with no bypass, and trader-core never fabricates follows)
    # so synthetic/fixture data can NEVER be written to the operator DB —
    # 不再有任何產生假資料的邏輯.

    # ------------------------------------------------------------------
    # Auto-redeem toggle (brief §9; default OFF — per-op confirmation
    # lives at the web layer)
    # ------------------------------------------------------------------
    auto_redeem: bool = Field(
        default=False,
        description=(
            "Enable automatic claim-winnings / redeem (env "
            "POLYECHO_AUTO_REDEEM). When True the redeem worker acts on "
            "the data-api `redeemable` flag (brief §9). Default OFF."
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

    # ------------------------------------------------------------------
    # P1-3 fail-fast validators — no silent localhost DB fallback in live
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _require_explicit_db_url_in_live_mode(self) -> Settings:
        """Fail-closed: in live mode the localhost default DB URL is rejected.

        The two ``db_url`` / ``db_url_drizzle`` fields carry a localhost
        sentinel default so the test/CI suite (which runs under
        ``POLYECHO_DATASOURCE=fixture``) does not need DB env vars.
        When the operator runs with ``POLYECHO_DATASOURCE=live`` and forgets
        to export their real Postgres DSN, this validator RAISES instead of
        silently routing every worker / web request at ``localhost:5432`` —
        which on a CI/Docker host is *not* the operator DB and would
        previously have caused either confusing connection errors or, worse,
        writes against the wrong database.
        """
        if self.datasource != "live":
            return self
        if self.db_url.get_secret_value() == _FIXTURE_DEFAULT_DB_URL:
            raise RuntimeError(
                "POLYECHO_DB_URL is required in live mode; export it in .env "
                "(or docker-compose env). The localhost default is only "
                "allowed when POLYECHO_DATASOURCE=fixture."
            )
        if self.db_url_drizzle.get_secret_value() == _FIXTURE_DEFAULT_DB_URL_DRIZZLE:
            raise RuntimeError(
                "POLYECHO_DB_URL_DRIZZLE is required in live mode; export it "
                "in .env (or docker-compose env). The localhost default is "
                "only allowed when POLYECHO_DATASOURCE=fixture."
            )
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application-wide :class:`Settings` singleton.

    The result is cached via :func:`functools.lru_cache` so environment
    variables are parsed only once per process.  In tests, clear the cache
    with ``get_settings.cache_clear()`` after monkeypatching env vars.
    """
    return Settings()

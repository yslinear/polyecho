"""
polyecho.shared — PolyEcho shared library.

Provides the building blocks used by every PolyEcho service:
  - config   : pydantic-settings Settings with POLYECHO_ env prefix
  - db       : SQLAlchemy 2.0 async engine, session factory, Base, helpers
  - models   : ORM-mapped tables for every entity defined in PRD §6.1
  - crypto   : AES-256-GCM private-key encryption / decryption helpers
  - polymarket: Thin async wrapper around py_clob_client_v2.ClobClient
  - logging  : structlog JSON renderer with secret-redacting processor

Import examples::

    from polyecho.shared.config import get_settings
    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Account, CandidateLeader
    from polyecho.shared.crypto import encrypt_private_key, decrypt_private_key
    from polyecho.shared.polymarket.client import PolymarketClient
    from polyecho.shared.logging import configure_logging
"""

from polyecho.shared import (
    config,
    crypto,
    danger_passphrase,
    datasource_contract,
    db,
    health,
    logging,
    models,
)
from polyecho.shared.polymarket import client as polymarket_client

__all__ = [
    "config",
    "crypto",
    "danger_passphrase",
    "datasource_contract",
    "db",
    "health",
    "logging",
    "models",
    "polymarket_client",
]

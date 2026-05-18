"""
polyecho.shared.logging
=======================
Structured JSON logging via ``structlog``.

Key design decisions
--------------------
* **JSON output** — every line is machine-parseable; works with journald,
  ``jq``, Loki, or any log aggregator.
* **Secret redaction** — a log key is redacted when its *name* matches the
  secret pattern (``private_key``, ``secret``, ``passphrase``, ``password``,
  ``api_key``, ``access_token`` / ``…_token``, a cryptographic ``signature``
  / ``…_signature`` — but **NOT** ``signature_type``, which is a small EVM
  enum int, never a secret), OR when a string *value* looks like a known
  secret material (``BEGIN … PRIVATE KEY`` PEM blocks).  The matched value
  is replaced with ``"[REDACTED]"`` before the line is emitted.  The pattern
  is deliberately tight — over-redaction (e.g. nuking ``signature_type``)
  loses operationally-useful, non-sensitive fields.
* **Standard-library bridge** — ``logging.basicConfig`` is configured so that
  third-party libraries that use the stdlib ``logging`` module are also
  captured and formatted by structlog.

Usage::

    from polyecho.shared.logging import configure_logging
    import structlog

    configure_logging(log_level="INFO")
    log = structlog.get_logger(__name__)
    log.info("trader_started", account_id=str(account.id))
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from typing import Any

import structlog

# Keys whose *values* must never appear in emitted logs.
#
# Tightened so ``signature_type`` (a 0–3 EVM signature-mode enum int, not a
# secret) is NOT redacted while a real cryptographic ``signature`` /
# ``…_signature`` / ``signature_hash`` still is.  Each alternative is
# anchored to a word boundary; the ``signature`` alternative uses a
# negative-lookahead so ``signature`` / ``order_signature`` redact but
# ``signature_type`` (and ``signature_types``) do not.
_REDACT_PATTERN: re.Pattern[str] = re.compile(
    r"""
    (?:^|_)                       # start, or a word part after '_'
    (?:
        private_key
      | secret
      | passphrase
      | password
      | api_key
      | (?:access_|refresh_|auth_|bearer_)?token
      | signature(?!_type)        # 'signature' but NOT 'signature_type'
    )
    (?:$|_)                       # end, or continues with '_'
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Best-effort *value* redaction: a string value carrying obvious secret
# material (a PEM private-key block) is redacted regardless of its key name.
_SECRET_VALUE_PATTERN: re.Pattern[str] = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)

_REDACTED_PLACEHOLDER = "[REDACTED]"


def _redact_secrets(
    logger: Any,  # noqa: ANN401  — structlog processor protocol
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """structlog processor that redacts sensitive key-value pairs.

    A value is replaced with ``"[REDACTED]"`` when **either** its key name
    matches :data:`_REDACT_PATTERN` (e.g. ``private_key``, ``passphrase``,
    ``…_signature`` — but *not* ``signature_type``) **or** the value is a
    string carrying obvious secret material (a PEM ``PRIVATE KEY`` block,
    via :data:`_SECRET_VALUE_PATTERN`).  The ``event`` field is never
    redacted (it is the human log message, not a data field).
    """
    for key in list(event_dict.keys()):
        if key == "event":
            continue
        value = event_dict[key]
        if _REDACT_PATTERN.search(key) or (
            isinstance(value, str) and _SECRET_VALUE_PATTERN.search(value)
        ):
            event_dict[key] = _REDACTED_PLACEHOLDER
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog with JSON rendering and secret redaction.

    Call this **once** at process startup, before any log output is produced.
    Subsequent calls are idempotent (structlog allows reconfiguration, but the
    stdlib handler is only added once).

    Args:
        log_level: Minimum log level string, e.g. ``"DEBUG"`` or ``"INFO"``.
                   Defaults to ``"INFO"``.  Overridden by
                   :func:`~polyecho.shared.config.get_settings` in production
                   code — pass ``settings.log_level`` here.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure the stdlib root logger so third-party libraries are captured.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Callable[..., Any]] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Avoid adding duplicate handlers if called more than once.
    already_configured = any(
        isinstance(h, logging.StreamHandler) and h.formatter is formatter
        for h in root_logger.handlers
    )
    if not already_configured:
        root_logger.handlers.clear()
        root_logger.addHandler(handler)

    root_logger.setLevel(level)

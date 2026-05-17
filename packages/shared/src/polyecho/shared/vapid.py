"""
polyecho.shared.vapid
=====================
**FROZEN Phase-5 Wave-1 contract module.**

VAPID (RFC 8292) key + claims helpers built on ``py_vapid`` 1.9.4.
Wave-2 (cli ``vapid-gen``, web ``applicationServerKey``, push-sender
``webpush(...)``) imports these three functions *verbatim*.

What lives here (and only here)
-------------------------------
* :func:`generate_vapid_keypair`  — ``(public_b64url, private_b64url)``.
* :func:`application_server_key`   — normalise the public key for the FE.
* :func:`build_vapid_claims`       — ``{"sub": ...}`` only.

Security
--------
Private-key material is **never** logged.  This module emits no log
records that contain a key.

py_vapid API (verified against the installed 1.9.4 source — not guessed)
------------------------------------------------------------------------
* ``Vapid01()`` + ``.generate_keys()`` → EC ``SECP256R1`` keypair.
* Public "application server key" = the raw **uncompressed** EC point::

      pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

  → 65 bytes (0x04 ‖ X ‖ Y), then ``py_vapid.b64urlencode`` (URL-safe,
  **no padding**) — exactly what a browser passes to
  ``applicationServerKey`` via ``urlBase64ToUint8Array``.
* Private key string ``pywebpush.webpush(vapid_private_key=...)`` accepts:
  the raw 32-byte private value, ``b64urlencode``'d.  ``Vapid01.from_string``
  sees ``len(b64urldecode(s)) == 32`` → routes to ``from_raw`` (verified
  round-trip: ``from_string`` reconstructs the identical private value).
* ``pywebpush.webpush`` auto-fills ``aud`` (from the endpoint) and ``exp``
  (now + 12 h) — confirmed in the pywebpush source — so
  :func:`build_vapid_claims` MUST set **only** ``sub``.
"""

from __future__ import annotations

import logging

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from py_vapid import Vapid01, b64urldecode, b64urlencode

from polyecho.shared.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "application_server_key",
    "build_vapid_claims",
    "generate_vapid_keypair",
]

#: Length of a raw uncompressed EC P-256 point: 0x04 ‖ 32-byte X ‖ 32-byte Y.
_UNCOMPRESSED_POINT_LEN = 65
#: Length of a raw P-256 private scalar.
_PRIVATE_SCALAR_LEN = 32


def generate_vapid_keypair() -> tuple[str, str]:
    """Generate a fresh VAPID EC P-256 keypair.

    Returns:
        ``(public_key_b64url, private_key_b64url)`` where:

        * ``public_key_b64url`` is the raw **uncompressed** EC point
          (65 bytes) in URL-safe base64 **without padding** — the exact
          string a browser hands to ``applicationServerKey``.
        * ``private_key_b64url`` is the raw 32-byte private scalar in the
          same encoding — directly accepted by
          ``pywebpush.webpush(vapid_private_key=...)`` (its
          ``Vapid01.from_string`` parses a 32-byte value via ``from_raw``).

    The private key is never logged.
    """
    vapid = Vapid01()
    vapid.generate_keys()

    public_raw = vapid.public_key.public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(
        _PRIVATE_SCALAR_LEN, "big"
    )

    public_b64 = str(b64urlencode(public_raw))
    private_b64 = str(b64urlencode(private_raw))

    # NOTE: only the public key is logged — the private key MUST NOT be.
    logger.info("vapid_keypair_generated", extra={"public_key": public_b64})
    return public_b64, private_b64


def application_server_key(public_key_b64url: str) -> str:
    """Normalise *public_key_b64url* into the FE ``applicationServerKey``.

    Idempotent identity/normalisation: decodes the (possibly padded or
    standard-base64) input and re-encodes it URL-safe **without padding**,
    validating it is a 65-byte uncompressed EC point.  Use this to hand a
    stored ``settings.vapid_public_key`` to the browser.

    Args:
        public_key_b64url: A base64/base64url VAPID public key.

    Returns:
        The canonical unpadded URL-safe base64 string.

    Raises:
        ValueError: If the decoded key is not a 65-byte uncompressed point.
    """
    raw: bytes = bytes(b64urldecode(public_key_b64url.strip().encode()))
    if len(raw) != _UNCOMPRESSED_POINT_LEN or raw[0] != 0x04:
        raise ValueError(
            "VAPID public key must be a 65-byte uncompressed EC P-256 point"
        )
    return str(b64urlencode(raw))


def build_vapid_claims(subject: str | None = None) -> dict[str, str]:
    """Build the VAPID JWT claims for ``pywebpush.webpush(vapid_claims=...)``.

    Returns **only** ``{"sub": <subject>}``.  ``pywebpush`` fills ``aud``
    (derived from the subscription endpoint) and ``exp`` (now + 12 h)
    itself — setting them here would be overwritten / can break delivery,
    so we intentionally do not.

    Args:
        subject: Override contact URI; defaults to
            ``Settings.vapid_subject`` (``POLYECHO_VAPID_SUBJECT``).
    """
    return {"sub": subject or get_settings().vapid_subject}

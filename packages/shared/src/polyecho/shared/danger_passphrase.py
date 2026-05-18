"""
polyecho.shared.danger_passphrase
=================================
**FROZEN Phase-5 Wave-1 cross-language security contract.**

The operator **pre-provisions** one *danger passphrase* (CLI →
:func:`set_danger_passphrase`).  Every danger-tier confirm
(:func:`polyecho.shared.pending_actions.confirm_pending_action`) verifies
the caller-supplied passphrase against this pre-provisioned hash.  Nothing
on the web / DB-write path may create or change it — the web only ever
*verifies* against the already-provisioned value, never sets it.

Why this module exists (P0-1)
-----------------------------
Previously a pending action stored a per-row hash derived **from the
caller's own input** on first confirm, so "knowing the secret" reduced to
"knowing whatever you just typed" — no real second factor.  This module
establishes a single, operator-owned secret and a canonical hash string
that Python **and** TypeScript reproduce *byte-for-byte*, so the web (Node
``crypto.pbkdf2``) mirror needs no guessing.

Canonical passphrase-hash string format (FROZEN — mirror verbatim)
------------------------------------------------------------------
::

    pbkdf2$<iterations>$<b64_salt>$<b64_hash>

* ``pbkdf2``     — literal scheme tag (lowercase).  Exactly four
  ``$``-delimited fields; no field may contain ``$``.
* ``<iterations>`` — PBKDF2 iteration count, **decimal ASCII**, no
  leading zeros.  This is the *clamped* value
  ``max(settings.key_kdf_iterations, MIN_KDF_ITERATIONS)`` at hash time
  (so it is always ≥ :data:`polyecho.shared.config.MIN_KDF_ITERATIONS`
  = 100000).  Verification uses the iteration count **embedded in the
  string**, never the current settings — old hashes stay verifiable if
  the setting changes.
* ``<b64_salt>`` — standard Base64 (RFC 4648, ``+/`` alphabet, ``=``
  padding) of a per-hash **cryptographically-random 16-byte** salt.
* ``<b64_hash>`` — standard Base64 of the **32-byte** PBKDF2 output.

KDF parameters (FROZEN):

* Algorithm:  PBKDF2-HMAC-**SHA256**.
* Password:   the passphrase encoded as **UTF-8** bytes (NFC is *not*
  applied — the exact code-unit bytes; callers must send the same string).
* Salt:       the 16 raw bytes decoded from ``<b64_salt>``.
* Iterations: the integer parsed from ``<iterations>``.
* Derived-key length (dkLen): **32** bytes.

Node mirror (reference — the web agent implements exactly this)::

    import { pbkdf2Sync, randomBytes, timingSafeEqual } from "node:crypto";
    const SALT = 16, DK = 32;                    // bytes
    function hashDangerPassphrase(p: string, iterations: number): string {
      const salt = randomBytes(SALT);
      const dk = pbkdf2Sync(Buffer.from(p, "utf8"), salt, iterations,
                            DK, "sha256");
      return `pbkdf2$${iterations}$${salt.toString("base64")}` +
             `$${dk.toString("base64")}`;
    }
    function verifyDangerPassphrase(p: string, stored: string): boolean {
      const m = stored.split("$");
      if (m.length !== 4 || m[0] !== "pbkdf2") return false;
      const iter = Number(m[1]);
      if (!Number.isInteger(iter) || iter < 100000) return false;  // floor
      const salt = Buffer.from(m[2], "base64");
      const expected = Buffer.from(m[3], "base64");
      const cand = pbkdf2Sync(Buffer.from(p, "utf8"), salt, iter,
                              expected.length, "sha256");
      return cand.length === expected.length &&
             timingSafeEqual(cand, expected);
    }

The web mirror MUST clamp ``iterations`` to ≥ 100000 on **verify** too
(reject a downgraded stored string) — matching
:data:`polyecho.shared.config.MIN_KDF_ITERATIONS`.

Storage
-------
The hash string lives in the ``settings`` table under the single key
:data:`DANGER_PASSPHRASE_SETTINGS_KEY`.  ``settings.value`` is JSONB; we
persist the canonical string as a JSON **string scalar** (web reads it the
same way).  No salt/secret is ever logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Any

from polyecho.shared.config import MIN_KDF_ITERATIONS, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "DANGER_PASSPHRASE_SETTINGS_KEY",
    "PASSPHRASE_HASH_SCHEME",
    "PASSPHRASE_SALT_BYTES",
    "PASSPHRASE_HASH_BYTES",
    "hash_danger_passphrase",
    "verify_danger_passphrase",
    "get_danger_passphrase_hash",
    "set_danger_passphrase",
    "is_danger_passphrase_provisioned",
]

#: ``settings`` table key holding the canonical passphrase-hash string.
#: FROZEN — the web/cli/core agents reference this exact constant.
DANGER_PASSPHRASE_SETTINGS_KEY = "danger_passphrase_hash"

#: Literal scheme tag (first ``$``-field of the canonical string).
PASSPHRASE_HASH_SCHEME = "pbkdf2"

#: Per-hash random salt size, in bytes (FROZEN).
PASSPHRASE_SALT_BYTES = 16

#: PBKDF2 derived-key length, in bytes (FROZEN; SHA-256 native size).
PASSPHRASE_HASH_BYTES = 32


def _effective_iterations() -> int:
    """The clamped KDF iteration count used for **new** hashes.

    ``Settings`` already floors ``key_kdf_iterations`` at
    :data:`MIN_KDF_ITERATIONS` (P0-7), so this is ≥ 100000 by construction;
    the explicit ``max`` is belt-and-braces and documents the invariant.
    """
    return max(int(get_settings().key_kdf_iterations), MIN_KDF_ITERATIONS)


def _pbkdf2(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256(UTF-8(passphrase), salt, iterations) → 32 bytes."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        iterations,
        dklen=PASSPHRASE_HASH_BYTES,
    )


def hash_danger_passphrase(passphrase: str) -> str:
    """Hash *passphrase* into the canonical ``pbkdf2$…`` string.

    Uses a fresh 16-byte random salt and the clamped
    (``≥ MIN_KDF_ITERATIONS``) iteration count.  The plaintext is never
    stored or logged.

    Args:
        passphrase: The operator danger passphrase (non-empty).

    Returns:
        ``pbkdf2$<iter>$<b64salt>$<b64hash>`` — see the module docstring.

    Raises:
        ValueError: If *passphrase* is empty.
    """
    if not passphrase:
        raise ValueError("danger passphrase must not be empty")
    iterations = _effective_iterations()
    salt = os.urandom(PASSPHRASE_SALT_BYTES)
    digest = _pbkdf2(passphrase, salt, iterations)
    return (
        f"{PASSPHRASE_HASH_SCHEME}${iterations}"
        f"${base64.b64encode(salt).decode('ascii')}"
        f"${base64.b64encode(digest).decode('ascii')}"
    )


def verify_danger_passphrase(passphrase: str, stored: str | None) -> bool:
    """Constant-time check of *passphrase* against a canonical *stored* hash.

    Returns ``False`` (never raises) for a missing, malformed, or
    downgraded (``iterations < MIN_KDF_ITERATIONS``) stored value so callers
    map every failure to one uniform "bad passphrase" outcome and an
    attacker cannot distinguish *why* it failed.  Verification uses the
    iteration count and salt **embedded in the string** (old hashes stay
    verifiable) and :func:`hmac.compare_digest` for the final comparison.

    Args:
        passphrase: The candidate passphrase.
        stored:     A canonical ``pbkdf2$…`` string, or ``None``.

    Returns:
        ``True`` iff *passphrase* reproduces the embedded digest.
    """
    if not stored or not passphrase:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != PASSPHRASE_HASH_SCHEME:
        return False
    scheme, iter_s, salt_b64, hash_b64 = parts
    try:
        iterations = int(iter_s)
    except (ValueError, TypeError):
        return False
    # Fail-closed on a downgraded stored string (defends against a hostile
    # DB write lowering the work factor below the OWASP floor).
    if iterations < MIN_KDF_ITERATIONS:
        return False
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if not salt or len(expected) != PASSPHRASE_HASH_BYTES:
        return False
    candidate = _pbkdf2(passphrase, salt, iterations)
    return hmac.compare_digest(candidate, expected)


async def get_danger_passphrase_hash(session: Any = None) -> str | None:
    """Return the provisioned canonical hash string, or ``None`` if unset.

    Reads the single ``settings`` row keyed
    :data:`DANGER_PASSPHRASE_SETTINGS_KEY`.  Tolerates the value being
    stored either as a bare JSON string (canonical) or wrapped as
    ``{"value": "<string>"}`` (defensive — matches the proxy-setting
    pattern in :mod:`polyecho.shared.http`).  Returns ``None`` when the row
    is absent or the value is not a usable string.

    Args:
        session: Optional caller session; ``None`` → a new
                 :func:`~polyecho.shared.db.session_scope`.
    """
    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Setting

    def _coerce(value: Any) -> str | None:
        if isinstance(value, str):
            v = value.strip()
            return v or None
        if isinstance(value, dict):
            inner = value.get("value")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return None

    async def _read(sess: Any) -> str | None:
        row = (
            await sess.execute(
                select(Setting).where(
                    Setting.key == DANGER_PASSPHRASE_SETTINGS_KEY
                )
            )
        ).scalar_one_or_none()
        return _coerce(row.value) if row is not None else None

    if session is not None:
        return await _read(session)
    async with session_scope() as owned:
        return await _read(owned)


async def set_danger_passphrase(
    passphrase: str, session: Any = None
) -> None:
    """Provision / rotate the operator danger passphrase (CLI-only path).

    Hashes *passphrase* with :func:`hash_danger_passphrase` and upserts the
    canonical string into the ``settings`` row keyed
    :data:`DANGER_PASSPHRASE_SETTINGS_KEY`.  This is the **only** function
    that writes the secret; the web/DB-write confirm path must never call
    it (it would let the web self-provision — exactly the hole P0-1
    closes).  The plaintext is never logged.

    Args:
        passphrase: The new operator danger passphrase (non-empty).
        session:    Optional caller session; ``None`` → a new scope.

    Raises:
        ValueError: If *passphrase* is empty.
    """
    hashed = hash_danger_passphrase(passphrase)

    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Setting

    async def _write(sess: Any) -> None:
        row = (
            await sess.execute(
                select(Setting).where(
                    Setting.key == DANGER_PASSPHRASE_SETTINGS_KEY
                )
            )
        ).scalar_one_or_none()
        if row is None:
            sess.add(
                Setting(
                    key=DANGER_PASSPHRASE_SETTINGS_KEY, value=hashed
                )
            )
        else:
            row.value = hashed
        await sess.flush()
        logger.info(
            "danger_passphrase_provisioned",
            extra={"rotated": row is not None},
        )

    if session is not None:
        await _write(session)
        return
    async with session_scope() as owned:
        await _write(owned)


async def is_danger_passphrase_provisioned(session: Any = None) -> bool:
    """``True`` iff a usable danger-passphrase hash is provisioned.

    A thin, side-effect-free predicate over
    :func:`get_danger_passphrase_hash` — the web layer uses it to surface
    "operator must provision the danger passphrase (CLI) first" instead of
    a confusing confirm failure.

    Args:
        session: Optional caller session; ``None`` → a new scope.
    """
    return (await get_danger_passphrase_hash(session)) is not None

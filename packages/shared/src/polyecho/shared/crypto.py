"""
polyecho.shared.crypto
======================
AES-256-GCM private-key encryption / decryption helpers.

Security design
---------------
* **Key derivation** — PBKDF2-HMAC-SHA256 with a random 32-byte salt and
  a per-blob iteration count (the *clamped* ``settings.key_kdf_iterations``
  in effect at encrypt time, default 600 000 per OWASP 2023).  Both the salt
  *and* the iteration count are stored in the blob so decryption is fully
  self-contained and survives a later change to
  ``POLYECHO_KEY_KDF_ITERATIONS`` (R1 — see "Blob versioning" below).
* **Cipher** — AES-256-GCM.  The 12-byte nonce is randomly generated per
  encryption call and stored in the blob.  The 16-byte GCM authentication tag
  provides ciphertext integrity.
* **Blob versioning (R1 — iteration count must be stored)**

  The legacy v0 layout stored only the salt, so decryption had to guess the
  iteration count from the *current* env ``key_kdf_iterations``.  Changing
  ``POLYECHO_KEY_KDF_ITERATIONS`` then made every existing key blob
  permanently undecryptable.  The header now embeds the iteration count.

  .. code-block:: text

      v1 (current):
        [ 0x01 (1 B) | iterations (4 B, big-endian uint32)
          | salt (32 B) | nonce (12 B) | tag (16 B) | ciphertext (N B) ]
        The 5-byte header (version || iterations) is bound as AES-GCM
        *associated data* so it is tamper-evident (a flipped iteration
        count fails the GCM tag rather than silently mis-deriving).
        Total overhead: 65 bytes.

      v0 (legacy, no header — still decryptable):
        [ salt (32 B) | nonce (12 B) | tag (16 B) | ciphertext (N B) ]
        Total overhead: 60 bytes.

  **Back-compat heuristic (proven safe).** ``encrypt_private_key`` always
  emits v1.  ``decrypt_private_key`` inspects ``blob[0]``: a v1 blob always
  starts with the version byte ``0x01``; a v0 blob starts with the first
  byte of a 32-byte ``os.urandom`` salt.  We treat the blob as v1 **iff**
  ``blob[0] == 0x01`` *and* it is at least the v1 minimum length; otherwise
  it is decrypted as legacy v0 using ``settings.key_kdf_iterations``.  The
  only ambiguous case is a *legacy* blob whose random salt byte happens to
  be ``0x01`` (probability 1/256): for those the v1 path is attempted first,
  its GCM tag fails (the AAD/derivation differ), and we transparently fall
  back to the v0 interpretation — so every legacy blob still decrypts.  A
  fresh random salt can never collide with a *forged* v1 header because the
  header is authenticated by the GCM tag under the derived key.

* **No secrets in logs** — the plaintext, passphrase, and derived key are
  never passed to any logger.  ``structlog``'s ``_redact_secrets`` processor
  provides an additional defence layer.

Usage::

    from polyecho.shared.crypto import encrypt_private_key, decrypt_private_key

    blob: bytes = encrypt_private_key("0xdeadbeef...", passphrase="hunter2")
    key: str    = decrypt_private_key(blob, passphrase="hunter2")
    assert key == "0xdeadbeef..."
"""

from __future__ import annotations

import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from polyecho.shared.config import MIN_KDF_ITERATIONS, get_settings

# Fixed field sizes in bytes — changing these breaks existing blobs.
_SALT_SIZE: int = 32
_NONCE_SIZE: int = 12
_TAG_SIZE: int = 16   # appended automatically by AESGCM; included here for documentation.
_KEY_SIZE: int = 32   # 256-bit AES key

#: Current blob format version (first byte of a v1 blob).  A legacy v0 blob
#: has no version byte (it starts with a random salt byte instead).
_BLOB_VERSION_V1: int = 0x01
#: v1 header = 1-byte version || 4-byte big-endian uint32 iteration count.
#: The header is bound as AES-GCM associated data so a flipped iteration
#: count fails the auth tag instead of silently mis-deriving the key.
_V1_HEADER_SIZE: int = 5
_V1_MIN_SIZE: int = _V1_HEADER_SIZE + _SALT_SIZE + _NONCE_SIZE + _TAG_SIZE
_V0_MIN_SIZE: int = _SALT_SIZE + _NONCE_SIZE + _TAG_SIZE
#: Largest value the 4-byte big-endian iteration field can hold.
_MAX_ITERATIONS: int = 0xFFFFFFFF
#: DoS guard (R1 hardening): the v1 header iteration count is attacker-
#: controllable (a corrupt or forged ``accounts.encrypted_private_key``
#: blob), yet PBKDF2 must run *before* the GCM tag can be verified.  A
#: tampered header near ``_MAX_ITERATIONS`` (~4.29e9) would peg a CPU for
#: tens of minutes before ``InvalidTag`` could ever be raised.  Any header
#: count outside ``[MIN_KDF_ITERATIONS, _MAX_DECRYPT_ITERATIONS]`` is
#: rejected *before* key derivation.  10_000_000 is ~16x the OWASP
#: PBKDF2-HMAC-SHA256 guidance (600k) — far above any realistic operator
#: setting — while bounding untrusted-header KDF work to a few seconds.
_MAX_DECRYPT_ITERATIONS: int = 10_000_000


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """Derive a 256-bit AES key from *passphrase* using PBKDF2-HMAC-SHA256.

    Args:
        passphrase: The user-supplied passphrase.
        salt:       Random 32-byte salt.
        iterations: KDF iteration count (from settings).

    Returns:
        32-byte derived key suitable for AES-256.
    """
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode())


def encrypt_private_key(plaintext: str, passphrase: str) -> bytes:
    """Encrypt *plaintext* (a private key string) with AES-256-GCM.

    The passphrase is never stored; the caller is responsible for prompting
    the user and clearing it from memory as soon as possible.

    Args:
        plaintext:  The private key string to encrypt (e.g. ``"0xdeadbeef..."``).
        passphrase: The user-supplied passphrase used for key derivation.

    Returns:
        A self-contained v1 blob:
        ``0x01 || iterations(4B BE) || salt || nonce || tag || ciphertext``.
        The 5-byte header is AES-GCM associated data (tamper-evident).

    Raises:
        ValueError: If *plaintext* or *passphrase* is empty.
    """
    if not plaintext:
        raise ValueError("plaintext must not be empty")
    if not passphrase:
        raise ValueError("passphrase must not be empty")

    settings = get_settings()
    # ``Settings`` already floors this at MIN_KDF_ITERATIONS (P0-7); the
    # explicit max documents the invariant and the 4-byte field can hold it
    # (600 000 default ≪ 2**32).
    iterations = max(int(settings.key_kdf_iterations), MIN_KDF_ITERATIONS)
    if iterations > _MAX_ITERATIONS:  # pragma: no cover - absurdly high config
        raise ValueError(
            f"key_kdf_iterations {iterations} exceeds the storable maximum "
            f"{_MAX_ITERATIONS}"
        )

    salt = os.urandom(_SALT_SIZE)
    nonce = os.urandom(_NONCE_SIZE)
    key = _derive_key(passphrase, salt, iterations)

    # Header is bound as associated data so the stored iteration count is
    # authenticated: tampering with it fails the GCM tag (InvalidTag)
    # instead of silently deriving a different key.
    header = bytes([_BLOB_VERSION_V1]) + struct.pack(">I", iterations)
    aesgcm = AESGCM(key)
    # AESGCM.encrypt returns ciphertext + tag (tag appended at the end).
    ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode(), header)

    return header + salt + nonce + ct_and_tag


def _decrypt_v0(blob: bytes, passphrase: str) -> str:
    """Decrypt a legacy (header-less) v0 blob.

    v0 stored no iteration count, so we fall back to the *current*
    ``settings.key_kdf_iterations``.  This is the documented back-compat
    path: a v0 blob is only decryptable while that setting matches what it
    was encrypted with (the exact limitation R1 fixes for new v1 blobs).
    """
    settings = get_settings()
    salt = blob[:_SALT_SIZE]
    nonce = blob[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
    ct_and_tag = blob[_SALT_SIZE + _NONCE_SIZE :]
    key = _derive_key(passphrase, salt, settings.key_kdf_iterations)
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ct_and_tag, None)
    return plaintext_bytes.decode()


def _decrypt_v1(blob: bytes, passphrase: str) -> str:
    """Decrypt a v1 blob, reading the iteration count from its header.

    The 5-byte header (version || iterations) is verified as AES-GCM
    associated data, so a tampered iteration count fails the auth tag
    (``InvalidTag``) rather than silently mis-deriving the key.
    """
    header = blob[:_V1_HEADER_SIZE]
    iterations = struct.unpack(">I", blob[1:_V1_HEADER_SIZE])[0]
    # DoS guard: ``header`` (hence ``iterations``) is attacker-controllable,
    # but PBKDF2 below runs BEFORE the GCM tag is verified.  An out-of-range
    # count — a tampered/forged blob, or a legacy v0 blob whose random
    # salt[0] collided with 0x01 — must fail fast as a tamper, NOT derive a
    # key for billions of rounds.  Treat it exactly like a bad tag: raise
    # InvalidTag so ``decrypt_private_key`` keeps its uniform "bad
    # passphrase / corrupt blob" outcome and its transparent v0 fallback.
    if not MIN_KDF_ITERATIONS <= iterations <= _MAX_DECRYPT_ITERATIONS:
        raise InvalidTag
    off = _V1_HEADER_SIZE
    salt = blob[off : off + _SALT_SIZE]
    nonce = blob[off + _SALT_SIZE : off + _SALT_SIZE + _NONCE_SIZE]
    ct_and_tag = blob[off + _SALT_SIZE + _NONCE_SIZE :]
    key = _derive_key(passphrase, salt, iterations)
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ct_and_tag, header)
    return plaintext_bytes.decode()


def decrypt_private_key(blob: bytes, passphrase: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_private_key` (v1 or v0).

    R1 fix: a v1 blob carries its own iteration count in an
    AES-GCM-authenticated header, so it stays decryptable even after
    ``POLYECHO_KEY_KDF_ITERATIONS`` changes.  Legacy v0 blobs (no header)
    remain decryptable via the current setting (the original limitation).

    Version detection: a v1 blob starts with ``0x01`` and is at least the
    v1 minimum length.  A legacy v0 blob starts with a random salt byte; in
    the 1/256 case that byte is ``0x01`` the v1 parse is attempted first,
    its GCM tag fails, and we transparently fall back to v0 — so every
    legacy blob still decrypts.

    Args:
        blob:       The encrypted blob (v1 or legacy v0 layout).
        passphrase: The user-supplied passphrase.

    Returns:
        The decrypted private key string.

    Raises:
        ValueError: If *blob* is too short or *passphrase* is empty.
        cryptography.exceptions.InvalidTag: If the passphrase is wrong or
            the blob has been tampered with.
    """
    if not passphrase:
        raise ValueError("passphrase must not be empty")

    if len(blob) < _V0_MIN_SIZE:
        raise ValueError(
            f"blob is too short: expected at least {_V0_MIN_SIZE} bytes, "
            f"got {len(blob)}"
        )

    looks_v1 = blob[0] == _BLOB_VERSION_V1 and len(blob) >= _V1_MIN_SIZE
    if looks_v1:
        try:
            return _decrypt_v1(blob, passphrase)
        except InvalidTag:
            # Either a genuine wrong-passphrase/tamper on a real v1 blob, or
            # a legacy v0 blob whose random salt byte collided with 0x01.
            # Re-try as v0; if that also fails the InvalidTag propagates
            # (uniform "bad passphrase / corrupt blob" outcome).
            return _decrypt_v0(blob, passphrase)
    return _decrypt_v0(blob, passphrase)

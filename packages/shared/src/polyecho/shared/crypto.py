"""
polyecho.shared.crypto
======================
AES-256-GCM private-key encryption / decryption helpers.

Security design
---------------
* **Key derivation** — PBKDF2-HMAC-SHA256 with a random 32-byte salt and
  ``settings.key_kdf_iterations`` iterations (default 600 000, per OWASP 2023).
  The salt is stored alongside the ciphertext so decryption is self-contained.
* **Cipher** — AES-256-GCM.  The 12-byte nonce is randomly generated per
  encryption call and stored in the blob.  The 16-byte GCM authentication tag
  provides ciphertext integrity.
* **Blob layout** (all big-endian lengths are implicit from fixed field sizes):

  .. code-block:: text

      [ salt (32 B) | nonce (12 B) | tag (16 B) | ciphertext (N B) ]

  Total overhead: 60 bytes.

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

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from polyecho.shared.config import get_settings

# Fixed field sizes in bytes — changing these breaks existing blobs.
_SALT_SIZE: int = 32
_NONCE_SIZE: int = 12
_TAG_SIZE: int = 16   # appended automatically by AESGCM; included here for documentation.
_KEY_SIZE: int = 32   # 256-bit AES key


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
        A self-contained blob: ``salt || nonce || tag || ciphertext``.

    Raises:
        ValueError: If *plaintext* or *passphrase* is empty.
    """
    if not plaintext:
        raise ValueError("plaintext must not be empty")
    if not passphrase:
        raise ValueError("passphrase must not be empty")

    settings = get_settings()
    salt = os.urandom(_SALT_SIZE)
    nonce = os.urandom(_NONCE_SIZE)
    key = _derive_key(passphrase, salt, settings.key_kdf_iterations)

    aesgcm = AESGCM(key)
    # AESGCM.encrypt returns ciphertext + tag (tag appended at the end).
    ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode(), None)

    return salt + nonce + ct_and_tag


def decrypt_private_key(blob: bytes, passphrase: str) -> str:
    """Decrypt a blob produced by :func:`encrypt_private_key`.

    Args:
        blob:       The encrypted blob (``salt || nonce || tag || ciphertext``).
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

    min_size = _SALT_SIZE + _NONCE_SIZE + _TAG_SIZE
    if len(blob) < min_size:
        raise ValueError(
            f"blob is too short: expected at least {min_size} bytes, got {len(blob)}"
        )

    settings = get_settings()
    salt = blob[:_SALT_SIZE]
    nonce = blob[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
    ct_and_tag = blob[_SALT_SIZE + _NONCE_SIZE :]

    key = _derive_key(passphrase, salt, settings.key_kdf_iterations)
    aesgcm = AESGCM(key)
    # AESGCM.decrypt verifies the GCM tag and raises InvalidTag on failure.
    plaintext_bytes = aesgcm.decrypt(nonce, ct_and_tag, None)
    return plaintext_bytes.decode()

"""
tests/test_vapid.py
===================
Phase-5 Wave-1 — :mod:`polyecho.shared.vapid` binding contract.

Covers: keypair round-trip (public decodes to a 65-byte uncompressed
P-256 point; private re-parses via ``Vapid01.from_string`` to the same
scalar); unpadded URL-safe encoding; ``application_server_key``
normalisation/validation; and the ``build_vapid_claims`` shape
(``sub`` only — pywebpush fills ``aud``/``exp``).
"""

from __future__ import annotations

import base64

import pytest
from py_vapid import Vapid01, b64urldecode

from polyecho.shared.vapid import (
    application_server_key,
    build_vapid_claims,
    generate_vapid_keypair,
)


def _has_no_padding(s: str) -> bool:
    return "=" not in s


def _is_urlsafe(s: str) -> bool:
    # URL-safe base64 alphabet never contains '+' or '/'.
    return "+" not in s and "/" not in s


def test_generate_keypair_public_is_65_byte_uncompressed_point() -> None:
    pub, priv = generate_vapid_keypair()
    assert _has_no_padding(pub) and _is_urlsafe(pub)
    assert _has_no_padding(priv) and _is_urlsafe(priv)

    raw_pub = b64urldecode(pub.encode())
    assert len(raw_pub) == 65  # 0x04 ‖ 32-byte X ‖ 32-byte Y
    assert raw_pub[0] == 0x04  # uncompressed point prefix

    raw_priv = b64urldecode(priv.encode())
    assert len(raw_priv) == 32  # raw P-256 private scalar


def test_private_key_roundtrips_via_vapid_from_string() -> None:
    """pywebpush calls ``Vapid01.from_string`` on the private string we emit."""
    _pub, priv = generate_vapid_keypair()
    v = Vapid01.from_string(priv)  # 32-byte → from_raw path
    # Re-export the reconstructed key and confirm it matches.
    reexported = v.private_key.private_numbers().private_value.to_bytes(
        32, "big"
    )
    assert reexported == b64urldecode(priv.encode())


def test_public_private_keypair_are_paired() -> None:
    pub, priv = generate_vapid_keypair()
    v = Vapid01.from_string(priv)
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    derived_pub = v.public_key.public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    assert derived_pub == b64urldecode(pub.encode())


def test_keypairs_are_unique() -> None:
    a, _ = generate_vapid_keypair()
    b, _ = generate_vapid_keypair()
    assert a != b


def test_application_server_key_is_idempotent_identity() -> None:
    pub, _ = generate_vapid_keypair()
    assert application_server_key(pub) == pub
    # Tolerates a standard-base64 / padded input and normalises it.
    raw = b64urldecode(pub.encode())
    padded_std = base64.b64encode(raw).decode()  # '+' '/' '=' alphabet
    assert application_server_key(padded_std) == pub


def test_application_server_key_rejects_non_point() -> None:
    bad = base64.urlsafe_b64encode(b"\x04" + b"\x00" * 10).rstrip(b"=").decode()
    with pytest.raises(ValueError, match="uncompressed EC P-256 point"):
        application_server_key(bad)


def test_build_vapid_claims_sub_only_default_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYECHO_VAPID_SUBJECT", "mailto:ops@example.com")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()
    claims = build_vapid_claims()
    # ONLY sub — pywebpush auto-fills aud (endpoint) and exp (now+12h).
    assert claims == {"sub": "mailto:ops@example.com"}
    assert "aud" not in claims and "exp" not in claims


def test_build_vapid_claims_explicit_subject_override() -> None:
    assert build_vapid_claims("https://example.com/contact") == {
        "sub": "https://example.com/contact"
    }

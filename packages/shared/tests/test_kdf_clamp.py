"""
tests/test_kdf_clamp.py
=======================
P0-7 — ``Settings.key_kdf_iterations`` is clamped UP to
:data:`polyecho.shared.config.MIN_KDF_ITERATIONS` (the OWASP floor).

This is the single chokepoint every key-derivation reader goes through
(``crypto._derive_key``, the danger-passphrase hash), so a too-low value
(typo / hostile env) can never weaken derivation.
"""

from __future__ import annotations

import pytest

from polyecho.shared.config import MIN_KDF_ITERATIONS, Settings, get_settings


def test_floor_constant_is_owasp_minimum() -> None:
    assert MIN_KDF_ITERATIONS == 100_000


@pytest.mark.parametrize("low", [0, 1, 1_000, 99_999])
def test_below_floor_is_raised_to_floor(
    monkeypatch: pytest.MonkeyPatch, low: int
) -> None:
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", str(low))
    get_settings.cache_clear()
    assert get_settings().key_kdf_iterations == MIN_KDF_ITERATIONS


@pytest.mark.parametrize("ok", [100_000, 100_001, 600_000, 1_000_000])
def test_at_or_above_floor_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, ok: int
) -> None:
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", str(ok))
    get_settings.cache_clear()
    assert get_settings().key_kdf_iterations == ok


def test_default_is_well_above_floor() -> None:
    s = Settings()
    assert s.key_kdf_iterations == 600_000
    assert s.key_kdf_iterations >= MIN_KDF_ITERATIONS


def test_clamp_applies_to_direct_construction() -> None:
    """The validator runs on every load path, not just env parsing."""
    assert Settings(key_kdf_iterations=5).key_kdf_iterations == (
        MIN_KDF_ITERATIONS
    )


def test_crypto_round_trip_uses_clamped_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob encrypted while the env requested a sub-floor count still
    decrypts — proving the clamped value is what crypto actually used."""
    from polyecho.shared.crypto import (
        decrypt_private_key,
        encrypt_private_key,
    )

    monkeypatch.setenv(
        "POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test"
    )
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "10")
    get_settings.cache_clear()
    blob = encrypt_private_key("0x" + "ab" * 32, "pw")
    assert decrypt_private_key(blob, "pw") == "0x" + "ab" * 32

"""
tests/test_crypto_config.py
===========================
Deep coverage for :mod:`polyecho.shared.crypto` and
:mod:`polyecho.shared.config` beyond the smoke tests in
``test_imports.py``.

crypto
------
* Round-trip across *different* KDF iteration counts.
* Wrong passphrase / tampered blob (salt, nonce, ciphertext, tag) all
  raise ``InvalidTag`` — never silently return garbage.
* Blob layout: salt/nonce are random per call; declared overhead is exact.
* Unicode passphrase + plaintext round-trips.
* Boundary: a blob of exactly the minimum length is rejected (no ciphertext).

config
------
* ``POLYECHO_`` env precedence over defaults; wrong prefix ignored.
* ``datasource`` Literal accepts fixture/live and rejects junk.
* ``SecretStr`` fields never leak the secret via ``repr``/``str``.
* ``get_settings`` is a per-process singleton until ``cache_clear``.

All tests are deterministic and order-independent: the autouse
``_clear_settings_cache`` fixture (see ``conftest.py``) resets the
``get_settings`` cache around every test, and KDF iterations are pinned
low via env so crypto stays fast.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from polyecho.shared.config import Settings, get_settings
from polyecho.shared.crypto import (
    _NONCE_SIZE,
    _SALT_SIZE,
    _TAG_SIZE,
    decrypt_private_key,
    encrypt_private_key,
)

# A realistic 32-byte (64-hex) secp256k1 private key string.
_PK = "0x" + "ab" * 32
_PASSPHRASE = "correct horse battery staple"


def _fast_env(monkeypatch: pytest.MonkeyPatch, iterations: int = 1000) -> None:
    monkeypatch.setenv("POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test")
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", str(iterations))


# ---------------------------------------------------------------------------
# crypto — round-trip & parameter independence
# ---------------------------------------------------------------------------


class TestCryptoRoundTrip:
    def test_round_trip_default_iterations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        blob = encrypt_private_key(_PK, _PASSPHRASE)
        assert decrypt_private_key(blob, _PASSPHRASE) == _PK

    @pytest.mark.parametrize("iterations", [1, 1000, 50_000])
    def test_round_trip_across_iteration_counts(
        self, monkeypatch: pytest.MonkeyPatch, iterations: int
    ) -> None:
        """Encrypt+decrypt must succeed for any iteration count, and a blob
        encrypted under one count must decrypt under the *same* count."""
        _fast_env(monkeypatch, iterations)
        get_settings.cache_clear()
        blob = encrypt_private_key(_PK, _PASSPHRASE)
        assert decrypt_private_key(blob, _PASSPHRASE) == _PK

    def test_blob_encrypted_under_different_iterations_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The KDF count is *not* stored in the blob; decrypting with a
        different ``key_kdf_iterations`` derives a different key → InvalidTag.
        This documents the (intentional) coupling and guards against a
        silent-success regression."""
        _fast_env(monkeypatch, 1000)
        get_settings.cache_clear()
        blob = encrypt_private_key(_PK, _PASSPHRASE)

        monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "2000")
        get_settings.cache_clear()
        with pytest.raises(InvalidTag):
            decrypt_private_key(blob, _PASSPHRASE)

    def test_unicode_passphrase_and_plaintext(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        secret = "0xnon-ascii-你好-\U0001f510"
        passphrase = "pässwörd-验证"  # noqa: RUF001
        blob = encrypt_private_key(secret, passphrase)
        assert decrypt_private_key(blob, passphrase) == secret


# ---------------------------------------------------------------------------
# crypto — blob layout & overhead
# ---------------------------------------------------------------------------


class TestCryptoBlobLayout:
    def test_salt_and_nonce_random_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        b1 = encrypt_private_key(_PK, _PASSPHRASE)
        b2 = encrypt_private_key(_PK, _PASSPHRASE)
        assert b1[:_SALT_SIZE] != b2[:_SALT_SIZE], "salt must be random"
        n1 = b1[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
        n2 = b2[_SALT_SIZE : _SALT_SIZE + _NONCE_SIZE]
        assert n1 != n2, "nonce must be random"

    def test_overhead_is_exactly_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        plaintext = "A" * 37
        blob = encrypt_private_key(plaintext, _PASSPHRASE)
        overhead = _SALT_SIZE + _NONCE_SIZE + _TAG_SIZE  # 60
        # AES-GCM is a stream cipher → ciphertext length == plaintext length.
        assert len(blob) == overhead + len(plaintext.encode())

    def test_minimum_length_blob_without_ciphertext_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blob of exactly salt+nonce+tag has zero ciphertext. The length
        guard uses ``<`` so this passes the size check but must still fail
        authentication rather than return ``""``."""
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        bogus = b"\x00" * (_SALT_SIZE + _NONCE_SIZE + _TAG_SIZE)
        with pytest.raises(InvalidTag):
            decrypt_private_key(bogus, _PASSPHRASE)


# ---------------------------------------------------------------------------
# crypto — tamper detection (GCM integrity)
# ---------------------------------------------------------------------------


class TestCryptoTamperDetection:
    @pytest.fixture
    def blob(self, monkeypatch: pytest.MonkeyPatch) -> bytes:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        return encrypt_private_key(_PK, _PASSPHRASE)

    def test_wrong_passphrase_raises(self, blob: bytes) -> None:
        with pytest.raises(InvalidTag):
            decrypt_private_key(blob, "not the passphrase")

    def test_flipped_ciphertext_byte_raises(self, blob: bytes) -> None:
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01  # flip a ciphertext bit
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_flipped_tag_byte_raises(self, blob: bytes) -> None:
        # GCM tag = last 16 bytes of (ciphertext||tag) region.
        tampered = bytearray(blob)
        tag_start = _SALT_SIZE + _NONCE_SIZE + (len(blob) - _SALT_SIZE - _NONCE_SIZE - _TAG_SIZE)
        tampered[tag_start] ^= 0x01
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_corrupted_salt_raises(self, blob: bytes) -> None:
        tampered = bytearray(blob)
        tampered[0] ^= 0xFF  # wrong salt → wrong derived key
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_corrupted_nonce_raises(self, blob: bytes) -> None:
        tampered = bytearray(blob)
        tampered[_SALT_SIZE] ^= 0xFF  # wrong nonce → auth fails
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_truncated_blob_raises_value_error(self, blob: bytes) -> None:
        with pytest.raises(ValueError, match="too short"):
            decrypt_private_key(blob[:30], _PASSPHRASE)


# ---------------------------------------------------------------------------
# config — env precedence, defaults, datasource switch
# ---------------------------------------------------------------------------


class TestConfigEnv:
    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYECHO_WEB_PORT", "8123")
        monkeypatch.setenv("POLYECHO_POLYMARKET_CHAIN_ID", "80002")
        get_settings.cache_clear()
        s = get_settings()
        assert s.web_port == 8123
        assert s.polymarket_chain_id == 80002

    def test_unprefixed_env_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_PORT", "9999")  # missing POLYECHO_ prefix
        monkeypatch.setenv("DATASOURCE", "live")
        get_settings.cache_clear()
        s = get_settings()
        assert s.web_port == 3000
        assert s.datasource == "fixture"

    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "POLYECHO_WEB_PORT",
            "POLYECHO_DATASOURCE",
            "POLYECHO_LOG_LEVEL",
            "POLYECHO_TZ",
            "POLYECHO_KEY_KDF_ITERATIONS",
        ):
            monkeypatch.delenv(var, raising=False)
        get_settings.cache_clear()
        s = get_settings()
        assert s.web_port == 3000
        assert s.datasource == "fixture"
        assert s.log_level == "INFO"
        assert s.tz == "Asia/Taipei"
        assert s.key_kdf_iterations == 600_000
        assert s.polymarket_data_api_url == "https://data-api.polymarket.com"

    def test_case_insensitive_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``case_sensitive=False`` → lowercase env var is honoured."""
        monkeypatch.setenv("polyecho_web_port", "7654")
        get_settings.cache_clear()
        assert get_settings().web_port == 7654


class TestConfigDatasourceSwitch:
    @pytest.mark.parametrize("value", ["fixture", "live"])
    def test_valid_datasource_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("POLYECHO_DATASOURCE", value)
        get_settings.cache_clear()
        assert get_settings().datasource == value

    def test_invalid_datasource_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYECHO_DATASOURCE", "kafka")
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()

    @pytest.mark.parametrize(
        "level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    )
    def test_valid_log_levels(
        self, monkeypatch: pytest.MonkeyPatch, level: str
    ) -> None:
        monkeypatch.setenv("POLYECHO_LOG_LEVEL", level)
        get_settings.cache_clear()
        assert get_settings().log_level == level

    def test_invalid_log_level_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYECHO_LOG_LEVEL", "TRACE")
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()


class TestConfigSecrets:
    def test_secretstr_not_leaked_in_repr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYECHO_VAPID_PRIVATE_KEY", "super-secret-vapid")
        get_settings.cache_clear()
        s = get_settings()
        assert s.vapid_private_key.get_secret_value() == "super-secret-vapid"
        assert "super-secret-vapid" not in repr(s)
        assert "super-secret-vapid" not in str(s)
        assert "super-secret-vapid" not in str(s.model_dump())

    def test_get_settings_is_cached_singleton(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYECHO_WEB_PORT", "4001")
        get_settings.cache_clear()
        first = get_settings()
        # Mutating env without clearing the cache must NOT change the result.
        monkeypatch.setenv("POLYECHO_WEB_PORT", "4002")
        second = get_settings()
        assert first is second
        assert second.web_port == 4001
        get_settings.cache_clear()
        assert get_settings().web_port == 4002

    def test_settings_constructible_directly(self) -> None:
        """Direct ``Settings()`` (no env) yields documented defaults — used
        by ``get_datasource(settings=...)`` callers in tests."""
        s = Settings()
        assert s.datasource in {"fixture", "live"}
        assert s.polymarket_clob_url.startswith("https://")

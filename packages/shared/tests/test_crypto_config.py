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
    _BLOB_VERSION_V1,
    _NONCE_SIZE,
    _SALT_SIZE,
    _TAG_SIZE,
    _V1_HEADER_SIZE,
    decrypt_private_key,
    encrypt_private_key,
)

# v1 (current) blobs prepend a 5-byte header (version || iter); the salt
# therefore starts at offset _V1_HEADER_SIZE, not 0.
_V1_SALT_OFF = _V1_HEADER_SIZE
_V1_NONCE_OFF = _V1_HEADER_SIZE + _SALT_SIZE

# A realistic 32-byte (64-hex) secp256k1 private key string.
_PK = "0x" + "ab" * 32
_PASSPHRASE = "correct horse battery staple"


# P0-7: Settings clamps key_kdf_iterations UP to MIN_KDF_ITERATIONS
# (100_000).  Crypto tests therefore cannot run "fast" below the floor any
# more — every effective count is >= 100_000.  These tests stay correct
# (round-trip / tamper detection are iteration-count agnostic); only the
# explicit "different iterations" coupling test changes (see below).
def _fast_env(
    monkeypatch: pytest.MonkeyPatch, iterations: int = 100_000
) -> None:
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

    @pytest.mark.parametrize("iterations", [100_000, 150_000, 600_000])
    def test_round_trip_across_iteration_counts(
        self, monkeypatch: pytest.MonkeyPatch, iterations: int
    ) -> None:
        """Encrypt+decrypt must succeed for any (clamped) iteration count,
        and a blob encrypted under one count must decrypt under the *same*
        count.  Values below the floor are covered by the dedicated KDF
        clamp test; here we use floor-and-above values."""
        _fast_env(monkeypatch, iterations)
        get_settings.cache_clear()
        blob = encrypt_private_key(_PK, _PASSPHRASE)
        assert decrypt_private_key(blob, _PASSPHRASE) == _PK

    def test_blob_survives_iteration_count_change(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R1 FIX: the iteration count is now stored in the v1 blob header,
        so changing ``POLYECHO_KEY_KDF_ITERATIONS`` after encryption must
        STILL decrypt the existing blob (previously this raised InvalidTag
        and made every existing key permanently undecryptable)."""
        _fast_env(monkeypatch, 100_000)
        get_settings.cache_clear()
        blob = encrypt_private_key(_PK, _PASSPHRASE)
        # v1 header: first byte is the version tag.
        assert blob[0] == _BLOB_VERSION_V1

        # Change the env iteration count well above the floor (so it does
        # NOT collapse back to 100_000) — the blob must still decrypt
        # because its own header carries the original count.
        monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "350000")
        get_settings.cache_clear()
        assert decrypt_private_key(blob, _PASSPHRASE) == _PK

    def test_tampered_iteration_header_fails_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 5-byte header is bound as AES-GCM associated data, so
        flipping the stored iteration count fails the auth tag rather than
        silently mis-deriving the key (tamper-evident)."""
        _fast_env(monkeypatch, 100_000)
        get_settings.cache_clear()
        blob = bytearray(encrypt_private_key(_PK, _PASSPHRASE))
        blob[1] ^= 0x7F  # corrupt the big-endian iteration field
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(blob), _PASSPHRASE)

    def test_legacy_v0_blob_still_decrypts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-compat: a header-less legacy v0 blob
        (``salt||nonce||tag||ct``) must still decrypt via the current
        ``key_kdf_iterations`` fallback, including the 1/256 case where the
        random salt's first byte collides with the v1 version tag 0x01."""
        import os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from polyecho.shared.crypto import _derive_key

        _fast_env(monkeypatch, 100_000)
        get_settings.cache_clear()
        iters = get_settings().key_kdf_iterations

        def _make_v0(first_salt_byte: int) -> bytes:
            salt = bytes([first_salt_byte]) + os.urandom(_SALT_SIZE - 1)
            nonce = os.urandom(_NONCE_SIZE)
            key = _derive_key(_PASSPHRASE, salt, iters)
            ct = AESGCM(key).encrypt(nonce, _PK.encode(), None)  # no AAD (v0)
            return salt + nonce + ct

        # Ordinary legacy blob.
        assert decrypt_private_key(_make_v0(0xAB), _PASSPHRASE) == _PK
        # Ambiguous legacy blob: salt[0] == 0x01 looks like a v1 header; the
        # v1 parse is attempted, its tag fails, we transparently fall back.
        assert (
            decrypt_private_key(_make_v0(_BLOB_VERSION_V1), _PASSPHRASE)
            == _PK
        )

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
        s1 = b1[_V1_SALT_OFF : _V1_SALT_OFF + _SALT_SIZE]
        s2 = b2[_V1_SALT_OFF : _V1_SALT_OFF + _SALT_SIZE]
        assert s1 != s2, "salt must be random"
        n1 = b1[_V1_NONCE_OFF : _V1_NONCE_OFF + _NONCE_SIZE]
        n2 = b2[_V1_NONCE_OFF : _V1_NONCE_OFF + _NONCE_SIZE]
        assert n1 != n2, "nonce must be random"

    def test_overhead_is_exactly_declared(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fast_env(monkeypatch)
        get_settings.cache_clear()
        plaintext = "A" * 37
        blob = encrypt_private_key(plaintext, _PASSPHRASE)
        # v1 overhead = 5-byte header + salt + nonce + tag = 65.
        overhead = _V1_HEADER_SIZE + _SALT_SIZE + _NONCE_SIZE + _TAG_SIZE
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
        # GCM tag = last 16 bytes of the blob (after the v1 header region).
        tampered = bytearray(blob)
        tag_start = len(blob) - _TAG_SIZE
        tampered[tag_start] ^= 0x01
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_corrupted_salt_raises(self, blob: bytes) -> None:
        tampered = bytearray(blob)
        # Corrupt the salt (offset shifted past the 5-byte v1 header) →
        # wrong derived key → v1 tag fails, v0 fallback also fails.
        tampered[_V1_SALT_OFF] ^= 0xFF
        with pytest.raises(InvalidTag):
            decrypt_private_key(bytes(tampered), _PASSPHRASE)

    def test_corrupted_nonce_raises(self, blob: bytes) -> None:
        tampered = bytearray(blob)
        tampered[_V1_NONCE_OFF] ^= 0xFF  # wrong nonce → auth fails
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
        # P1-3: in live mode the localhost default DB URL is rejected, so
        # provide an explicit (non-localhost-sentinel) DSN for this branch.
        if value == "live":
            monkeypatch.setenv(
                "POLYECHO_DB_URL",
                "postgresql+asyncpg://u:p@db.example.com:5432/polyecho",
            )
            monkeypatch.setenv(
                "POLYECHO_DB_URL_DRIZZLE",
                "postgresql://u:p@db.example.com:5432/polyecho",
            )
        get_settings.cache_clear()
        assert get_settings().datasource == value

    def test_invalid_datasource_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("POLYECHO_DATASOURCE", "kafka")
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()

    def test_live_mode_requires_explicit_db_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-3 fail-fast: live mode + localhost-default DB URL → RuntimeError."""
        monkeypatch.setenv("POLYECHO_DATASOURCE", "live")
        monkeypatch.delenv("POLYECHO_DB_URL", raising=False)
        monkeypatch.delenv("POLYECHO_DB_URL_DRIZZLE", raising=False)
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="POLYECHO_DB_URL is required"):
            get_settings()

    def test_live_mode_requires_explicit_drizzle_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-3 fail-fast: drizzle-side URL has the same fail-closed contract."""
        monkeypatch.setenv("POLYECHO_DATASOURCE", "live")
        monkeypatch.setenv(
            "POLYECHO_DB_URL",
            "postgresql+asyncpg://u:p@db.example.com:5432/polyecho",
        )
        monkeypatch.delenv("POLYECHO_DB_URL_DRIZZLE", raising=False)
        get_settings.cache_clear()
        with pytest.raises(
            RuntimeError, match="POLYECHO_DB_URL_DRIZZLE is required"
        ):
            get_settings()

    def test_fixture_mode_allows_localhost_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In fixture mode the bundled localhost DSN is the explicit default
        (test-friendly opt-in path) — must NOT raise."""
        monkeypatch.setenv("POLYECHO_DATASOURCE", "fixture")
        monkeypatch.delenv("POLYECHO_DB_URL", raising=False)
        monkeypatch.delenv("POLYECHO_DB_URL_DRIZZLE", raising=False)
        get_settings.cache_clear()
        s = get_settings()
        assert s.datasource == "fixture"
        assert "localhost" in s.db_url.get_secret_value()

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

    def test_vapid_subject_default_is_not_personal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R13 (privacy): the shipped ``vapid_subject`` default must be a
        non-personal placeholder — never a real personal address that would
        be sent in every outbound VAPID JWT when unset."""
        monkeypatch.delenv("POLYECHO_VAPID_SUBJECT", raising=False)
        get_settings.cache_clear()
        # Assert the SHIPPED code default in true isolation.  model_config
        # sets ``env_file=".env"``, so plain ``get_settings()`` would read
        # the operator's local ``.env`` — which ``monkeypatch.delenv``
        # cannot scrub (it only clears os.environ, not the .env file).
        # ``_env_file=None`` bypasses any .env so this asserts the code
        # default, not a deployer's private config (otherwise: false PASS
        # in CI where no .env exists, false FAIL on a configured host).
        # ``_env_file`` is a pydantic-settings *runtime* init kwarg; the
        # pydantic mypy plugin synthesises ``__init__`` from declared fields
        # only, so it is absent there — the ignore is required, not laziness.
        subject = Settings(_env_file=None).vapid_subject  # type: ignore[call-arg]
        assert "gmail.com" not in subject
        assert "yslinear" not in subject
        assert subject == "mailto:admin@example.com"

    def test_vapid_subject_env_override_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "POLYECHO_VAPID_SUBJECT", "mailto:ops@operator.example"
        )
        get_settings.cache_clear()
        assert (
            get_settings().vapid_subject == "mailto:ops@operator.example"
        )

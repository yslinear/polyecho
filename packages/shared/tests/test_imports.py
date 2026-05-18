"""
tests/test_imports.py
=====================
Smoke tests for ``polyecho-shared``:

1. Every ORM model can be imported and references ``Base``.
2. ``get_settings()`` works with monkeypatched environment variables.
3. ``encrypt_private_key / decrypt_private_key`` round-trips correctly.
4. Wrong passphrase raises ``cryptography.exceptions.InvalidTag``.

These tests do NOT require a live Postgres connection — they only exercise
in-process Python logic.
"""

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _set_minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the minimum env vars so Settings() does not raise."""
    monkeypatch.setenv("POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test")
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "1000")  # fast for tests


# ---------------------------------------------------------------------------
# 1. Model imports
# ---------------------------------------------------------------------------


class TestModelImports:
    def test_all_models_importable(self) -> None:
        from polyecho.shared.db import Base
        from polyecho.shared.models import (
            Account,
            BacktestJob,
            CandidateLeader,
            CandidateScoreHistory,
            CopyTemplate,
            Event,
            FollowRelation,
            LeaderTrade,
            MyTrade,
            PendingAction,
            PushSubscription,
            Setting,
        )

        for model_cls in (
            Account,
            BacktestJob,
            CandidateLeader,
            CandidateScoreHistory,
            CopyTemplate,
            Event,
            FollowRelation,
            LeaderTrade,
            MyTrade,
            PendingAction,
            PushSubscription,
            Setting,
        ):
            assert issubclass(model_cls, Base), (
                f"{model_cls.__name__} does not inherit from Base"
            )
            assert hasattr(model_cls, "__tablename__"), (
                f"{model_cls.__name__} missing __tablename__"
            )

    def test_models_have_expected_tablenames(self) -> None:
        from polyecho.shared.models import (
            Account,
            BacktestJob,
            CandidateLeader,
            CandidateScoreHistory,
            CopyTemplate,
            Event,
            FollowRelation,
            LeaderTrade,
            MyTrade,
            PendingAction,
            PushSubscription,
            Setting,
        )

        expected = {
            Account: "accounts",
            BacktestJob: "backtest_jobs",
            CandidateLeader: "candidate_leaders",
            CandidateScoreHistory: "candidate_score_history",
            CopyTemplate: "copy_templates",
            Event: "events",
            FollowRelation: "follow_relations",
            LeaderTrade: "leader_trades",
            MyTrade: "my_trades",
            PendingAction: "pending_actions",
            PushSubscription: "push_subscriptions",
            Setting: "settings",
        }
        for cls, tablename in expected.items():
            assert cls.__tablename__ == tablename, (
                f"Expected {cls.__name__}.__tablename__ == {tablename!r}, "
                f"got {cls.__tablename__!r}"
            )


# ---------------------------------------------------------------------------
# 2. Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_get_settings_with_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimal_env(monkeypatch)

        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        # db_url / db_url_drizzle are SecretStr (DSN holds the password):
        # the cleartext is only available via .get_secret_value(), and the
        # secret must never appear in repr()/str() (never-logged contract).
        assert (
            settings.db_url.get_secret_value()
            == "postgresql+asyncpg://u:p@localhost/test"
        )
        assert "u:p@localhost" not in repr(settings.db_url)
        assert "u:p@localhost" not in str(settings.db_url)
        assert "u:p@localhost" not in repr(settings)
        # P0-7: env asks for 1000 but Settings clamps UP to the OWASP
        # floor MIN_KDF_ITERATIONS (100_000) — a too-low value is never
        # honoured for key derivation.
        assert settings.key_kdf_iterations == 100_000
        assert settings.log_level == "INFO"

    def test_settings_default_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_minimal_env(monkeypatch)

        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        s = get_settings()

        assert s.polymarket_clob_url == "https://clob.polymarket.com"
        assert s.polymarket_chain_id == 137
        assert s.web_port == 3000
        assert s.tz == "Asia/Taipei"

    def test_settings_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env var without the POLYECHO_ prefix must NOT be picked up."""
        monkeypatch.setenv("POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test")
        monkeypatch.setenv("WEB_PORT", "9999")  # wrong prefix — should be ignored
        monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "1000")

        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        assert s.web_port == 3000  # default, not 9999

    def teardown_method(self) -> None:
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 3. Crypto round-trip
# ---------------------------------------------------------------------------


class TestCrypto:
    """Tests for AES-256-GCM private-key encryption."""

    def setup_method(self) -> None:
        # Patch env so get_settings() works with low iteration count.
        os.environ["POLYECHO_DB_URL"] = "postgresql+asyncpg://u:p@localhost/test"
        os.environ["POLYECHO_KEY_KDF_ITERATIONS"] = "1000"

        from polyecho.shared.config import get_settings

        get_settings.cache_clear()

    def teardown_method(self) -> None:
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        os.environ.pop("POLYECHO_DB_URL", None)
        os.environ.pop("POLYECHO_KEY_KDF_ITERATIONS", None)

    def test_round_trip(self) -> None:
        from polyecho.shared.crypto import decrypt_private_key, encrypt_private_key

        plaintext = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        passphrase = "correct horse battery staple"

        blob = encrypt_private_key(plaintext, passphrase)
        assert isinstance(blob, bytes)
        # Minimum blob size: 32 (salt) + 12 (nonce) + 16 (tag) = 60 bytes
        assert len(blob) >= 60

        recovered = decrypt_private_key(blob, passphrase)
        assert recovered == plaintext

    def test_different_passphrases_produce_different_blobs(self) -> None:
        from polyecho.shared.crypto import encrypt_private_key

        pk = "0xabcdef"
        blob1 = encrypt_private_key(pk, "passphrase1")
        blob2 = encrypt_private_key(pk, "passphrase1")
        # Random salt + nonce mean each encryption is unique
        assert blob1 != blob2

    def test_wrong_passphrase_raises(self) -> None:
        from polyecho.shared.crypto import decrypt_private_key, encrypt_private_key

        blob = encrypt_private_key("0xsecret", "correct")
        with pytest.raises(InvalidTag):
            decrypt_private_key(blob, "wrong_passphrase")

    def test_empty_plaintext_raises(self) -> None:
        from polyecho.shared.crypto import encrypt_private_key

        with pytest.raises(ValueError, match="plaintext must not be empty"):
            encrypt_private_key("", "passphrase")

    def test_empty_passphrase_raises(self) -> None:
        from polyecho.shared.crypto import encrypt_private_key

        with pytest.raises(ValueError, match="passphrase must not be empty"):
            encrypt_private_key("0xsecret", "")

    def test_blob_too_short_raises(self) -> None:
        from polyecho.shared.crypto import decrypt_private_key

        with pytest.raises(ValueError, match="too short"):
            decrypt_private_key(b"\x00" * 10, "passphrase")

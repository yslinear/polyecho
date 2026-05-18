"""
tests/test_danger_passphrase.py
===============================
P0-1 — the FROZEN cross-language danger-passphrase contract:
:mod:`polyecho.shared.danger_passphrase`.

Covers the canonical hash-string format, hash/verify round-trip, the
constant-time path, wrong-passphrase + downgrade + malformed rejection,
and the async get/set/is-provisioned helpers against an in-memory
``settings`` double (no Postgres).  ``test_format_is_node_reproducible``
recomputes the digest with a standalone ``hashlib`` call exactly as the
documented Node ``crypto.pbkdf2`` mirror would, proving the string is
reproducible byte-for-byte from the published spec alone.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any
from unittest.mock import patch

import pytest

from polyecho.shared.config import MIN_KDF_ITERATIONS, get_settings
from polyecho.shared.danger_passphrase import (
    DANGER_PASSPHRASE_SETTINGS_KEY,
    PASSPHRASE_HASH_BYTES,
    PASSPHRASE_HASH_SCHEME,
    PASSPHRASE_SALT_BYTES,
    get_danger_passphrase_hash,
    hash_danger_passphrase,
    is_danger_passphrase_provisioned,
    set_danger_passphrase,
    verify_danger_passphrase,
)

from .conftest import run

_PW = "correct horse battery staple"


# ---------------------------------------------------------------------------
# canonical format
# ---------------------------------------------------------------------------
def test_canonical_format_fields() -> None:
    s = hash_danger_passphrase(_PW)
    parts = s.split("$")
    assert len(parts) == 4
    scheme, iter_s, salt_b64, hash_b64 = parts
    assert scheme == PASSPHRASE_HASH_SCHEME == "pbkdf2"
    iters = int(iter_s)
    assert iters >= MIN_KDF_ITERATIONS
    assert str(iters) == iter_s  # decimal, no leading zeros / padding
    salt = base64.b64decode(salt_b64, validate=True)
    digest = base64.b64decode(hash_b64, validate=True)
    assert len(salt) == PASSPHRASE_SALT_BYTES == 16
    assert len(digest) == PASSPHRASE_HASH_BYTES == 32


def test_salt_is_random_per_hash() -> None:
    assert hash_danger_passphrase(_PW) != hash_danger_passphrase(_PW)


def test_format_is_node_reproducible() -> None:
    """Recompute the digest from the string's own salt+iter exactly as the
    documented Node mirror (``pbkdf2(utf8(pw), salt, iter, 32, sha256)``)
    would — the contract is reproducible from the spec alone."""
    s = hash_danger_passphrase(_PW)
    _, iter_s, salt_b64, hash_b64 = s.split("$")
    salt = base64.b64decode(salt_b64)
    recomputed = hashlib.pbkdf2_hmac(
        "sha256", _PW.encode("utf-8"), salt, int(iter_s), dklen=32
    )
    assert base64.b64encode(recomputed).decode("ascii") == hash_b64


def test_uses_clamped_iteration_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "10")  # sub-floor
    get_settings.cache_clear()
    s = hash_danger_passphrase(_PW)
    assert int(s.split("$")[1]) == MIN_KDF_ITERATIONS


# ---------------------------------------------------------------------------
# hash / verify round-trip
# ---------------------------------------------------------------------------
def test_round_trip_true() -> None:
    assert verify_danger_passphrase(_PW, hash_danger_passphrase(_PW)) is True


def test_wrong_passphrase_false() -> None:
    stored = hash_danger_passphrase(_PW)
    assert verify_danger_passphrase("nope", stored) is False
    assert verify_danger_passphrase(_PW + " ", stored) is False


def test_unicode_passphrase_round_trip() -> None:
    pw = "pässwörd-验证-\U0001f510"
    assert verify_danger_passphrase(pw, hash_danger_passphrase(pw)) is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "notpbkdf2$1$x$y",
        "pbkdf2$abc$x$y",            # non-int iterations
        "pbkdf2$100000$!!$!!",       # invalid base64
        "pbkdf2$100000$" + base64.b64encode(b"x" * 16).decode() + "$"
        + base64.b64encode(b"x" * 31).decode(),  # wrong digest length
        "pbkdf2$100000$x",           # too few fields
    ],
)
def test_malformed_stored_is_false_never_raises(bad: Any) -> None:
    assert verify_danger_passphrase(_PW, bad) is False


def test_empty_passphrase_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        hash_danger_passphrase("")
    assert verify_danger_passphrase("", hash_danger_passphrase(_PW)) is False


def test_downgraded_iteration_count_rejected() -> None:
    """A hostile DB write lowering work factor below the floor must fail
    closed even though the digest field is internally consistent."""
    salt = b"\x11" * 16
    weak_iter = 5
    digest = hashlib.pbkdf2_hmac(
        "sha256", _PW.encode("utf-8"), salt, weak_iter, dklen=32
    )
    forged = (
        f"pbkdf2${weak_iter}${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(digest).decode()}"
    )
    # Self-consistent, yet rejected purely for being under MIN_KDF_ITERATIONS.
    assert verify_danger_passphrase(_PW, forged) is False


def test_verify_is_constant_time() -> None:
    stored = hash_danger_passphrase(_PW)
    with patch(
        "polyecho.shared.danger_passphrase.hmac.compare_digest",
        return_value=True,
    ) as cmp_mock:
        assert verify_danger_passphrase(_PW, stored) is True
    cmp_mock.assert_called_once()


# ---------------------------------------------------------------------------
# async get / set / is-provisioned (in-memory settings double)
# ---------------------------------------------------------------------------
class _SettingRow:
    def __init__(self, key: str, value: Any) -> None:
        self.key = key
        self.value = value


class _Sess:
    """In-memory ``settings`` store.

    ``set_danger_passphrase`` constructs a **real** ``Setting`` ORM object
    and calls ``add``; ``select(Setting)`` also uses the real class — so we
    must NOT patch ``Setting`` (patching the class breaks ``select()``).
    The store just captures whatever object is added, keyed by ``.key``.
    """

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def add(self, obj: Any) -> None:
        self.store[obj.key] = obj

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        key = next(iter(stmt.compile().params.values()))
        row = self.store.get(key)

        class _R:
            def scalar_one_or_none(self) -> Any:
                return row

        return _R()


def test_is_provisioned_false_then_true_after_set() -> None:
    sess = _Sess()
    assert run(is_danger_passphrase_provisioned(sess)) is False
    assert run(get_danger_passphrase_hash(sess)) is None
    run(set_danger_passphrase(_PW, sess))
    assert run(is_danger_passphrase_provisioned(sess)) is True
    stored = run(get_danger_passphrase_hash(sess))
    assert stored is not None
    assert verify_danger_passphrase(_PW, stored) is True
    assert _PW not in stored  # plaintext never persisted


def test_set_rotates_existing_hash() -> None:
    sess = _Sess()
    run(set_danger_passphrase("first", sess))
    first = run(get_danger_passphrase_hash(sess))
    run(set_danger_passphrase("second", sess))
    second = run(get_danger_passphrase_hash(sess))
    assert first != second
    assert verify_danger_passphrase("second", second) is True
    assert verify_danger_passphrase("first", second) is False
    # Single key — rotation overwrites, never accumulates.
    assert list(sess.store) == [DANGER_PASSPHRASE_SETTINGS_KEY]


def test_get_coerces_dict_wrapped_value() -> None:
    sess = _Sess()
    canonical = hash_danger_passphrase(_PW)
    sess.store[DANGER_PASSPHRASE_SETTINGS_KEY] = _SettingRow(
        DANGER_PASSPHRASE_SETTINGS_KEY, {"value": canonical}
    )
    assert run(get_danger_passphrase_hash(sess)) == canonical

"""
Tests for ``polyecho account`` — account management (Phase 3).

Design
------
* importlib mode (no ``tests/__init__.py``).
* NO real network / DB: ``session_scope`` is a fake async-CM mock and the
  datasource is the bundled ``FixtureDataSource`` (or a small subclass).
* ``detect`` renders the 4-candidate table + recommendation.
* ``import`` round-trips: the hidden private-key prompt is fed via stdin,
  encryption is real (fast KDF via env), the row is upserted, status
  transitions to ``ready``, and ``decrypt_private_key`` recovers the key.
* ``list`` renders accounts and **never** prints key material.
* Secrets never appear in any command output — asserted explicitly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app
from polyecho.shared.accounts import derive_wallets
from polyecho.shared.datasource import (
    ActivityRecord,
    FixtureDataSource,
    PositionRecord,
)

runner = CliRunner()

_EOA = "0xAbC0000000000000000000000000000000000123"
_EOA_LOWER = _EOA.lower()
_PASSPHRASE = "correct horse battery staple"
_PRIVATE_KEY = "0x" + "ab" * 32
SWING = "0xswing1000000000000000000000000000000001"


@pytest.fixture(autouse=True)
def _fast_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin KDF iterations low so real encryption is fast & deterministic."""
    monkeypatch.setenv("POLYECHO_KEY_KDF_ITERATIONS", "1000")
    monkeypatch.setenv("POLYECHO_DB_URL", "postgresql+asyncpg://u:p@localhost/test")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fake session_scope
# ---------------------------------------------------------------------------
def _make_session_scope(scalar_result: Any = None) -> Any:
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(_stmt: Any) -> MagicMock:
            result = MagicMock()
            result.scalar_one_or_none.return_value = scalar_result
            result.scalars.return_value.all.return_value = (
                scalar_result if isinstance(scalar_result, list) else (
                    [scalar_result] if scalar_result is not None else []
                )
            )
            return result

        session.execute = fake_execute
        session.commit = AsyncMock()
        session.rollback = AsyncMock()
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


def _account_row(**over: Any) -> MagicMock:
    a = MagicMock()
    a.name = over.get("name", "alice")
    a.eoa_address = over.get("eoa_address", _EOA_LOWER)
    a.funder_address = over.get("funder_address", "0x" + "cd" * 20)
    a.signature_type = over.get("signature_type", 2)
    a.status = over.get("status", "ready")
    a.encrypted_private_key = over.get("encrypted_private_key", b"\x00\x10")
    a.created_at = "2026-05-17T00:00:00+00:00"
    return a


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------
class _SwingDS:
    """DataSource-shaped fake (composition) mapping the type-2 candidate
    onto the SWING archetype fixtures so detection has data to rank."""

    def __init__(self, type2_addr: str) -> None:
        self._t2 = type2_addr
        self._fx = FixtureDataSource()

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        tgt = SWING if wallet.lower() == self._t2 else wallet
        return await self._fx.get_positions(tgt, size_threshold=size_threshold)

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        tgt = SWING if wallet.lower() == self._t2 else wallet
        return await self._fx.get_user_activity(
            tgt, start=start, end=end, activity_type=activity_type, limit=limit
        )


class TestAccountDetect:
    def test_detect_renders_table_and_recommendation(self) -> None:
        w = derive_wallets(_EOA)
        ds = _SwingDS(w[2])
        with patch("polyecho.shared.datasource.get_datasource", return_value=ds):
            result = runner.invoke(
                app, ["account", "detect", _EOA], env={"COLUMNS": "200"}
            )

        assert result.exit_code == 0, result.output
        out = result.output
        # All four candidate types shown.
        for name in ("EOA", "POLY_PROXY", "POLY_GNOSIS_SAFE", "POLY_1271"):
            assert name in out
        assert "Recommended" in out
        assert "Type 2" in out
        assert _EOA_LOWER in out

    def test_detect_no_data_reports_none(self) -> None:
        ds = FixtureDataSource()  # nothing maps
        with patch("polyecho.shared.datasource.get_datasource", return_value=ds):
            result = runner.invoke(app, ["account", "detect", "0x" + "ab" * 20])
        assert result.exit_code == 0, result.output
        assert "No positions or activity" in result.output

    def test_detect_invalid_address_exits_nonzero(self) -> None:
        result = runner.invoke(app, ["account", "detect", "not-an-address"])
        assert result.exit_code != 0
        assert "Invalid address" in result.output


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------
class TestAccountImport:
    def test_import_encrypts_and_creates_row(self) -> None:
        """import → new Account with status 'ready', funder from derivation,
        and an encrypted_private_key blob that decrypts back to the key."""
        from polyecho.shared.crypto import decrypt_private_key

        scope = _make_session_scope(scalar_result=None)  # no existing row
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "--signature-type", "2", "--name", "alice"],
                # passphrase (prompt+confirm) then private key (hidden prompt)
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )

        assert result.exit_code == 0, result.output
        assert "created" in result.output
        assert "ready" in result.output
        # Secret never echoed.
        assert _PRIVATE_KEY not in result.output
        assert _PASSPHRASE not in result.output

        session = scope._captured["session"]
        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.eoa_address == _EOA_LOWER
        assert added.signature_type == 2
        assert added.status == "ready"
        assert added.funder_address == derive_wallets(_EOA)[2]
        assert added.name == "alice"
        # Round-trip: the stored blob decrypts to the original key.
        blob = added.encrypted_private_key
        assert isinstance(blob, bytes)
        assert decrypt_private_key(blob, _PASSPHRASE) == _PRIVATE_KEY

    def test_import_updates_existing_row_status_transition(self) -> None:
        """An existing pending row is upserted in place → status becomes
        ready and the encrypted key is set."""
        existing = _account_row(
            status="pending_key_import", encrypted_private_key=None
        )
        scope = _make_session_scope(scalar_result=existing)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "-t", "1"],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )

        assert result.exit_code == 0, result.output
        assert "updated" in result.output
        # session.add NOT called (in-place update of the existing row).
        scope._captured["session"].add.assert_not_called()
        assert existing.status == "ready"
        assert existing.signature_type == 1
        assert existing.encrypted_private_key is not None
        assert existing.funder_address == derive_wallets(_EOA)[1]

    def test_import_empty_private_key_aborts(self) -> None:
        scope = _make_session_scope(scalar_result=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n   \n",
            )
        assert result.exit_code != 0
        assert "Empty private key" in result.output
        assert _PRIVATE_KEY not in result.output
        # Empty key aborts *before* any DB access — the session_scope CM was
        # never even entered (good: no connection attempt on bad input).
        assert "session" not in scope._captured

    def test_import_invalid_eoa_exits(self) -> None:
        result = runner.invoke(
            app,
            ["account", "import", "bad-addr"],
            input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
        )
        assert result.exit_code != 0
        assert "Invalid address" in result.output

    def test_import_db_failure_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom_scope() -> AsyncGenerator[Any, None]:
            raise RuntimeError("db unreachable")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom_scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )
        assert result.exit_code != 0
        assert "Import failed" in result.output
        # Even on failure the key must not leak.
        assert _PRIVATE_KEY not in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
class TestAccountList:
    def test_list_renders_accounts_without_key_material(self) -> None:
        secret_blob = b"\xde\xad\xbe\xef" * 8
        rows = [
            _account_row(name="alice", status="ready",
                         encrypted_private_key=secret_blob),
            _account_row(name="bob", status="pending_key_import",
                         encrypted_private_key=None,
                         eoa_address="0x" + "11" * 20),
        ]
        scope = _make_session_scope(scalar_result=rows)
        with patch("polyecho.shared.db.session_scope", scope):
            # Wide terminal so Rich does not truncate the status column.
            result = runner.invoke(
                app, ["account", "list"], env={"COLUMNS": "200"}
            )

        assert result.exit_code == 0, result.output
        out = result.output
        assert "alice" in out
        assert "bob" in out
        assert "ready" in out
        assert "pending_key_import" in out
        assert "encrypted" in out  # presence indicator, not the bytes
        # The raw key bytes / hex must never be rendered.
        assert secret_blob.hex() not in out
        assert "deadbeef" not in out.lower()

    def test_list_empty_shows_hint(self) -> None:
        scope = _make_session_scope(scalar_result=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["account", "list"])
        assert result.exit_code == 0, result.output
        assert "No accounts registered" in result.output

    def test_list_db_error_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom_scope() -> AsyncGenerator[Any, None]:
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom_scope):
            result = runner.invoke(app, ["account", "list"])
        assert result.exit_code != 0
        assert "Could not load accounts" in result.output


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------
class TestAccountBalance:
    def test_balance_resolves_and_reports(self) -> None:
        row = _account_row(name="alice", funder_address=SWING, signature_type=2)
        scope = _make_session_scope(scalar_result=row)
        with (
            patch("polyecho.shared.db.session_scope", scope),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=FixtureDataSource(),
            ),
            patch(
                "polyecho.shared.accounts.fetch_usdc_balance",
                new=AsyncMock(return_value=987.65),
            ),
        ):
            result = runner.invoke(app, ["account", "balance", "alice"])

        assert result.exit_code == 0, result.output
        out = result.output
        assert "alice" in out
        assert "987.65" in out
        assert "open" in out  # positions summary line

    def test_balance_account_not_found(self) -> None:
        scope = _make_session_scope(scalar_result=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(app, ["account", "balance", "ghost"])
        assert result.exit_code != 0
        assert "Account not found" in result.output

    def test_balance_unavailable_is_graceful(self) -> None:
        row = _account_row(name="alice", funder_address=SWING)
        scope = _make_session_scope(scalar_result=row)
        with (
            patch("polyecho.shared.db.session_scope", scope),
            patch(
                "polyecho.shared.datasource.get_datasource",
                return_value=FixtureDataSource(),
            ),
            patch(
                "polyecho.shared.accounts.fetch_usdc_balance",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = runner.invoke(app, ["account", "balance", "alice"])
        assert result.exit_code == 0, result.output
        assert "unavailable" in result.output

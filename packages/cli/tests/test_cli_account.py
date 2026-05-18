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
    """DataSource-shaped fake (composition) mapping the type-1 candidate
    onto the SWING archetype fixtures so detection has data to rank.

    P0-5: ``derive_wallets`` no longer fabricates type-2/3 addresses (they
    are ``None``), so the only non-EOA derivable candidate is type-1
    (verified CREATE2 Magic proxy).  We map that onto SWING fixtures."""

    def __init__(self, type1_addr: str) -> None:
        self._t1 = type1_addr
        self._fx = FixtureDataSource()

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        tgt = SWING if wallet.lower() == self._t1 else wallet
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
        tgt = SWING if wallet.lower() == self._t1 else wallet
        return await self._fx.get_user_activity(
            tgt, start=start, end=end, activity_type=activity_type, limit=limit
        )


class TestAccountDetect:
    def test_detect_renders_table_and_recommendation(self) -> None:
        # P0-5: derive_wallets returns {0: eoa, 1: magic_proxy, 2: None,
        # 3: None}.  type-2/3 have NO trustworthy offline derivation, so
        # the only derivable non-EOA candidate is type-1 (verified CREATE2).
        # The fixture maps SWING onto type-1, so type-1 must be recommended.
        w = derive_wallets(_EOA)
        assert w[2] is None and w[3] is None  # contract: no fabricated addrs
        t1 = w[1]
        assert t1 is not None  # type-1 is the verified CREATE2 candidate
        ds = _SwingDS(t1)
        with patch("polyecho.shared.datasource.get_datasource", return_value=ds):
            result = runner.invoke(
                app, ["account", "detect", _EOA], env={"COLUMNS": "200"}
            )

        assert result.exit_code == 0, result.output
        out = result.output
        # All four candidate types still shown (the result describes 0..3).
        for name in ("EOA", "POLY_PROXY", "POLY_GNOSIS_SAFE", "POLY_1271"):
            assert name in out
        assert "Recommended" in out
        # type-1 (the only derivable non-EOA candidate, fed SWING data) wins.
        assert "Type 1" in out
        assert _EOA_LOWER in out
        # type-2/3 must render the safe zh-TW hint, NOT a fabricated address
        # and NOT a crash.
        assert "需顯式提供 funder" in out

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
    def test_import_type2_with_explicit_funder_creates_row(self) -> None:
        """P0-5: type-2 (POLY_GNOSIS_SAFE) has NO offline derivation
        (derive_wallets[2] is None).  import MUST require an explicit,
        operator-supplied --funder and persist EXACTLY that (lowercased) —
        never a fabricated address.  Round-trips the encrypted key too."""
        from polyecho.shared.crypto import decrypt_private_key

        assert derive_wallets(_EOA)[2] is None  # contract: no fabricated addr
        explicit_funder = "0x" + "FE" * 20

        scope = _make_session_scope(scalar_result=None)  # no existing row
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                [
                    "account", "import", _EOA,
                    "--signature-type", "2",
                    "--name", "alice",
                    "--funder", explicit_funder,
                ],
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
        # The persisted funder is EXACTLY the operator-supplied value
        # (lowercased) — NOT a fabricated derivation.
        assert added.funder_address == explicit_funder.lower()
        assert added.name == "alice"
        # Round-trip: the stored blob decrypts to the original key.
        blob = added.encrypted_private_key
        assert isinstance(blob, bytes)
        assert decrypt_private_key(blob, _PASSPHRASE) == _PRIVATE_KEY

    def test_import_type2_without_funder_refuses(self) -> None:
        """P0-5 core: type-2 with NO --funder must refuse loudly and
        persist NOTHING (no fabricated address ever written)."""
        scope = _make_session_scope(scalar_result=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "--signature-type", "2"],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )
        assert result.exit_code != 0
        assert "需顯式提供" in result.output or "缺少 funder" in result.output
        # Never reached the DB write — no fabricated row persisted.
        assert "session" not in scope._captured

    def test_import_type3_without_funder_refuses(self) -> None:
        """P0-5: type-3 (POLY_1271) likewise has no derivation and must
        fail-closed without an explicit --funder."""
        assert derive_wallets(_EOA)[3] is None
        scope = _make_session_scope(scalar_result=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "-t", "3"],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )
        assert result.exit_code != 0
        assert "session" not in scope._captured

    def test_import_type2_invalid_funder_rejected(self) -> None:
        """An explicit but malformed --funder is rejected before any DB
        access (no garbage address persisted)."""
        scope = _make_session_scope(scalar_result=None)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "-t", "2", "--funder", "not-an-addr"],
                input=f"{_PASSPHRASE}\n{_PASSPHRASE}\n{_PRIVATE_KEY}\n",
            )
        assert result.exit_code != 0
        assert "有效的 EVM 位址" in result.output
        assert "session" not in scope._captured

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
            # type-1 is offline-derivable (no --funder needed), so this
            # exercises the empty-key guard, not the P0-5 funder refusal.
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "-t", "1"],
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
            # type-1 reaches the DB write (offline-derivable funder), so a
            # session failure surfaces as "Import failed" — the path under
            # test (unchanged by P0-5).
            result = runner.invoke(
                app,
                ["account", "import", _EOA, "-t", "1"],
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


# ---------------------------------------------------------------------------
# set-danger-passphrase  (P0-1 — operator provisioning)
# ---------------------------------------------------------------------------
class TestSetDangerPassphrase:
    _PP = "operator danger secret"

    def test_provisions_and_reports_true(self) -> None:
        """The hidden+confirm prompt feeds set_danger_passphrase, then
        is_danger_passphrase_provisioned() is shown true.  The plaintext
        never appears in the output."""
        set_mock = AsyncMock()
        prov_mock = AsyncMock(return_value=True)
        with (
            patch(
                "polyecho.shared.danger_passphrase.set_danger_passphrase",
                new=set_mock,
            ),
            patch(
                "polyecho.shared.danger_passphrase.is_danger_passphrase_provisioned",
                new=prov_mock,
            ),
        ):
            result = runner.invoke(
                app,
                ["account", "set-danger-passphrase"],
                input=f"{self._PP}\n{self._PP}\n",
            )
        assert result.exit_code == 0, result.output
        set_mock.assert_awaited_once_with(self._PP)
        assert "true" in result.output
        # Plaintext secret must never be echoed.
        assert self._PP not in result.output

    def test_passphrase_not_a_cli_option(self) -> None:
        """The passphrase must NOT be bindable as a CLI option (no
        --passphrase) so it can never land in argv / shell history."""
        result = runner.invoke(
            app, ["account", "set-danger-passphrase", "--help"]
        )
        assert result.exit_code == 0, result.output
        assert "--passphrase" not in result.output

    def test_empty_passphrase_aborts(self) -> None:
        with patch(
            "polyecho.shared.danger_passphrase.set_danger_passphrase",
            new=AsyncMock(),
        ) as set_mock:
            result = runner.invoke(
                app,
                ["account", "set-danger-passphrase"],
                input="   \n   \n",
            )
        assert result.exit_code != 0
        set_mock.assert_not_awaited()

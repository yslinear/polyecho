"""
Hardened tests for ``polyecho db migrate``.

Complements ``test_cli_backtest.py``'s migrate tests by covering:
* the *real* ``infra/migrations/0000_init.sql`` is located and read,
* the exact SQL bytes read from disk are forwarded to the driver,
* the engine-execution failure path exits non-zero (``sys.exit(1)``),
* ``_migration_path`` env-var override + repo-root walk + not-found.

NO real DB: the async engine / connection is fully mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

import polyecho.cli.db as db_module
from polyecho.cli.__main__ import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# _migration_path resolution
# ---------------------------------------------------------------------------


class TestMigrationPath:
    def test_locates_real_init_sql(self) -> None:
        """The repo-root walk must find the real 0000_init.sql, and it
        must be a non-trivial, idempotent (CREATE TABLE IF NOT EXISTS) script."""
        path = db_module._migration_path()
        assert path.name == "0000_init.sql"
        assert path.exists()
        sql = path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert len(sql.splitlines()) > 50  # baseline schema is substantial

    def test_env_var_override(self, tmp_path: Path) -> None:
        """POLYECHO_REPO_ROOT must take precedence over the directory walk."""
        with patch.dict("os.environ", {"POLYECHO_REPO_ROOT": str(tmp_path)}):
            path = db_module._migration_path()
        assert path == tmp_path / "infra" / "migrations" / "0000_init.sql"

    def test_not_found_raises(self, tmp_path: Path) -> None:
        """A bogus __file__ with no repo sentinel -> FileNotFoundError."""
        fake_file = tmp_path / "deep" / "nested" / "db.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.write_text("")
        with (
            patch.dict("os.environ", {}, clear=False) as env,
            patch.object(db_module, "__file__", str(fake_file)),
        ):
            env.pop("POLYECHO_REPO_ROOT", None)
            with pytest.raises(FileNotFoundError, match="Cannot locate repo root"):
                db_module._migration_path()


# ---------------------------------------------------------------------------
# db migrate — happy path with the REAL sql file
# ---------------------------------------------------------------------------


def _fake_conn(executed: list[str]) -> MagicMock:
    async def fake_exec_driver_sql(sql: str) -> None:
        executed.append(sql)

    conn = MagicMock()
    conn.exec_driver_sql = fake_exec_driver_sql
    conn.commit = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


class TestDbMigrateExecution:
    def test_real_sql_is_read_and_forwarded(self) -> None:
        """db migrate reads the real 0000_init.sql and forwards the exact
        text to exec_driver_sql in a single call, then commits."""
        real_sql = db_module._migration_path().read_text(encoding="utf-8")
        executed: list[str] = []
        conn = _fake_conn(executed)
        engine = MagicMock()
        engine.connect.return_value = conn

        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code == 0, result.output
        assert "Applied" in result.output
        assert "0000_init.sql" in result.output
        assert executed == [real_sql]
        conn.commit.assert_awaited_once()

    def test_engine_failure_exits_nonzero(self) -> None:
        """If the driver execute raises, migrate must exit non-zero and
        print the failure (covers the sys.exit(1) branch)."""

        async def boom(_sql: str) -> None:
            raise RuntimeError("relation already exists, fatal")

        conn = MagicMock()
        conn.exec_driver_sql = boom
        conn.commit = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.connect.return_value = conn

        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code != 0
        assert "Migration failed" in result.output

    def test_connect_failure_exits_nonzero(self) -> None:
        """Engine.connect() context-manager raising -> non-zero exit."""
        conn = MagicMock()
        conn.__aenter__ = AsyncMock(side_effect=OSError("db unreachable"))
        conn.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.connect.return_value = conn

        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate"])

        assert result.exit_code != 0
        assert "Migration failed" in result.output

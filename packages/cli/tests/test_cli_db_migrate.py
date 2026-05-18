"""
Hardened tests for ``polyecho db migrate`` and ``polyecho db backup``.

Covers:
* the *real* ``infra/migrations/0000_init.sql`` is located and read,
* the exact SQL bytes are forwarded to the **raw asyncpg** connection
  (``engine.raw_connection().driver_connection.execute(sql)`` — the
  multi-statement simple-query path, NOT the broken prepared
  ``exec_driver_sql`` path),
* the execution-failure path exits non-zero,
* ``--dry-run`` executes nothing and prints a redacted target,
* the non-TTY confirmation gate (``--yes`` / ``POLYECHO_DB_MIGRATE_YES``),
* ``_migration_path`` env-var override + repo-root walk + not-found,
* ``db backup`` command/path shaping (subprocess fully mocked — no real
  pg_dump), gpg path, dry-run, and failure exit.

NO real DB / NO real pg_dump: engine + subprocess are fully mocked.
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
        path = db_module._migration_path()
        assert path.name == "0000_init.sql"
        assert path.exists()
        sql = path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert len(sql.splitlines()) > 50

    def test_env_var_override(self, tmp_path: Path) -> None:
        with patch.dict("os.environ", {"POLYECHO_REPO_ROOT": str(tmp_path)}):
            path = db_module._migration_path()
        assert path == tmp_path / "infra" / "migrations" / "0000_init.sql"

    def test_not_found_raises(self, tmp_path: Path) -> None:
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
# Shared raw-connection fake (mirrors test_schema_reflect.py's real path)
# ---------------------------------------------------------------------------


def _fake_engine(executed: list[str], *, boom: bool = False) -> MagicMock:
    """An engine whose raw_connection().driver_connection.execute records SQL."""

    async def fake_execute(sql: str) -> None:
        if boom:
            raise RuntimeError("relation already exists, fatal")
        executed.append(sql)

    driver_conn = MagicMock()
    driver_conn.execute = fake_execute

    raw = MagicMock()
    raw.driver_connection = driver_conn
    raw.close = MagicMock()

    engine = MagicMock()
    engine.raw_connection = AsyncMock(return_value=raw)
    engine.dispose = AsyncMock()
    return engine


# ---------------------------------------------------------------------------
# db migrate — happy path / failure / flags
# ---------------------------------------------------------------------------


class TestDbMigrateExecution:
    def test_real_sql_forwarded_via_raw_asyncpg(self) -> None:
        """db migrate reads the real 0000_init.sql and forwards the exact
        text to the RAW asyncpg driver_connection.execute in one call."""
        real_sql = db_module._migration_path().read_text(encoding="utf-8")
        executed: list[str] = []
        engine = _fake_engine(executed)

        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Applied" in result.output
        assert "0000_init.sql" in result.output
        assert executed == [real_sql]
        engine.raw_connection.assert_awaited_once()
        engine.dispose.assert_awaited_once()

    def test_engine_failure_exits_nonzero(self) -> None:
        engine = _fake_engine([], boom=True)
        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate", "--yes"])
        assert result.exit_code != 0
        assert "Migration failed" in result.output

    def test_none_driver_connection_raises_runtime_error_not_assert(
        self,
    ) -> None:
        """R14 #4: the None-driver_connection guard must be an explicit
        RuntimeError, NOT `assert` (assert is stripped under
        python -O / PYTHONOPTIMIZE — a None would then slip through to an
        opaque AttributeError). We simulate a raw conn whose
        driver_connection is None and assert a clean non-zero exit with
        the explicit guard message."""
        raw = MagicMock()
        raw.driver_connection = None
        raw.close = MagicMock()
        engine = MagicMock()
        engine.raw_connection = AsyncMock(return_value=raw)
        engine.dispose = AsyncMock()

        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate", "--yes"])

        assert result.exit_code != 0
        assert "Migration failed" in result.output
        # The explicit Runtimeable message (proves it is not a bare
        # AssertionError nor a None.execute AttributeError).
        assert "driver_connection" in result.output
        raw.close.assert_called_once()

    def test_dry_run_executes_nothing_and_redacts(self) -> None:
        """--dry-run prints the redacted target + sql path, runs no SQL."""
        executed: list[str] = []
        engine = _fake_engine(executed)
        with patch("polyecho.shared.db.get_engine", return_value=engine):
            result = runner.invoke(app, ["db", "migrate", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert "0000_init.sql" in result.output
        # password must never appear; redacted form uses ***
        assert "polyecho:polyecho@" not in result.output
        assert executed == []
        engine.raw_connection.assert_not_called()

    def test_non_tty_without_yes_aborts(self) -> None:
        """No TTY + no --yes + no env => refuse with exit code 2 (no exec)."""
        executed: list[str] = []
        engine = _fake_engine(executed)
        with (
            patch("polyecho.shared.db.get_engine", return_value=engine),
            patch.dict("os.environ", {}, clear=False) as env,
        ):
            env.pop("POLYECHO_DB_MIGRATE_YES", None)
            # CliRunner provides a non-TTY stdin by default.
            result = runner.invoke(app, ["db", "migrate"])
        assert result.exit_code == 2, result.output
        assert "without confirmation" in result.output
        assert executed == []

    def test_env_var_bypasses_confirmation(self) -> None:
        executed: list[str] = []
        engine = _fake_engine(executed)
        with (
            patch("polyecho.shared.db.get_engine", return_value=engine),
            patch.dict(
                "os.environ", {"POLYECHO_DB_MIGRATE_YES": "1"}, clear=False
            ),
        ):
            result = runner.invoke(app, ["db", "migrate"])
        assert result.exit_code == 0, result.output
        assert len(executed) == 1


# ---------------------------------------------------------------------------
# db backup — command/path shaping, fully mocked subprocess
# ---------------------------------------------------------------------------


def _fake_popen(stdout: bytes = b"-- dump --\n", rc: int = 0) -> MagicMock:
    """A Popen double whose .stdout streams *stdout* once then EOF.

    R14 #3: the production code is now deadlock-safe — it streams stdout
    to gzip then calls ``proc.wait()`` (stderr goes to a real-fd temp
    file, never an undrained PIPE). So ``wait()`` must return the real
    exit code, NOT a truthy MagicMock. ``communicate`` is deliberately a
    tripwire: if production ever reverts to ``communicate()`` while
    streaming stdout (the deadlock pattern) this raises instead of
    silently passing.
    """
    import io

    proc = MagicMock()
    proc.stdout = io.BytesIO(stdout)
    proc.returncode = rc
    proc.wait = MagicMock(return_value=rc)

    def _no_communicate(*_a: object, **_k: object) -> tuple[bytes, bytes]:
        raise AssertionError(
            "db backup must NOT call proc.communicate() while it is "
            "streaming stdout to gzip — that is the R14 #3 deadlock "
            "pattern (stderr PIPE fills, child blocks forever)."
        )

    proc.communicate = MagicMock(side_effect=_no_communicate)
    return proc


class TestDbBackup:
    def test_dry_run_no_subprocess(self) -> None:
        with (
            patch("polyecho.cli.db.subprocess.Popen") as popen_mock,
            patch("polyecho.cli.db.subprocess.run") as run_mock,
        ):
            result = runner.invoke(app, ["db", "backup", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert "polyecho:polyecho@" not in result.output  # redacted
        popen_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_pg_dump_command_and_path_shape(self, tmp_path: Path) -> None:
        """pg_dump is invoked with the libpq DSN + safe flags, writing a
        timestamped .sql.gz under --out (no real pg_dump runs)."""
        with (
            patch(
                "polyecho.cli.db.shutil.which", return_value="/usr/bin/pg_dump"
            ),
            patch(
                "polyecho.cli.db.subprocess.Popen",
                return_value=_fake_popen(),
            ) as popen_mock,
        ):
            result = runner.invoke(
                app, ["db", "backup", "--out", str(tmp_path)]
            )

        assert result.exit_code == 0, result.output
        popen_mock.assert_called_once()
        argv = popen_mock.call_args[0][0]
        assert argv[0] == "pg_dump"
        assert "--dbname" in argv
        dsn = argv[argv.index("--dbname") + 1]
        assert dsn.startswith("postgresql://")  # libpq, +asyncpg stripped
        assert "--no-owner" in argv and "--no-privileges" in argv
        assert "Backup written" in result.output
        assert ".sql.gz" in result.output
        # The gzip artifact was actually written under --out.
        artifacts = list(tmp_path.glob("polyecho-*.sql.gz"))
        assert len(artifacts) == 1

    def test_missing_pg_dump_exits_nonzero(self, tmp_path: Path) -> None:
        with patch("polyecho.cli.db.shutil.which", return_value=None):
            result = runner.invoke(
                app, ["db", "backup", "--out", str(tmp_path)]
            )
        assert result.exit_code != 0
        assert "pg_dump not found" in result.output

    def test_pg_dump_failure_exits_nonzero(self, tmp_path: Path) -> None:
        with (
            patch(
                "polyecho.cli.db.shutil.which", return_value="/usr/bin/pg_dump"
            ),
            patch(
                "polyecho.cli.db.subprocess.Popen",
                return_value=_fake_popen(stdout=b"", rc=1),
            ),
        ):
            result = runner.invoke(
                app, ["db", "backup", "--out", str(tmp_path)]
            )
        assert result.exit_code != 0
        assert "Backup failed" in result.output
        # Failed dump must not leave a partial artifact behind.
        assert list(tmp_path.glob("polyecho-*.sql.gz")) == []

    def test_backup_is_deadlock_safe_no_undrained_stderr_pipe(
        self, tmp_path: Path
    ) -> None:
        """R14 #3 regression: backup must NOT use the deadlock pattern
        (consume stdout, then communicate() with an undrained stderr
        PIPE). Assert: communicate() is never called, and stderr is NOT
        subprocess.PIPE (it must be a real fd / file object so pg_dump
        can never block on a full stderr pipe buffer)."""
        import subprocess as _sp

        fp = _fake_popen()
        with (
            patch(
                "polyecho.cli.db.shutil.which", return_value="/usr/bin/pg_dump"
            ),
            patch(
                "polyecho.cli.db.subprocess.Popen", return_value=fp
            ) as popen_mock,
        ):
            result = runner.invoke(
                app, ["db", "backup", "--out", str(tmp_path)]
            )
        assert result.exit_code == 0, result.output
        # The deadlock-prone communicate() path was never taken.
        fp.communicate.assert_not_called()
        # proc.wait() (deadlock-safe: stdout already drained in-process)
        # is the join used instead.
        fp.wait.assert_called_once()
        # stderr is NOT an undrained PIPE — passing subprocess.PIPE while
        # the parent only reads stdout is the exact deadlock condition.
        _, kwargs = popen_mock.call_args
        assert kwargs.get("stderr") is not _sp.PIPE
        assert kwargs.get("stdout") is _sp.PIPE  # stdout IS streamed
        # The gzip artifact was still produced (behaviour preserved).
        assert len(list(tmp_path.glob("polyecho-*.sql.gz"))) == 1

    def test_gpg_recipient_encrypts(self, tmp_path: Path) -> None:
        """--gpg-recipient adds a gpg --encrypt step producing a .gpg."""
        with (
            patch(
                "polyecho.cli.db.shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            patch(
                "polyecho.cli.db.subprocess.Popen",
                return_value=_fake_popen(),
            ),
            patch("polyecho.cli.db.subprocess.run") as run_mock,
        ):
            run_mock.return_value = MagicMock(returncode=0)
            result = runner.invoke(
                app,
                [
                    "db",
                    "backup",
                    "--out",
                    str(tmp_path),
                    "--gpg-recipient",
                    "ops@example.com",
                ],
            )

        assert result.exit_code == 0, result.output
        # gpg is the only subprocess.run call (pg_dump uses Popen).
        run_mock.assert_called_once()
        gpg_argv = run_mock.call_args[0][0]
        assert gpg_argv[0] == "gpg"
        assert "--encrypt" in gpg_argv
        assert "ops@example.com" in gpg_argv
        assert ".gpg" in result.output

"""
Tests for ``polyecho logs show`` — compose log viewer (Wave C-cli).

Design
------
* importlib mode; Typer ``CliRunner``.
* NO real docker — ``shutil.which`` and ``subprocess.run`` are fully
  mocked.  We assert the exact ``docker compose logs`` argv shape, the
  service allowlist, the tail/follow flags, the docker-unavailable error,
  and that a non-zero compose exit propagates non-zero.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


def _ok_proc() -> MagicMock:
    p = MagicMock()
    p.returncode = 0
    return p


class TestLogsShow:
    def test_invokes_docker_compose_logs_with_tail(self) -> None:
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker"
                if b == "docker"
                else None,
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock,
        ):
            result = runner.invoke(
                app,
                ["logs", "show", "trader-core", "--tail", "50"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        run_mock.assert_called_once()
        argv = run_mock.call_args[0][0]
        assert argv[:2] == ["docker", "compose"]
        assert "logs" in argv
        assert "--tail" in argv and "50" in argv
        assert argv[-1] == "trader-core"
        assert "--follow" not in argv

    def test_follow_flag_added(self) -> None:
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker"
                if b == "docker"
                else None,
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock,
        ):
            result = runner.invoke(
                app,
                ["logs", "show", "postgres", "--follow"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        argv = run_mock.call_args[0][0]
        assert "--follow" in argv

    def test_legacy_docker_compose_binary_fallback(self) -> None:
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker-compose"
                if b == "docker-compose"
                else None,
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                return_value=_ok_proc(),
            ) as run_mock,
        ):
            result = runner.invoke(
                app, ["logs", "show", "dashboard-web"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        argv = run_mock.call_args[0][0]
        assert argv[0] == "docker-compose"
        assert "compose" not in argv  # legacy binary, no `compose` subcmd

    def test_unknown_service_rejected_nonzero(self) -> None:
        with patch("polyecho.cli.logs.subprocess.run") as run_mock:
            result = runner.invoke(
                app, ["logs", "show", "not-a-service"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "Unknown service" in result.output
        run_mock.assert_not_called()

    def test_docker_unavailable_exits_nonzero(self) -> None:
        with patch(
            "polyecho.cli.logs.shutil.which", return_value=None
        ):
            result = runner.invoke(
                app, ["logs", "show", "trader-core"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "No compose CLI found" in result.output

    def test_compose_nonzero_exit_propagates(self) -> None:
        bad = MagicMock()
        bad.returncode = 1
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker"
                if b == "docker"
                else None,
            ),
            patch("polyecho.cli.logs.subprocess.run", return_value=bad),
        ):
            result = runner.invoke(
                app, ["logs", "show", "discovery-worker"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "exited 1" in result.output


@pytest.mark.parametrize(
    "args",
    [["logs", "--help"], ["logs", "show", "--help"]],
    ids=lambda a: " ".join(a),
)
def test_logs_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output

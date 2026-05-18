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

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


def _ok_proc() -> MagicMock:
    p = MagicMock()
    p.returncode = 0
    return p


def _compose_run_router(
    *, plugin_ok: bool = True, logs_rc: int = 0
) -> Callable[..., MagicMock]:
    """A ``subprocess.run`` side_effect that distinguishes the cheap
    ``docker compose version`` *probe* (R15 #3) from the real
    ``docker compose logs`` call so tests can assert each independently.
    """

    def _run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        p = MagicMock()
        if argv[:3] == ["docker", "compose", "version"]:
            p.returncode = 0 if plugin_ok else 1
        else:
            p.returncode = logs_rc
        return p

    return _run


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
                side_effect=_compose_run_router(),
            ) as run_mock,
        ):
            result = runner.invoke(
                app,
                ["logs", "show", "trader-core", "--tail", "50"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        # Two run() calls now: the plugin probe + the real logs call.
        argv = run_mock.call_args[0][0]  # last call = the logs invocation
        assert argv[:2] == ["docker", "compose"]
        assert "logs" in argv
        assert "--tail" in argv and "50" in argv
        assert argv[-1] == "trader-core"
        assert "--follow" not in argv
        # The probe ran exactly once before the logs call.
        probe_calls = [
            c
            for c in run_mock.call_args_list
            if c[0][0][:3] == ["docker", "compose", "version"]
        ]
        assert len(probe_calls) == 1

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
                side_effect=_compose_run_router(),
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
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker"
                if b == "docker"
                else None,
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                # plugin probe OK, but the real `logs` call exits 1.
                side_effect=_compose_run_router(plugin_ok=True, logs_rc=1),
            ),
        ):
            result = runner.invoke(
                app, ["logs", "show", "discovery-worker"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "exited 1" in result.output

    def test_docker_present_but_compose_plugin_missing_falls_back(
        self,
    ) -> None:
        """R15 #3: docker on PATH but the Compose V2 plugin absent ⇒ do
        NOT blindly return ['docker','compose'] (that yields a confusing
        'compose is not a docker command' later). The probe fails, so we
        fall back to the legacy docker-compose binary."""
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                # BOTH docker and the legacy binary are present.
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                side_effect=_compose_run_router(plugin_ok=False),
            ) as run_mock,
        ):
            result = runner.invoke(
                app, ["logs", "show", "trader-core"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        # The probe ran (and reported the plugin missing)…
        assert any(
            c[0][0][:3] == ["docker", "compose", "version"]
            for c in run_mock.call_args_list
        )
        # …so the actual logs call used the legacy standalone binary.
        logs_call = run_mock.call_args[0][0]
        assert logs_call[0] == "docker-compose"
        assert logs_call[:2] != ["docker", "compose"]

    def test_docker_present_plugin_missing_and_no_legacy_errors_clean(
        self,
    ) -> None:
        """docker present, plugin missing, NO docker-compose ⇒ the clean
        'No compose CLI found' message + non-zero (not a confusing
        runtime docker error)."""
        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: "/usr/bin/docker"
                if b == "docker"
                else None,
            ),
            patch(
                "polyecho.cli.logs.subprocess.run",
                side_effect=_compose_run_router(plugin_ok=False),
            ),
        ):
            result = runner.invoke(
                app, ["logs", "show", "trader-core"], env={"COLUMNS": "200"}
            )
        assert result.exit_code != 0
        assert "No compose CLI found" in result.output

    def test_probe_oserror_treated_as_plugin_unavailable(self) -> None:
        """If the probe itself raises OSError (docker shim broken), treat
        the plugin as unavailable and fall back cleanly — never crash."""
        def _raise(argv: list[str], *_a: object, **_k: object) -> MagicMock:
            if argv[:3] == ["docker", "compose", "version"]:
                raise OSError("exec format error")
            p = MagicMock()
            p.returncode = 0
            return p

        with (
            patch(
                "polyecho.cli.logs.shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}",
            ),
            patch("polyecho.cli.logs.subprocess.run", side_effect=_raise),
        ):
            result = runner.invoke(
                app, ["logs", "show", "trader-core"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        # Fell back to the legacy binary rather than crashing.


@pytest.mark.parametrize(
    "args",
    [["logs", "--help"], ["logs", "show", "--help"]],
    ids=lambda a: " ".join(a),
)
def test_logs_help_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output

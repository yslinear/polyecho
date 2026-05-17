"""
Tests for ``polyecho doctor`` — system health-check command.

Design
------
* importlib mode (no ``__init__.py`` in tests/).
* NO real network / DB / subprocess — every external touch-point is patched.
* Each of the four checks (Config, Database, Polymarket, Tailnet) is exercised
  in both its passing and failing branch, plus the exit-code contract:
    - all OK                       -> exit 0
    - DB down (critical failure)   -> exit 1
    - only non-critical warnings   -> exit 0
* Secrets must never appear in output — asserted explicitly.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

import polyecho.cli.doctor as doctor_mod
from polyecho.cli.__main__ import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# _check_config
# ---------------------------------------------------------------------------


class TestCheckConfig:
    def test_config_ok(self) -> None:
        """Settings load + all required URLs present -> ok=True."""
        fake_settings = MagicMock()
        fake_settings.db_url = "postgresql+asyncpg://u:p@h:5432/db"
        fake_settings.polymarket_clob_url = "https://clob.example"
        fake_settings.polymarket_data_api_url = "https://data.example"
        fake_settings.log_level = "INFO"

        with patch.object(doctor_mod, "get_settings", return_value=fake_settings):
            result = doctor_mod._check_config()

        assert result.ok is True
        assert result.critical is True
        assert "asyncpg" in result.detail
        assert "INFO" in result.detail

    def test_config_missing_env_vars(self) -> None:
        """Empty required URLs -> ok=False and the var names are listed."""
        fake_settings = MagicMock()
        fake_settings.db_url = ""
        fake_settings.polymarket_clob_url = ""
        fake_settings.polymarket_data_api_url = ""
        fake_settings.log_level = "INFO"

        with patch.object(doctor_mod, "get_settings", return_value=fake_settings):
            result = doctor_mod._check_config()

        assert result.ok is False
        assert "POLYECHO_DB_URL" in result.detail
        assert "POLYECHO_POLYMARKET_CLOB_URL" in result.detail
        assert "POLYECHO_POLYMARKET_DATA_API_URL" in result.detail

    def test_config_load_failure(self) -> None:
        """get_settings() raising -> ok=False, message captured, no traceback."""
        with patch.object(
            doctor_mod, "get_settings", side_effect=RuntimeError("boom")
        ):
            result = doctor_mod._check_config()

        assert result.ok is False
        assert "Failed to load settings" in result.detail
        assert "boom" in result.detail


# ---------------------------------------------------------------------------
# _check_db  (async SELECT 1)
# ---------------------------------------------------------------------------


def _fake_engine(connect_cm: Any) -> MagicMock:
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    return engine


class TestCheckDb:
    def test_db_ok(self) -> None:
        """A successful SELECT 1 -> ok=True with latency in detail."""
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=None)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "polyecho.shared.db.get_engine", return_value=_fake_engine(conn)
        ):
            result = doctor_mod._check_db()

        assert result.ok is True
        assert result.critical is True
        assert "SELECT 1 OK" in result.detail

    def test_db_down_returns_failure(self) -> None:
        """Connection raising -> ok=False, critical=True (drives exit 1)."""
        conn = MagicMock()
        conn.__aenter__ = AsyncMock(side_effect=OSError("connection refused"))
        conn.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "polyecho.shared.db.get_engine", return_value=_fake_engine(conn)
        ):
            result = doctor_mod._check_db()

        assert result.ok is False
        assert result.critical is True
        assert "Connection failed" in result.detail
        assert "connection refused" in result.detail


# ---------------------------------------------------------------------------
# _check_polymarket
# ---------------------------------------------------------------------------


class _FakeClient:
    """Context-manager stand-in for httpx.Client."""

    def __init__(self, *, resp: Any = None, exc: Exception | None = None) -> None:
        self._resp = resp
        self._exc = exc

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def get(self, url: str) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._resp


def _settings_with_clob() -> MagicMock:
    s = MagicMock()
    s.polymarket_clob_url = "https://clob.example"
    return s


class TestCheckPolymarket:
    def test_polymarket_reachable(self) -> None:
        """2xx response -> ok=True, non-critical."""
        resp = MagicMock()
        resp.is_success = True
        resp.status_code = 200

        with (
            patch.object(
                doctor_mod, "get_settings", return_value=_settings_with_clob()
            ),
            patch("httpx.Client", lambda timeout=0: _FakeClient(resp=resp)),
        ):
            result = doctor_mod._check_polymarket()

        assert result.ok is True
        assert result.critical is False
        assert "HTTP 200" in result.detail

    def test_polymarket_http_error(self) -> None:
        """Non-2xx -> ok=False but still non-critical (warning only)."""
        resp = MagicMock()
        resp.is_success = False
        resp.status_code = 503

        with (
            patch.object(
                doctor_mod, "get_settings", return_value=_settings_with_clob()
            ),
            patch("httpx.Client", lambda timeout=0: _FakeClient(resp=resp)),
        ):
            result = doctor_mod._check_polymarket()

        assert result.ok is False
        assert result.critical is False
        assert "HTTP 503" in result.detail

    def test_polymarket_connect_error(self) -> None:
        """httpx.ConnectError -> friendly 'Network unreachable' message."""
        with (
            patch.object(
                doctor_mod, "get_settings", return_value=_settings_with_clob()
            ),
            patch(
                "httpx.Client",
                lambda timeout=0: _FakeClient(exc=httpx.ConnectError("no route")),
            ),
        ):
            result = doctor_mod._check_polymarket()

        assert result.ok is False
        assert result.critical is False
        assert "Network unreachable" in result.detail

    def test_polymarket_unexpected_error(self) -> None:
        """Any other exception -> ok=False, message surfaced, non-critical."""
        with (
            patch.object(
                doctor_mod, "get_settings", return_value=_settings_with_clob()
            ),
            patch(
                "httpx.Client",
                lambda timeout=0: _FakeClient(exc=ValueError("weird")),
            ),
        ):
            result = doctor_mod._check_polymarket()

        assert result.ok is False
        assert result.critical is False
        assert "Unexpected error" in result.detail


# ---------------------------------------------------------------------------
# _check_tailnet
# ---------------------------------------------------------------------------


class TestCheckTailnet:
    def test_tailnet_running(self) -> None:
        """BackendState=Running -> ok=True with hostname/dns in detail."""
        payload = {
            "BackendState": "Running",
            "Self": {"HostName": "node-1", "DNSName": "node-1.tailnet.ts.net."},
        }
        completed = subprocess.CompletedProcess(
            args=["tailscale"], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            result = doctor_mod._check_tailnet()

        assert result.ok is True
        assert result.critical is False
        assert "node-1" in result.detail
        assert "node-1.tailnet.ts.net" in result.detail

    def test_tailnet_not_installed(self) -> None:
        """tailscale binary missing -> graceful non-critical failure."""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = doctor_mod._check_tailnet()

        assert result.ok is False
        assert result.critical is False
        assert "not found" in result.detail.lower()

    def test_tailnet_timeout(self) -> None:
        """tailscale status hang -> TimeoutExpired handled."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            result = doctor_mod._check_tailnet()

        assert result.ok is False
        assert result.critical is False
        assert "timed out" in result.detail.lower()

    def test_tailnet_nonzero_exit(self) -> None:
        """Non-zero return code -> stderr surfaced, non-critical."""
        completed = subprocess.CompletedProcess(
            args=["tailscale"], returncode=1, stdout="", stderr="not logged in"
        )
        with patch("subprocess.run", return_value=completed):
            result = doctor_mod._check_tailnet()

        assert result.ok is False
        assert result.critical is False
        assert "not logged in" in result.detail

    def test_tailnet_bad_json(self) -> None:
        """Unparseable JSON -> handled without raising."""
        completed = subprocess.CompletedProcess(
            args=["tailscale"], returncode=0, stdout="<<<not json>>>", stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            result = doctor_mod._check_tailnet()

        assert result.ok is False
        assert result.critical is False
        assert "parse" in result.detail.lower()

    def test_tailnet_stopped_state(self) -> None:
        """BackendState != Running -> reported as not-on-tailnet."""
        payload = {"BackendState": "Stopped", "Self": {}}
        completed = subprocess.CompletedProcess(
            args=["tailscale"], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            result = doctor_mod._check_tailnet()

        assert result.ok is False
        assert result.critical is False
        assert "Stopped" in result.detail


# ---------------------------------------------------------------------------
# doctor() command — exit-code contract
# ---------------------------------------------------------------------------


def _ok(name: str, *, critical: bool = True) -> Any:
    return doctor_mod._CheckResult(name, ok=True, detail="ok", critical=critical)


def _fail(name: str, *, critical: bool) -> Any:
    return doctor_mod._CheckResult(name, ok=False, detail="bad", critical=critical)


class TestDoctorCommandExitCodes:
    def test_all_checks_pass_exit_zero(self) -> None:
        with (
            patch.object(doctor_mod, "_check_config", return_value=_ok("Config")),
            patch.object(doctor_mod, "_check_db", return_value=_ok("Database")),
            patch.object(
                doctor_mod,
                "_check_polymarket",
                return_value=_ok("Polymarket API", critical=False),
            ),
            patch.object(
                doctor_mod,
                "_check_tailnet",
                return_value=_ok("Tailnet", critical=False),
            ),
        ):
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0, result.output
        assert "All checks passed" in result.output

    def test_db_down_exits_one(self) -> None:
        """DB critical failure must drive a non-zero exit (exit 1)."""
        with (
            patch.object(doctor_mod, "_check_config", return_value=_ok("Config")),
            patch.object(
                doctor_mod,
                "_check_db",
                return_value=_fail("Database", critical=True),
            ),
            patch.object(
                doctor_mod,
                "_check_polymarket",
                return_value=_ok("Polymarket API", critical=False),
            ),
            patch.object(
                doctor_mod,
                "_check_tailnet",
                return_value=_ok("Tailnet", critical=False),
            ),
        ):
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 1, result.output
        assert "critical checks failed" in result.output.lower()

    def test_config_down_exits_one(self) -> None:
        """Config critical failure must also drive exit 1."""
        with (
            patch.object(
                doctor_mod,
                "_check_config",
                return_value=_fail("Config", critical=True),
            ),
            patch.object(doctor_mod, "_check_db", return_value=_ok("Database")),
            patch.object(
                doctor_mod,
                "_check_polymarket",
                return_value=_ok("Polymarket API", critical=False),
            ),
            patch.object(
                doctor_mod,
                "_check_tailnet",
                return_value=_ok("Tailnet", critical=False),
            ),
        ):
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 1, result.output

    def test_only_noncritical_warnings_exit_zero(self) -> None:
        """Polymarket/Tailnet down (non-critical) -> exit 0 with warning text."""
        with (
            patch.object(doctor_mod, "_check_config", return_value=_ok("Config")),
            patch.object(doctor_mod, "_check_db", return_value=_ok("Database")),
            patch.object(
                doctor_mod,
                "_check_polymarket",
                return_value=_fail("Polymarket API", critical=False),
            ),
            patch.object(
                doctor_mod,
                "_check_tailnet",
                return_value=_fail("Tailnet", critical=False),
            ),
        ):
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0, result.output
        assert "operational" in result.output.lower()

    def test_secrets_never_printed(self) -> None:
        """The rendered table must not leak secret-looking values."""
        secret = "super-secret-private-key-value"
        cfg = doctor_mod._CheckResult(
            "Config", ok=True, detail="driver=asyncpg log_level=INFO"
        )
        with (
            patch.object(doctor_mod, "_check_config", return_value=cfg),
            patch.object(doctor_mod, "_check_db", return_value=_ok("Database")),
            patch.object(
                doctor_mod,
                "_check_polymarket",
                return_value=_ok("Polymarket API", critical=False),
            ),
            patch.object(
                doctor_mod,
                "_check_tailnet",
                return_value=_ok("Tailnet", critical=False),
            ),
        ):
            result = runner.invoke(app, ["doctor"])

        assert secret not in result.output


# ---------------------------------------------------------------------------
# _CheckResult.status_icon
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ok", "critical", "expected"),
    [
        (True, True, doctor_mod._OK),
        (False, True, doctor_mod._FAIL),
        (False, False, doctor_mod._WARN),
    ],
)
def test_check_result_status_icon(ok: bool, critical: bool, expected: str) -> None:
    r = doctor_mod._CheckResult("X", ok=ok, detail="", critical=critical)
    assert r.status_icon == expected

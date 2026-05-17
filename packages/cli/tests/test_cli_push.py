"""
Tests for ``polyecho push vapid-keygen`` — Phase-5 Wave-2.

Design
------
* importlib mode (no ``tests/__init__.py``); Typer ``CliRunner``.
* No network / no DB — ``generate_vapid_keypair`` is real (pure crypto,
  fast) so we exercise the frozen shared contract end-to-end.
* Asserts: exit 0; all three ``POLYECHO_VAPID_*=`` lines on stdout; the
  secret WARNING is shown; two invocations yield *different* keypairs;
  nothing is written to the working directory / disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


def _env_value(output: str, key: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{key}= line not found in output:\n{output}")


class TestVapidKeygen:
    def test_prints_three_env_lines_and_secret_warning(self) -> None:
        result = runner.invoke(
            app, ["push", "vapid-keygen"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.output
        assert "POLYECHO_VAPID_PUBLIC_KEY=" in result.output
        assert "POLYECHO_VAPID_PRIVATE_KEY=" in result.output
        assert "POLYECHO_VAPID_SUBJECT=" in result.output

        pub = _env_value(result.output, "POLYECHO_VAPID_PUBLIC_KEY")
        priv = _env_value(result.output, "POLYECHO_VAPID_PRIVATE_KEY")
        subject = _env_value(result.output, "POLYECHO_VAPID_SUBJECT")
        assert pub and priv and pub != priv
        assert subject.startswith(("mailto:", "https:"))

        # Blunt secret warning is present.
        assert "SECRET" in result.output
        assert "VAPID PRIVATE KEY" in result.output
        assert "Never" in result.output
        # Re-subscribe consequence is spelled out.
        assert "re-subscribe" in result.output

    def test_two_invocations_yield_different_keypairs(self) -> None:
        r1 = runner.invoke(app, ["push", "vapid-keygen"])
        r2 = runner.invoke(app, ["push", "vapid-keygen"])
        assert r1.exit_code == 0 and r2.exit_code == 0
        assert _env_value(r1.output, "POLYECHO_VAPID_PUBLIC_KEY") != _env_value(
            r2.output, "POLYECHO_VAPID_PUBLIC_KEY"
        )
        assert _env_value(
            r1.output, "POLYECHO_VAPID_PRIVATE_KEY"
        ) != _env_value(r2.output, "POLYECHO_VAPID_PRIVATE_KEY")

    def test_writes_nothing_to_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        before = set(os.listdir(tmp_path))
        result = runner.invoke(app, ["push", "vapid-keygen"])
        assert result.exit_code == 0, result.output
        after = set(os.listdir(tmp_path))
        assert before == after, f"keygen created files: {after - before}"

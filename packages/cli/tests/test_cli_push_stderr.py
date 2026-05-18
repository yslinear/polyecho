"""
P3 hardening: ``polyecho push vapid-keygen`` writes the PRIVATE KEY to
**stderr**, never stdout (Wave C-cli).

Why
---
A stdout redirect (``polyecho push vapid-keygen > keys.env``) or a CI step
that captures stdout must NOT be able to swallow the VAPID private key
into a file/log.  Public key + subject stay on stdout (safe to capture);
the private key + its secret warning ride stderr.

Click >= 8.2 always keeps ``result.stdout`` / ``result.stderr``
independent (the old ``mix_stderr`` arg was removed), so we assert each
stream independently with a plain ``CliRunner``.
"""

from __future__ import annotations

from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()


def _line_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


class TestVapidKeygenStreams:
    def test_private_key_on_stderr_not_stdout(self) -> None:
        result = runner.invoke(
            app, ["push", "vapid-keygen"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0, result.output

        out = result.stdout
        err = result.stderr

        # Public key + subject on STDOUT (safe to capture / `> .env`).
        assert "POLYECHO_VAPID_PUBLIC_KEY=" in out
        assert "POLYECHO_VAPID_SUBJECT=" in out

        # The PRIVATE KEY line must be on STDERR and NOT on STDOUT.
        assert "POLYECHO_VAPID_PRIVATE_KEY=" in err
        assert "POLYECHO_VAPID_PRIVATE_KEY=" not in out

        # The actual secret value never appears on stdout.
        priv = _line_value(err, "POLYECHO_VAPID_PRIVATE_KEY")
        assert priv, "private key value missing from stderr"
        assert priv not in out

        # The blunt SECRET warning rides stderr with the key.
        assert "SECRET" in err
        assert "VAPID PRIVATE KEY" in err

    def test_stdout_alone_is_safe_to_capture(self) -> None:
        """Simulate ``> file``: only stdout is captured — it must contain
        NO private-key material at all."""
        result = runner.invoke(
            app, ["push", "vapid-keygen"], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 0
        out = result.stdout
        assert "PRIVATE_KEY" not in out
        # Public key still usable from the captured stdout.
        assert _line_value(out, "POLYECHO_VAPID_PUBLIC_KEY")

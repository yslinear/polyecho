"""
Tests for ``polyecho alerts list`` — Phase-5 Wave-2 push-event viewer.

Design
------
* importlib mode (no ``tests/__init__.py``); Typer ``CliRunner``.
* NO real DB: ``session_scope`` is a fake async-CM that records the
  executed statement and serves canned ``Event`` rows.
* Asserts: rows render; ``--severity`` / ``--unpushed`` add the right
  WHERE; ``--limit`` is forwarded; invalid severity exits non-zero;
  empty result prints a graceful "no events" message; the delivered
  marker reflects ``pushed_at``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from polyecho.cli import alerts as alerts_mod
from polyecho.cli.__main__ import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper unit tests — exercise the formatting / summary branches directly.
# ---------------------------------------------------------------------------
class TestAlertHelpers:
    def test_fmt_local_accepts_iso_string_and_renders_tz(self) -> None:
        tz = alerts_mod._tzinfo()
        out = alerts_mod._fmt_local("2026-05-18T00:00:00+00:00", tz)
        assert "2026-05-" in out  # rendered, not the raw ISO string

    def test_fmt_local_handles_naive_and_garbage_and_none(self) -> None:
        tz = alerts_mod._tzinfo()
        assert alerts_mod._fmt_local(None, tz) == "-"
        assert alerts_mod._fmt_local("not-a-date", tz) == "not-a-date"
        # Naive datetime is assumed UTC then converted (no crash).
        naive = datetime(2026, 5, 18, 0, 0, 0)
        assert "2026-05-" in alerts_mod._fmt_local(naive, tz)

    def test_tzinfo_falls_back_to_utc_on_bad_name(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("POLYECHO_TZ", "Not/AZone")
        try:
            assert str(alerts_mod._tzinfo()) == "UTC"
        finally:
            monkeypatch.delenv("POLYECHO_TZ", raising=False)
            get_settings.cache_clear()

    def test_summarise_branches(self) -> None:
        assert alerts_mod._summarise(None) == "-"
        assert alerts_mod._summarise({"data": None}) == "-"
        assert alerts_mod._summarise({"data": {}}) == "-"
        assert alerts_mod._summarise({"data": "hello"}) == "hello"
        assert "a=1" in alerts_mod._summarise({"data": {"a": 1, "b": 2}})


# ---------------------------------------------------------------------------
# Fake session_scope — records executed statements + serves canned rows.
# ---------------------------------------------------------------------------
def _make_scope(*, scalars_list: list[Any] | None = None) -> Any:
    captured: dict[str, Any] = {"executed": []}

    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock()

        async def fake_execute(stmt: Any) -> MagicMock:
            captured["executed"].append(stmt)
            result = MagicMock()
            scalars_obj = MagicMock()
            scalars_obj.all.return_value = (
                scalars_list if scalars_list is not None else []
            )
            result.scalars.return_value = scalars_obj
            return result

        session.execute = fake_execute
        captured["session"] = session
        yield session

    fake_scope._captured = captured  # type: ignore[attr-defined]
    return fake_scope


def _event(
    *,
    severity: str = "info",
    event_type: str = "copy_fill",
    payload: Any = None,
    pushed: bool = False,
) -> MagicMock:
    e = MagicMock()
    e.created_at = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)
    e.severity = severity
    e.event_type = event_type
    e.payload = payload
    e.pushed_at = datetime(2026, 5, 18, 12, 0, 5, tzinfo=UTC) if pushed else None
    return e


class TestAlertsList:
    def test_renders_rows_with_summary_and_delivered(self) -> None:
        rows = [
            _event(
                severity="critical",
                event_type="risk_daily_loss",
                payload={"data": {"pnl": -42.5, "limit": -40}, "push": {}},
                pushed=True,
            ),
            _event(
                severity="info",
                event_type="copy_fill",
                payload={"data": {"size": 10}},
                pushed=False,
            ),
        ]
        scope = _make_scope(scalars_list=rows)
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["alerts", "list"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "risk_daily_loss" in result.output
        assert "copy_fill" in result.output
        assert "pnl=-42.5" in result.output
        assert "✓" in result.output  # delivered
        assert "✗" in result.output  # not delivered

    def test_empty_prints_graceful_message(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["alerts", "list"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "No events recorded" in result.output

    def test_severity_filter_adds_where(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["alerts", "list", "--severity", "critical"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert "critical" in result.output
        compiled = str(scope._captured["executed"][0]).lower()
        assert "where" in compiled
        assert "severity" in compiled

    def test_unpushed_filter_adds_where(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["alerts", "list", "--unpushed"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        assert "unpushed" in result.output
        compiled = str(scope._captured["executed"][0]).lower()
        assert "where" in compiled
        assert "pushed_at is null" in compiled

    def test_limit_is_forwarded(self) -> None:
        scope = _make_scope(scalars_list=[])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app,
                ["alerts", "list", "-n", "5"],
                env={"COLUMNS": "200"},
            )
        assert result.exit_code == 0, result.output
        compiled = str(
            scope._captured["executed"][0].compile(
                compile_kwargs={"literal_binds": True}
            )
        ).lower()
        assert "limit 5" in compiled

    def test_invalid_severity_exits_nonzero(self) -> None:
        result = runner.invoke(
            app,
            ["alerts", "list", "--severity", "bogus"],
            env={"COLUMNS": "200"},
        )
        assert result.exit_code != 0
        assert "Invalid --severity" in result.output

    def test_db_error_exits_nonzero(self) -> None:
        @asynccontextmanager
        async def boom() -> AsyncGenerator[Any, None]:
            raise RuntimeError("connection refused")
            yield  # pragma: no cover

        with patch("polyecho.shared.db.session_scope", boom):
            result = runner.invoke(app, ["alerts", "list"])
        assert result.exit_code != 0
        assert "Could not query events" in result.output

    def test_large_payload_summary_is_truncated(self) -> None:
        big = {"data": {"blob": "x" * 500}, "push": {}}
        scope = _make_scope(scalars_list=[_event(payload=big)])
        with patch("polyecho.shared.db.session_scope", scope):
            result = runner.invoke(
                app, ["alerts", "list"], env={"COLUMNS": "200"}
            )
        assert result.exit_code == 0, result.output
        assert "…" in result.output
        assert "x" * 500 not in result.output

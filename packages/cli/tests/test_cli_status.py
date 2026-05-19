"""
Tests for ``polyecho status`` — aggregated system-status command.

Design
------
* importlib mode (no ``__init__.py`` in tests/).
* NO real DB / network: the frozen aggregator
  ``polyecho.shared.health.get_status`` is patched (it is an ``async``
  coroutine, so it is patched with an :class:`~unittest.mock.AsyncMock`,
  exactly as ``test_cli_doctor`` mocks async touch-points); we assert
  how its ``StatusSnapshot`` is *rendered* (workers + integrations) and
  the read-only / exit-code contract.
* Fixtures are built from the **real frozen models**
  (``WorkerHeartbeat`` / ``ProbeResult`` / ``IntegrationSnapshot`` /
  ``StatusSnapshot``) so this verifies ``status.py`` against the actual
  ``polyecho.shared.health`` contract — name is the *dict key*, worker
  liveness is the heartbeat's own 5-min ``is_stale`` verdict, and the
  integration row is ``StatusSnapshot.integration`` (singular) with a
  name-keyed ``.probes`` mapping.
* ``status`` is observability-only: it must ALWAYS exit 0 on a good
  snapshot (never a gate) and exit 1 *only* if the aggregator itself
  raises — and then with a clean message, no traceback.
* The command must never trip the live-trading lock: it only calls
  ``get_status`` (a pure read) and touches nothing else.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from click.testing import Result
from typer.testing import CliRunner

from polyecho.cli.__main__ import app
from polyecho.shared.health import (
    IntegrationSnapshot,
    ProbeResult,
    StatusSnapshot,
    WorkerHeartbeat,
)

runner = CliRunner()


def _invoke(args: list[str]) -> Result:
    """Invoke the CLI with a wide terminal.

    The ``status`` tables are intentionally compact, but Rich elides cell
    text to the *terminal* width; CliRunner defaults to 80 cols which
    truncates multi-column rows.  Pinning a wide ``COLUMNS`` makes these
    assertions deterministic without changing production rendering (real
    terminals/CI are wide; the layout itself is unchanged).
    """
    return runner.invoke(app, args, env={"COLUMNS": "200"})


def _patch_status(
    snapshot: StatusSnapshot,
) -> AbstractContextManager[AsyncMock]:
    """Patch the frozen *async* aggregator to return ``snapshot``.

    ``get_status`` is ``async def`` so it must be patched with an
    ``AsyncMock`` — a plain return value would hand ``status`` an
    un-awaited coroutine.
    """
    return patch(
        "polyecho.shared.health.get_status",
        new=AsyncMock(return_value=snapshot),
    )


def _fresh_ts() -> str:
    """An ISO ts well inside the 5-minute window (worker reads running)."""
    return datetime.now(tz=UTC).isoformat()


def _stale_ts() -> str:
    """An ISO ts older than HEARTBEAT_STALE_SECONDS (worker reads stale)."""
    return (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()


# ---------------------------------------------------------------------------
# Command wiring + exit contract
# ---------------------------------------------------------------------------


class TestStatusCommandContract:
    def test_status_is_registered_top_level(self) -> None:
        """`polyecho --help` lists `status` as a top-level command."""
        result = _invoke(["--help"])
        assert result.exit_code == 0
        assert "status" in result.output

    def test_empty_snapshot_exits_zero(self) -> None:
        """No workers / no integration is still a valid status -> exit 0."""
        with _patch_status(StatusSnapshot(workers={}, integration=None)):
            result = _invoke(["status"])

        assert result.exit_code == 0, result.output
        assert "System Status" in result.output
        assert "Workers" in result.output
        assert "Integrations" in result.output

    def test_aggregator_error_exits_one_cleanly(self) -> None:
        """If get_status() raises, exit 1 with a clean message (no traceback)."""
        with patch(
            "polyecho.shared.health.get_status",
            new=AsyncMock(side_effect=RuntimeError("db pool exhausted")),
        ):
            result = _invoke(["status"])

        assert result.exit_code == 1
        assert "Could not read system status" in result.output
        assert "db pool exhausted" in result.output
        assert "Traceback" not in result.output

    def test_status_only_calls_get_status_read_only(self) -> None:
        """The command's sole side-effect is the get_status() read.

        Proven by: get_status is awaited exactly once and the command
        succeeds without importing/calling any write/live path.
        """
        mocked = AsyncMock(
            return_value=StatusSnapshot(workers={}, integration=None)
        )
        with patch("polyecho.shared.health.get_status", new=mocked):
            result = _invoke(["status"])

        assert result.exit_code == 0
        mocked.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# Worker rendering — running / stale / down + fields
# ---------------------------------------------------------------------------


class TestWorkerRendering:
    def test_running_worker_fields_rendered(self) -> None:
        """A fresh heartbeat renders running + its phase/datasource/counters."""
        snap = StatusSnapshot(
            workers={
                "trader-core": WorkerHeartbeat(
                    ts=_fresh_ts(),
                    phase="copying",
                    datasource="fixture",
                    counters={"copied": 7, "skipped": 2},
                )
            },
            integration=None,
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0, result.output
        out = result.output
        assert "trader-core" in out
        assert "running" in out
        assert "copying" in out
        assert "fixture" in out
        assert "copied=7" in out
        assert "skipped=2" in out

    def test_stale_worker_labelled_stale(self) -> None:
        """A heartbeat older than the 5-min rule -> the STALE label."""
        snap = StatusSnapshot(
            workers={"discovery": WorkerHeartbeat(ts=_stale_ts())},
            integration=None,
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0
        assert "discovery" in result.output
        assert "stale" in result.output

    def test_down_worker_labelled_down(self) -> None:
        """An absent heartbeat (None in the workers map) -> down."""
        snap = StatusSnapshot(
            workers={"push-sender": None},
            integration=None,
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0
        assert "push-sender" in result.output
        assert "down" in result.output

    def test_string_state_only_worker(self) -> None:
        """A duck-typed worker exposing only a string ``state`` renders sanely.

        Not the frozen shape, but ``status`` keeps a defensive fallback so
        an already-serialised / loosely-typed snapshot still renders.
        """
        snap = StatusSnapshot.model_construct(
            generated_at=datetime.now(tz=UTC).isoformat(),
            workers={"backtest": {"state": "running", "phase": "idle"}},
            integration=None,
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0
        assert "backtest" in result.output
        assert "running" in result.output


# ---------------------------------------------------------------------------
# Integration rendering — ok / down + detail / latency / checked-at
# ---------------------------------------------------------------------------


class TestIntegrationRendering:
    def test_ok_and_down_integrations(self) -> None:
        """The name-keyed ``integration.probes`` map renders ok + down rows."""
        snap = StatusSnapshot(
            workers={},
            integration=IntegrationSnapshot(
                checked_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC).isoformat(),
                probes={
                    "polymarket_clob": ProbeResult(
                        ok=True,
                        detail="HTTP 200",
                        latency_ms=42.0,
                        checked_at=datetime(
                            2026, 5, 19, 12, 0, 0, tzinfo=UTC
                        ).isoformat(),
                    ),
                    "polygon_rpc": ProbeResult(
                        ok=False,
                        detail="timeout",
                        latency_ms=None,
                        checked_at=None,
                    ),
                },
            ),
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0, result.output
        out = result.output
        assert "polymarket_clob" in out
        assert "HTTP 200" in out
        assert "42 ms" in out
        assert "polygon_rpc" in out
        assert "timeout" in out
        assert "down" in out

    def test_new_ws_probes_render_generically(self) -> None:
        """The non-frozen WS probes merged by trader-core render generically.

        ``status`` iterates ``integration.probes`` by key, so the two new
        ``polymarket_ws_*`` probes appear with no per-probe code.
        """
        snap = StatusSnapshot(
            workers={},
            integration=IntegrationSnapshot(
                checked_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC).isoformat(),
                probes={
                    "polymarket_ws_user": ProbeResult(
                        ok=True, detail="ws user up", latency_ms=15.0
                    ),
                    "polymarket_ws_market": ProbeResult(
                        ok=False, detail="ws market down"
                    ),
                },
            ),
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0, result.output
        out = result.output
        assert "polymarket_ws_user" in out
        assert "ws user up" in out
        assert "polymarket_ws_market" in out
        assert "ws market down" in out

    def test_dict_integration_supported(self) -> None:
        """Mapping-shaped probe entries are rendered too (defensive path)."""
        snap = StatusSnapshot.model_construct(
            generated_at=datetime.now(tz=UTC).isoformat(),
            workers={},
            integration={
                "checked_at": None,
                "probes": {
                    "vpn_egress": {
                        "ok": True,
                        "detail": "egress ok",
                        "latency_ms": 9,
                    }
                },
            },
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0
        assert "vpn_egress" in result.output
        assert "egress ok" in result.output

    def test_snapshot_generated_at_footer(self) -> None:
        """The ``generated_at`` field renders as the snapshot footer."""
        snap = StatusSnapshot(
            generated_at=datetime(2026, 5, 19, 12, 30, 0, tzinfo=UTC).isoformat(),
            workers={},
            integration=None,
        )
        with _patch_status(snap):
            result = _invoke(["status"])

        assert result.exit_code == 0
        assert "Snapshot as of" in result.output


# ---------------------------------------------------------------------------
# Snapshot collection coercion (dict-keyed snapshots)
# ---------------------------------------------------------------------------


def test_dict_keyed_collections_are_coerced() -> None:
    """workers / probes delivered as plain dicts (keyed by name) still render.

    The frozen contract IS dict-keyed-by-name; this pins that ``status``
    preserves the key as the displayed name even for loosely-typed values.
    """
    snap = StatusSnapshot.model_construct(
        generated_at=datetime.now(tz=UTC).isoformat(),
        workers={"trader": {"state": "running"}},
        integration={
            "checked_at": None,
            "probes": {"clob": {"ok": True, "detail": "ok"}},
        },
    )
    with _patch_status(snap):
        result = _invoke(["status"])

    assert result.exit_code == 0, result.output
    assert "trader" in result.output
    assert "clob" in result.output

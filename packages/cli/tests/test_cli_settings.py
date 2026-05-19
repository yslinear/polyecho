"""
Tests for ``polyecho settings proxy-show / proxy-set / proxy-clear`` — the
CLI half of the Polymarket-proxy control, mirroring the web ``/settings``.

Design
------
* importlib mode (no ``tests/__init__.py``); Typer ``CliRunner``.
* NO real DB / network: ``session_scope`` is a fake async-CM over an
  in-memory ``settings`` store that supports select / add / flush / delete
  using the REAL ``Setting`` ORM + ``select()``/``delete()`` (so we never
  patch ``Setting``).
* Asserts: ``proxy-set`` writes the bare-string row; ``proxy-show``
  reflects it and reports the source (DB vs env vs direct); ``proxy-clear``
  deletes it; SSRF / SOCKS / bad-scheme URLs are rejected with a non-zero
  exit and **no DB write**; the persisted value is the exact shape the
  web/runtime resolver reads (single source of truth).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import patch

from typer.testing import CliRunner

from polyecho.cli.__main__ import app

runner = CliRunner()

_PROXY_KEY = "polymarket_proxy_url"


# ---------------------------------------------------------------------------
# In-memory ``settings`` store shared by a fake ``session_scope``.
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, key: str, value: Any) -> None:
        self.key = key
        self.value = value


class _Store:
    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = dict(initial or {})

    def add(self, obj: Any) -> None:
        self.data[obj.key] = obj

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        params = stmt.compile().params
        key = next(iter(params.values())) if params else None
        if stmt.__class__.__name__.lower().startswith("delete"):
            self.data.pop(cast(str, key), None)

            class _Empty:
                def scalar_one_or_none(self) -> Any:
                    return None

            return _Empty()
        row = self.data.get(cast(str, key))

        class _R:
            def scalar_one_or_none(self) -> Any:
                return row

        return _R()


def _scope_for(store: _Store) -> Any:
    @asynccontextmanager
    async def fake_scope() -> AsyncGenerator[_Store, None]:
        yield store

    return fake_scope


def _patch_db(store: _Store) -> Any:
    """Patch the ``session_scope`` symbol both helpers import."""
    return patch("polyecho.shared.db.session_scope", _scope_for(store))


# ---------------------------------------------------------------------------
# proxy-set
# ---------------------------------------------------------------------------
class TestProxySet:
    def test_set_writes_bare_string_row(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "http://203.0.113.10:8080"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 0, res.output
        assert list(store.data) == [_PROXY_KEY]
        assert store.data[_PROXY_KEY].value == "http://203.0.113.10:8080"
        assert "Proxy saved" in res.output

    def test_set_redacts_credentials_in_output(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                [
                    "settings",
                    "proxy-set",
                    "http://user:supersecret@proxy.example.com:8080",
                ],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 0, res.output
        assert "supersecret" not in res.output
        assert "user" not in res.output
        # But the FULL value (with creds) is what gets persisted.
        assert (
            store.data[_PROXY_KEY].value
            == "http://user:supersecret@proxy.example.com:8080"
        )

    def test_set_updates_existing_row(self) -> None:
        store = _Store({_PROXY_KEY: _Row(_PROXY_KEY, "http://old:1")})
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "https://new.example:3128"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 0, res.output
        assert list(store.data) == [_PROXY_KEY]
        assert store.data[_PROXY_KEY].value == "https://new.example:3128"

    def test_set_rejects_socks_no_write(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "socks5://1.2.3.4:1080"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 2, res.output
        assert "SOCKS" in res.output
        assert store.data == {}  # nothing written

    def test_set_rejects_loopback_no_write(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "http://127.0.0.1:8080"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 2, res.output
        assert "Rejected" in res.output
        assert store.data == {}

    def test_set_rejects_cloud_metadata_no_write(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "http://169.254.169.254/latest"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 2, res.output
        assert store.data == {}

    def test_set_rejects_non_http_scheme_no_write(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "ftp://1.2.3.4"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 2, res.output
        assert store.data == {}

    def test_set_rejects_rfc1918_no_write(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app,
                ["settings", "proxy-set", "http://192.168.1.1:3128"],
                env={"COLUMNS": "200"},
            )
        assert res.exit_code == 2, res.output
        assert store.data == {}


# ---------------------------------------------------------------------------
# proxy-clear
# ---------------------------------------------------------------------------
class TestProxyClear:
    def test_clear_deletes_existing_row(self) -> None:
        store = _Store({_PROXY_KEY: _Row(_PROXY_KEY, "http://203.0.113.10:8080")})
        with _patch_db(store):
            res = runner.invoke(
                app, ["settings", "proxy-clear"], env={"COLUMNS": "200"}
            )
        assert res.exit_code == 0, res.output
        assert _PROXY_KEY not in store.data
        assert "direct" in res.output

    def test_clear_idempotent_when_missing(self) -> None:
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app, ["settings", "proxy-clear"], env={"COLUMNS": "200"}
            )
        assert res.exit_code == 0, res.output
        assert store.data == {}


# ---------------------------------------------------------------------------
# proxy-show — source attribution (DB row vs env fallback vs direct)
# ---------------------------------------------------------------------------
class TestProxyShow:
    def test_show_direct_when_no_row_no_env(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
        get_settings.cache_clear()
        store = _Store()
        with _patch_db(store):
            res = runner.invoke(
                app, ["settings", "proxy-show"], env={"COLUMNS": "200"}
            )
        assert res.exit_code == 0, res.output
        assert "direct" in res.output.lower()
        get_settings.cache_clear()

    def test_show_reports_db_source_and_redacts(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        # No env proxy → a resolved value can only be the DB row.
        monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
        get_settings.cache_clear()
        store = _Store(
            {
                _PROXY_KEY: _Row(
                    _PROXY_KEY, "http://user:sekret@p.example.com:8080"
                )
            }
        )
        with _patch_db(store):
            res = runner.invoke(
                app, ["settings", "proxy-show"], env={"COLUMNS": "200"}
            )
        assert res.exit_code == 0, res.output
        assert "DB" in res.output  # attributed to the settings row
        assert "sekret" not in res.output  # redacted
        get_settings.cache_clear()

    def test_show_reports_env_source_when_no_db_row(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        monkeypatch.setenv(
            "POLYECHO_POLYMARKET_PROXY_URL", "http://envproxy:7777"
        )
        get_settings.cache_clear()
        store = _Store()  # no DB row → env fallback
        with _patch_db(store):
            res = runner.invoke(
                app, ["settings", "proxy-show"], env={"COLUMNS": "200"}
            )
        assert res.exit_code == 0, res.output
        assert "env" in res.output.lower()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# End-to-end single-source-of-truth: set via CLI → show via CLI reflects it.
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_set_then_show_reflects_db_value(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
        get_settings.cache_clear()
        store = _Store()
        with _patch_db(store):
            set_res = runner.invoke(
                app,
                ["settings", "proxy-set", "http://203.0.113.10:8080"],
                env={"COLUMNS": "200"},
            )
            assert set_res.exit_code == 0, set_res.output
            show_res = runner.invoke(
                app, ["settings", "proxy-show"], env={"COLUMNS": "200"}
            )
        assert show_res.exit_code == 0, show_res.output
        # The exact bare string the CLI wrote is what proxy-show resolves
        # (via get_effective_proxy_url) and what the web /settings page,
        # reading the same row + _coerce_proxy, would display.
        assert store.data[_PROXY_KEY].value == "http://203.0.113.10:8080"
        assert "203.0.113.10:8080" in show_res.output
        assert "DB" in show_res.output
        get_settings.cache_clear()

    def test_set_then_clear_then_show_is_direct(
        self, monkeypatch: Any
    ) -> None:
        from polyecho.shared.config import get_settings

        monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
        get_settings.cache_clear()
        store = _Store()
        with _patch_db(store):
            runner.invoke(
                app,
                ["settings", "proxy-set", "http://203.0.113.10:8080"],
                env={"COLUMNS": "200"},
            )
            clear_res = runner.invoke(
                app, ["settings", "proxy-clear"], env={"COLUMNS": "200"}
            )
            assert clear_res.exit_code == 0, clear_res.output
            show_res = runner.invoke(
                app, ["settings", "proxy-show"], env={"COLUMNS": "200"}
            )
        assert show_res.exit_code == 0, show_res.output
        assert _PROXY_KEY not in store.data
        assert "direct" in show_res.output.lower()
        get_settings.cache_clear()

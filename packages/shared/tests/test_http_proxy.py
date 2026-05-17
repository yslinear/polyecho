"""Tests for polyecho.shared.http — the configurable Polymarket proxy.

Covers: env-level resolution, the no-proxy "identical to before" path,
explicit-override precedence, JSONB value coercion, and the
settings-DB-overrides-env resolution (mocked — no real DB/network).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from polyecho.shared import http as shared_http
from polyecho.shared.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- get_proxy_url (env level) --------------------------------------------
def test_get_proxy_url_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()
    assert shared_http.get_proxy_url() is None


def test_get_proxy_url_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "   ")
    get_settings.cache_clear()
    assert shared_http.get_proxy_url() is None


def test_get_proxy_url_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://u:p@host:8080")
    get_settings.cache_clear()
    assert shared_http.get_proxy_url() == "http://u:p@host:8080"


# --- make_async_client ----------------------------------------------------
def test_make_async_client_no_proxy_kwarg_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class _Spy(httpx.AsyncClient):
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Spy)
    c = shared_http.make_async_client(base_url="https://x")
    assert "proxy" not in captured  # identical to bare httpx.AsyncClient
    asyncio.run(c.aclose())


def test_make_async_client_applies_env_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://px:3128")
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class _Spy(httpx.AsyncClient):
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Spy)
    c = shared_http.make_async_client()
    assert captured.get("proxy") == "http://px:3128"
    asyncio.run(c.aclose())


def test_explicit_proxy_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://env:1")
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class _Spy(httpx.AsyncClient):
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Spy)
    c = shared_http.make_async_client(proxy="http://explicit:9")
    assert captured["proxy"] == "http://explicit:9"
    asyncio.run(c.aclose())


# --- redact ---------------------------------------------------------------
def test_redact_hides_credentials() -> None:
    out = shared_http.redact_proxy_url("http://user:secret@host:8080")
    assert "secret" not in out and "user" not in out
    assert "***@host:8080" in out
    assert shared_http.redact_proxy_url(None) == "<none>"


# --- _coerce_proxy --------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://p:1", "http://p:1"),
        ("  http://p:2  ", "http://p:2"),
        ("", None),
        (None, None),
        ({"value": "http://d:3"}, "http://d:3"),
        ({"url": "http://d:4"}, "http://d:4"),
        ({"nope": "x"}, None),
        (123, None),
    ],
)
def test_coerce_proxy(value: Any, expected: str | None) -> None:
    assert shared_http._coerce_proxy(value) == expected


# --- get_effective_proxy_url (settings DB overrides env) ------------------
class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self._row = row

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._row)


def test_effective_proxy_db_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://env:1")
    get_settings.cache_clear()

    class _Row:
        value = "http://db-proxy:9000"

    result = asyncio.run(
        shared_http.get_effective_proxy_url(session=_FakeSession(_Row()))
    )
    assert result == "http://db-proxy:9000"


def test_effective_proxy_falls_back_to_env_when_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://env:2")
    get_settings.cache_clear()
    result = asyncio.run(
        shared_http.get_effective_proxy_url(session=_FakeSession(None))
    )
    assert result == "http://env:2"


def test_effective_proxy_none_when_neither(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()
    result = asyncio.run(
        shared_http.get_effective_proxy_url(session=_FakeSession(None))
    )
    assert result is None


def test_make_async_client_resolved_uses_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Spy(httpx.AsyncClient):
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Spy)

    async def _fake_resolve(session: Any = None) -> str:
        return "http://resolved:7"

    monkeypatch.setattr(shared_http, "get_effective_proxy_url", _fake_resolve)
    c = asyncio.run(shared_http.make_async_client_resolved())
    assert captured["proxy"] == "http://resolved:7"
    asyncio.run(c.aclose())

"""
tests/test_http_proxy_write.py
==============================
The CLI-side Polymarket-proxy **write** path + the shared SSRF/scheme
validator in :mod:`polyecho.shared.http`
(:func:`~polyecho.shared.http.validate_proxy_url`,
:func:`~polyecho.shared.http.set_polymarket_proxy`,
:func:`~polyecho.shared.http.clear_polymarket_proxy`).

These guarantee the CLI and the web ``setPolymarketProxy`` server action
validate a proxy URL **identically** and write the **same** single
``settings`` row in the **same** bare-string JSONB shape that
:func:`~polyecho.shared.http._coerce_proxy` /
:func:`~polyecho.shared.http.get_effective_proxy_url` (the live trader-core
path) reads — i.e. one source of truth across CLI / web / runtime.

No Postgres, no network: an in-memory ``settings`` double drives the
optional-session path (mirrors ``test_danger_passphrase`` / the
``packages/shared/tests/conftest.py`` ``run`` bridge).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from polyecho.shared import http as shared_http
from polyecho.shared.config import get_settings
from polyecho.shared.http import (
    PROXY_SETTING_KEY,
    ProxyValidationError,
    clear_polymarket_proxy,
    set_polymarket_proxy,
    validate_proxy_url,
)

from .conftest import run


# ---------------------------------------------------------------------------
# In-memory ``settings`` store — supports select / add / flush / delete.
# ``set_polymarket_proxy`` builds a REAL ``Setting`` ORM object and uses the
# REAL ``select()``/``delete()`` constructs, so we must NOT patch ``Setting``.
# ---------------------------------------------------------------------------
class _SettingRow:
    def __init__(self, key: str, value: Any) -> None:
        self.key = key
        self.value = value


class _Sess:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def add(self, obj: Any) -> None:
        self.store[obj.key] = obj

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        # Extract the bound ``key`` parameter from either a select(...) or a
        # delete(...) statement; delete() means "remove that key".
        params = stmt.compile().params
        key = next(iter(params.values())) if params else None
        is_delete = stmt.__class__.__name__.lower().startswith("delete")
        if is_delete:
            if key in self.store:
                del self.store[key]

            class _Empty:
                def scalar_one_or_none(self) -> Any:
                    return None

            return _Empty()

        row = self.store.get(cast(str, key))

        class _R:
            def scalar_one_or_none(self) -> Any:
                return row

        return _R()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# validate_proxy_url — parity with the web ``setPolymarketProxy``/``ssrfReject``
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://203.0.113.10:8080",
        "https://proxy.example.com:3128",
        "http://user:pass@proxy.example.com:8080",
        "http://[2001:db8::1]:8080",  # a global IPv6 — allowed
        "  http://203.0.113.10:8080  ",  # trimmed
    ],
)
def test_validate_accepts_public_httpx(url: str) -> None:
    out = validate_proxy_url(url)
    assert out == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        # SOCKS explicitly rejected (httpx transport is http(s)-only)
        "socks5://1.2.3.4:1080",
        "socks5h://1.2.3.4:1080",
        "socks4://1.2.3.4:1080",
        # non-http(s) schemes
        "ftp://1.2.3.4",
        "file:///etc/passwd",
        "gopher://1.2.3.4",
        # unparseable / no host
        "http://",
        "https://",
        # loopback / RFC1918 / link-local / metadata / 0.0.0.0
        "http://127.0.0.1:8080",
        "http://10.0.0.5:3128",
        "http://172.16.5.4:3128",
        "http://172.31.255.255:3128",
        "http://192.168.1.1:3128",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://169.254.10.10:8080",  # link-local
        "http://0.0.0.0:8080",
        # localhost-ish / internal names
        "http://localhost:8080",
        "http://svc.local:8080",
        "http://svc.internal:8080",
        # local IPv6 literals
        "http://[::1]:8080",
        "http://[::]:8080",
        "http://[fe80::1]:8080",
        "http://[fc00::1]:8080",
        "http://[fd12::1]:8080",
    ],
)
def test_validate_rejects_ssrf_and_bad_scheme(url: str) -> None:
    with pytest.raises(ProxyValidationError):
        validate_proxy_url(url)


def test_validate_rejects_empty() -> None:
    with pytest.raises(ProxyValidationError):
        validate_proxy_url("   ")


def test_validate_socks_message_is_socks_specific() -> None:
    with pytest.raises(ProxyValidationError, match="SOCKS"):
        validate_proxy_url("socks5://1.2.3.4:1080")


def test_validate_172_12_boundary_allowed() -> None:
    # 172.15 / 172.32 are OUTSIDE RFC1918 172.16.0.0/12 → allowed
    assert validate_proxy_url("http://172.15.0.1:3128")
    assert validate_proxy_url("http://172.32.0.1:3128")


# ---------------------------------------------------------------------------
# set_polymarket_proxy / clear_polymarket_proxy (in-memory settings double)
# ---------------------------------------------------------------------------
def test_set_writes_bare_string_row() -> None:
    sess = _Sess()
    run(set_polymarket_proxy("http://203.0.113.10:8080", session=sess))
    assert list(sess.store) == [PROXY_SETTING_KEY]
    row = sess.store[PROXY_SETTING_KEY]
    # The bare trimmed string — IDENTICAL shape to the web action's write.
    assert row.value == "http://203.0.113.10:8080"
    assert isinstance(row.value, str)


def test_set_trims_before_persisting() -> None:
    sess = _Sess()
    run(set_polymarket_proxy("  https://p.example:3128  ", session=sess))
    assert sess.store[PROXY_SETTING_KEY].value == "https://p.example:3128"


def test_set_updates_existing_row_in_place() -> None:
    sess = _Sess()
    run(set_polymarket_proxy("http://a.example:1", session=sess))
    first = sess.store[PROXY_SETTING_KEY]
    run(set_polymarket_proxy("http://b.example:2", session=sess))
    # Single key — upsert overwrites, never accumulates.
    assert list(sess.store) == [PROXY_SETTING_KEY]
    assert sess.store[PROXY_SETTING_KEY] is first  # updated in place
    assert first.value == "http://b.example:2"


def test_clear_deletes_the_row() -> None:
    sess = _Sess()
    run(set_polymarket_proxy("http://203.0.113.10:8080", session=sess))
    assert PROXY_SETTING_KEY in sess.store
    run(clear_polymarket_proxy(session=sess))
    assert PROXY_SETTING_KEY not in sess.store


def test_clear_is_idempotent_on_missing_row() -> None:
    sess = _Sess()
    run(clear_polymarket_proxy(session=sess))  # no row → no-op, no raise
    assert sess.store == {}


@pytest.mark.parametrize("empty", [None, "", "   ", "\t\n"])
def test_set_empty_clears_like_the_web_action(empty: Any) -> None:
    sess = _Sess()
    run(set_polymarket_proxy("http://203.0.113.10:8080", session=sess))
    assert PROXY_SETTING_KEY in sess.store
    # Web: empty → DELETE the row (then env fallback / direct).
    run(set_polymarket_proxy(empty, session=sess))
    assert PROXY_SETTING_KEY not in sess.store


def test_set_rejected_url_writes_nothing() -> None:
    sess = _Sess()
    with pytest.raises(ProxyValidationError):
        run(set_polymarket_proxy("http://169.254.169.254", session=sess))
    assert sess.store == {}
    with pytest.raises(ProxyValidationError):
        run(set_polymarket_proxy("socks5://1.2.3.4:1080", session=sess))
    assert sess.store == {}
    with pytest.raises(ProxyValidationError):
        run(set_polymarket_proxy("http://127.0.0.1:9", session=sess))
    assert sess.store == {}


# ---------------------------------------------------------------------------
# Single-source-of-truth parity: what the CLI writes is EXACTLY what
# get_effective_proxy_url / _coerce_proxy (web + live trader-core) reads.
# ---------------------------------------------------------------------------
def test_written_value_is_what_effective_resolver_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No env proxy, so a resolved value can ONLY come from the DB row.
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()
    sess = _Sess()
    run(set_polymarket_proxy("http://203.0.113.10:8080", session=sess))

    stored = sess.store[PROXY_SETTING_KEY].value
    # The web's _coerce_proxy reads this exact stored shape …
    assert shared_http._coerce_proxy(stored) == "http://203.0.113.10:8080"
    # … and the live resolver, given the same row, returns the same value
    # (DB row wins over the absent env) — so the web /settings page and
    # trader-core would show/dial precisely what the CLI just wrote.
    effective = run(
        shared_http.get_effective_proxy_url(session=_RowSession(stored))
    )
    assert effective == "http://203.0.113.10:8080"


def test_cleared_row_falls_back_to_env_then_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After clear, the resolver must fall back to env, then None — matching
    # the web action's "empty → delete" + env-fallback semantics.
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://env:5")
    get_settings.cache_clear()
    # No DB row → env value.
    assert (
        run(shared_http.get_effective_proxy_url(session=_RowSession(None)))
        == "http://env:5"
    )
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()
    # No DB row, no env → direct (None).
    assert (
        run(shared_http.get_effective_proxy_url(session=_RowSession(None)))
        is None
    )


class _RowSession:
    """Minimal read-only session for ``get_effective_proxy_url`` (returns a
    row whose ``.value`` is *value*, or no row when *value* is ``None``)."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def execute(self, _stmt: Any) -> Any:
        row = _SettingRow(PROXY_SETTING_KEY, self._value) if (
            self._value is not None
        ) else None

        class _R:
            def scalar_one_or_none(self) -> Any:
                return row

        return _R()

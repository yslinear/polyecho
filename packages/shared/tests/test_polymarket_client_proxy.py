"""
tests/test_polymarket_client_proxy.py
=====================================
P0-4 — :class:`polyecho.shared.polymarket.client.PolymarketClient` must
NOT mutate the process-global proxy environment.

The previous code used ``os.environ.setdefault(...)`` which (a) survived
construction, (b) leaked proxy creds into every later HTTP call /
subprocess, and (c) let one client contaminate another (first-write-wins).
These tests pin the fixed behaviour: the proxy is applied ONLY during the
single ``ClobClient(...)`` construction and the prior environment is
restored byte-for-byte; two clients with different proxies never
contaminate each other.

``ClobClient`` is patched (a recording stub) so no SDK / network runs; we
capture the proxy env vars *as seen inside* its constructor.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from polyecho.shared.config import get_settings

_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")


class _RecordingClob:
    """Stub ClobClient: records the proxy env visible during __init__."""

    seen: list[dict[str, str | None]] = []

    def __init__(self, **_kw: Any) -> None:
        type(self).seen.append(
            {v: os.environ.get(v) for v in _PROXY_VARS}
        )


@pytest.fixture
def patched_clob(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClob]:
    import polyecho.shared.polymarket.client as mod

    _RecordingClob.seen = []
    monkeypatch.setattr(mod, "ClobClient", _RecordingClob)
    return _RecordingClob


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in _PROXY_VARS:
        monkeypatch.delenv(v, raising=False)


def _make(**kw: Any) -> Any:
    from polyecho.shared.polymarket.client import PolymarketClient

    return PolymarketClient(
        signature_type=kw.get("signature_type", 0),
        funder=kw.get("funder"),
        creds=None,
        private_key=None,
    )


def test_no_proxy_env_left_untouched(
    monkeypatch: pytest.MonkeyPatch, patched_clob: type[_RecordingClob]
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.delenv("POLYECHO_POLYMARKET_PROXY_URL", raising=False)
    get_settings.cache_clear()

    _make()

    # SDK saw no proxy; process env still has none afterwards.
    assert patched_clob.seen == [{v: None for v in _PROXY_VARS}]
    for v in _PROXY_VARS:
        assert v not in os.environ


def test_proxy_applied_only_during_construction_then_restored(
    monkeypatch: pytest.MonkeyPatch, patched_clob: type[_RecordingClob]
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv(
        "POLYECHO_POLYMARKET_PROXY_URL", "http://user:pass@px:3128"
    )
    get_settings.cache_clear()

    _make()

    # Visible to the SDK during __init__ …
    assert patched_clob.seen[0] == {
        v: "http://user:pass@px:3128" for v in _PROXY_VARS
    }
    # … but NOT leaked into the process env afterwards (no creds leak).
    for v in _PROXY_VARS:
        assert v not in os.environ


def test_preexisting_proxy_env_is_restored_exactly(
    monkeypatch: pytest.MonkeyPatch, patched_clob: type[_RecordingClob]
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://preexisting:1")
    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://cfg:2")
    get_settings.cache_clear()

    _make()

    # The configured proxy won inside construction …
    assert patched_clob.seen[0]["HTTPS_PROXY"] == "http://cfg:2"
    # … and the operator's original value is restored verbatim afterwards
    # (not clobbered, not deleted).
    assert os.environ["HTTPS_PROXY"] == "http://preexisting:1"


def test_two_clients_different_proxies_do_not_contaminate(
    monkeypatch: pytest.MonkeyPatch, patched_clob: type[_RecordingClob]
) -> None:
    _clear_proxy_env(monkeypatch)

    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://a:1")
    get_settings.cache_clear()
    _make()

    monkeypatch.setenv("POLYECHO_POLYMARKET_PROXY_URL", "http://b:2")
    get_settings.cache_clear()
    _make()

    # Each client saw ONLY its own proxy (old setdefault would have made
    # the 2nd inherit the 1st's "http://a:1").
    assert patched_clob.seen[0]["HTTPS_PROXY"] == "http://a:1"
    assert patched_clob.seen[1]["HTTPS_PROXY"] == "http://b:2"
    for v in _PROXY_VARS:
        assert v not in os.environ

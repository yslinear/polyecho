"""
tests/test_polygon_rpc_fallback.py
==================================
Coverage for the Polygon RPC fallback behaviour.

``POLYECHO_POLYGON_RPC_URL`` may be a single URL (the long-standing
configuration) **or** a comma-separated list of URLs.  In the list form
the integration probe and the SAFE-proxy resolver iterate through the
endpoints in order, advancing on each transport / HTTP / JSON-RPC
failure and reporting success on the first endpoint that works.

The tests assert:

1.  ``Settings.polygon_rpc_url`` keeps its plain-string shape for any
    legacy caller (``core.wiring`` / ``core.onchain_reconcile``).
2.  ``Settings.polygon_rpc_urls`` parses the CSV into an ordered list,
    trims whitespace, and drops empty entries.
3.  ``probe_polygon_rpc`` succeeds on the first reachable endpoint,
    even when earlier endpoints fail with a transport error / HTTP
    error / JSON-RPC error body.
4.  ``probe_polygon_rpc`` reports ``ok=false`` only when EVERY endpoint
    fails (last failure detail surfaced).
5.  The single-URL configuration continues to work unchanged
    (back-compat).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from polyecho.shared import health

from .conftest import run


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Route every health HTTP probe through a MockTransport (no network)."""

    async def _factory(*, session: Any = None, **overrides: Any) -> httpx.AsyncClient:
        overrides.pop("proxy", None)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=overrides.get("timeout", 5.0),
        )

    monkeypatch.setattr("polyecho.shared.http.make_async_client_resolved", _factory)


# ---------------------------------------------------------------------------
# Settings shape
# ---------------------------------------------------------------------------
class TestSettingsField:
    def test_single_url_back_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A single URL string still parses (no CSV detected)."""
        monkeypatch.setenv("POLYECHO_POLYGON_RPC_URL", "https://polygon.drpc.org")
        from polyecho.shared.config import get_settings

        s = get_settings()
        assert s.polygon_rpc_url == "https://polygon.drpc.org"
        assert s.polygon_rpc_urls == ["https://polygon.drpc.org"]

    def test_csv_parses_into_ordered_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CSV expands to a list in order; first URL is exposed as the str."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://polygon.drpc.org,https://polygon-bor-rpc.publicnode.com,https://1rpc.io/matic",
        )
        from polyecho.shared.config import get_settings

        s = get_settings()
        # Back-compat: the plain-string attr still has a single URL
        # (the FIRST one), so legacy callers continue to work.
        assert s.polygon_rpc_url == "https://polygon.drpc.org"
        # Full list available for iteration-aware consumers.
        assert s.polygon_rpc_urls == [
            "https://polygon.drpc.org",
            "https://polygon-bor-rpc.publicnode.com",
            "https://1rpc.io/matic",
        ]

    def test_csv_trims_whitespace_and_drops_blanks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "  https://a.example , ,https://b.example ,",
        )
        from polyecho.shared.config import get_settings

        s = get_settings()
        assert s.polygon_rpc_url == "https://a.example"
        assert s.polygon_rpc_urls == [
            "https://a.example",
            "https://b.example",
        ]

    def test_default_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLYECHO_POLYGON_RPC_URL", raising=False)
        from polyecho.shared.config import get_settings

        s = get_settings()
        # Whatever default ships in the field — list shape is consistent.
        assert isinstance(s.polygon_rpc_url, str) and s.polygon_rpc_url
        assert s.polygon_rpc_urls == [s.polygon_rpc_url]


# ---------------------------------------------------------------------------
# probe_polygon_rpc iteration
# ---------------------------------------------------------------------------
class TestProbePolygonRpcFallback:
    def test_single_url_success_back_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single-URL config: detail is the legacy ``block <hex>`` shape."""
        monkeypatch.setenv("POLYECHO_POLYGON_RPC_URL", "https://polygon.drpc.org")
        _mock_http(
            monkeypatch,
            lambda r: httpx.Response(200, json={"result": "0x1a2b"}),
        )
        res = run(health.probe_polygon_rpc())
        assert res.ok is True
        assert "0x1a2b" in res.detail
        # No "via …" suffix for the single-URL path (BC).
        assert " via " not in res.detail

    def test_single_url_failure_back_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single URL that fails returns the legacy ok=false detail."""
        monkeypatch.setenv("POLYECHO_POLYGON_RPC_URL", "https://polygon.drpc.org")
        _mock_http(monkeypatch, lambda r: httpx.Response(403))
        res = run(health.probe_polygon_rpc())
        assert res.ok is False
        assert "HTTP 403" in res.detail

    def test_first_fails_second_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """403 from URL #1, 200 from URL #2 → ok=True naming URL #2."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://first.example,https://second.example",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if "first.example" in str(req.url):
                return httpx.Response(403)
            return httpx.Response(200, json={"result": "0x42"})

        _mock_http(monkeypatch, handler)
        res = run(health.probe_polygon_rpc())
        assert res.ok is True
        assert "0x42" in res.detail
        assert "second.example" in res.detail  # detail names the winner

    def test_first_connect_error_second_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Transport-level failure on #1 still advances to #2."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://broken.example,https://good.example",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if "broken.example" in str(req.url):
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"result": "0x99"})

        _mock_http(monkeypatch, handler)
        res = run(health.probe_polygon_rpc())
        assert res.ok is True
        assert "0x99" in res.detail
        assert "good.example" in res.detail

    def test_first_json_rpc_error_second_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2xx + JSON-RPC ``error`` body on #1 also advances to #2."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://bad-rpc.example,https://ok-rpc.example",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if "bad-rpc.example" in str(req.url):
                return httpx.Response(200, json={"error": {"code": -32000}})
            return httpx.Response(200, json={"result": "0x7"})

        _mock_http(monkeypatch, handler)
        res = run(health.probe_polygon_rpc())
        assert res.ok is True
        assert "0x7" in res.detail
        assert "ok-rpc.example" in res.detail

    def test_all_endpoints_fail_returns_last_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every endpoint fails → ok=False, last failure detail surfaced."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://one.example,https://two.example,https://three.example",
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if "three.example" in str(req.url):
                return httpx.Response(503)
            return httpx.Response(403)

        _mock_http(monkeypatch, handler)
        res = run(health.probe_polygon_rpc())
        assert res.ok is False
        # Last failure detail wins (the 503 from the third endpoint).
        assert "503" in res.detail

    def test_first_url_succeeds_does_not_try_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short-circuit: a working first URL means later URLs are never hit."""
        monkeypatch.setenv(
            "POLYECHO_POLYGON_RPC_URL",
            "https://winner.example,https://never-called.example",
        )
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json={"result": "0x1"})

        _mock_http(monkeypatch, handler)
        res = run(health.probe_polygon_rpc())
        assert res.ok is True
        assert all("never-called" not in u for u in seen)
        assert any("winner.example" in u for u in seen)

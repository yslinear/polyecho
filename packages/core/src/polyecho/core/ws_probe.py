"""Bounded, fail-soft WebSocket reachability probes (NON-frozen).

The FROZEN ``polyecho.shared.health`` status contract probes the REST
endpoints (CLOB / Data API / Polygon RPC), DB, config and VPN egress, but
has **no WebSocket probe**.  trader-core's live fill-detection path is the
authenticated Polymarket *User-Channel* WebSocket (``CLOB_WSS_USER_URL``;
the market channel is ``CLOB_WSS_MARKET_URL``), so the ``/status`` page
must also surface whether those WS endpoints are reachable.

This module adds that WITHOUT touching the frozen health contract: it
returns the SAME frozen :class:`~polyecho.shared.health.ProbeResult`
model and is merged into the snapshot by the trader-core caller
(:meth:`TraderCore._run_integration_probe_once`).  The dashboard renders
``integration.probes`` generically, so a new key appears automatically.

Contract — IDENTICAL to the frozen ``run_all_probes`` probes:

* **Bounded** — each probe is wrapped in its own :func:`asyncio.timeout`
  *and* passes ``open_timeout`` to ``websockets.connect`` (belt and
  suspenders) so it can never hang the integration sweep.
* **Fail-soft** — ``ok=False`` + ``detail`` on any failure; it NEVER
  raises (cooperative :class:`asyncio.CancelledError` still propagates,
  exactly like the frozen probes' ``except Exception``).
* A successful WS *opening handshake* ⇒ ``ok=True``.  The live WS client
  itself is not yet implemented (fixture replay is the tested detection
  path), so this is a pure endpoint-reachability check, phrased as such
  in ``detail``.  It routes through the operator proxy
  (:func:`polyecho.shared.http.get_proxy_url` — the SAME seam the frozen
  REST probes use) when one is configured: ``*.polymarket.com``'s direct
  path is TLS-intercepted (self-signed cert) and only the proxy/VPN
  egress is clean, so a *direct* WS probe falsely reports the endpoint
  unreachable.  ``websockets`` (resolved 15.x) accepts ``proxy=``;
  ``None`` (no proxy configured) ⇒ direct.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from polyecho.shared.constants import CLOB_WSS_MARKET_URL, CLOB_WSS_USER_URL
from polyecho.shared.health import ProbeResult
from polyecho.shared.http import get_proxy_url

#: Per-probe hard ceiling.  Strictly < the caller's
#: ``INTEGRATION_PROBE_TIMEOUT_SECONDS`` (20s) which bounds the whole
#: sweep, so even both WS probes failing slow cannot starve it.
WS_PROBE_TIMEOUT_SECONDS: float = 6.0


def _now_iso() -> str:
    """ISO-8601 UTC, matching the frozen health module's ``_now_iso``."""
    return datetime.now(UTC).isoformat()


async def _probe_ws(url: str) -> ProbeResult:
    """Attempt a bounded WS opening handshake to *url*; never raise."""
    t0 = time.perf_counter()
    try:
        # Imported lazily so a missing/!=expected websockets build degrades
        # to ok=False (fail-soft) instead of an import-time crash.
        import websockets

        # Route through the operator proxy — the SAME seam the REST
        # probes use.  *.polymarket.com's direct path is TLS-intercepted
        # (self-signed cert); only the proxy/VPN egress is clean.  None
        # ⇒ no proxy configured ⇒ direct (websockets 15.x: proxy=None
        # explicitly disables, never auto-reads env).
        proxy = get_proxy_url()
        async with asyncio.timeout(WS_PROBE_TIMEOUT_SECONDS):
            async with websockets.connect(
                url,
                open_timeout=WS_PROBE_TIMEOUT_SECONDS,
                close_timeout=WS_PROBE_TIMEOUT_SECONDS,
                proxy=proxy,
            ):
                # Opening handshake completed → endpoint reachable.
                pass
        latency_ms = (time.perf_counter() - t0) * 1000
        return ProbeResult(
            ok=True,
            detail=f"WS opening handshake OK ({url})",
            latency_ms=latency_ms,
            checked_at=_now_iso(),
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: report, never raise
        return ProbeResult(
            ok=False,
            detail=f"WS unreachable ({url}): {exc!r}"[:240],
            latency_ms=(time.perf_counter() - t0) * 1000,
            checked_at=_now_iso(),
        )


async def probe_polymarket_ws_user() -> ProbeResult:
    """Reachability of the authenticated User-Channel WS (live detection)."""
    return await _probe_ws(CLOB_WSS_USER_URL)


async def probe_polymarket_ws_market() -> ProbeResult:
    """Reachability of the Market WS channel (orderbook / price changes)."""
    return await _probe_ws(CLOB_WSS_MARKET_URL)

"""PolyEcho detect — aiohttp service that derives the 4 Polymarket
signature-type funder candidates for an EOA.

Single endpoint. No DB. No keys. No public port.

* **GET /detect?eoa=0x<40-hex>** → 200 JSON object with the four funder
  candidates (sig0..sig3) per the Polymarket signature-type table.
  Bad input → 400 ``{"error": "invalid eoa"}`` — **never echo the input**.
* **GET /healthz** → 200 ``{"ok": true}`` for the compose healthcheck.

This module **MUST NOT** import :mod:`polyecho.shared.config`.  That module
boots ``Settings`` and transitively validates the DSN (see lesson #17 in
``polyecho-known-gaps``).  detect has no DB connection and must boot in a
container that doesn't have ``POLYECHO_DB_URL`` set.  Only the pure-data
modules :mod:`polyecho.shared.polymarket.wallet` and
:mod:`polyecho.shared.constants` are pulled in.  A guard test
(``tests/test_no_settings_import.py``) enforces this at CI time.

Security model:

* **NO public port.**  The compose service intentionally omits
  ``ports:``.  The container is reachable only from the ``web`` service
  over the polyecho docker network (``http://detect:8001``).  Trust here
  is provided by docker network isolation, not authentication —
  consistent with PRD §4.2 (DB-only intra-service comms — detect is the
  one exception, sanctioned because it touches NO keys, NO DB, NO state).
* **Stateless.**  No cache, no DB, no on-disk state.  Every call is a
  pure compute (sig0/1/3) plus a single Polygon view-call (sig2).
* **Never echo input on error.**  Malformed EOAs / RPC failures emit
  generic strings; the offending value never enters the response body.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from aiohttp import web

from polyecho.shared.polymarket.wallet import (
    WalletMode,
    deposit_wallet_address,
    magic_proxy_address,
    make_safe_proxy_resolver,
)

__all__ = ["W3_KEY", "make_app", "run"]

# Typed app-state key for the (Async)Web3 client. aiohttp 3.10+ recommends
# AppKey over bare-string keys (NotAppKeyWarning otherwise). Typed as
# ``object`` because the real value is ``AsyncWeb3`` in production but
# tests inject a structural fake — we don't want a hard web3 type pin in
# the key.
W3_KEY: web.AppKey[object] = web.AppKey("w3", object)

# ── Constants ────────────────────────────────────────────────────────────────
# Strict EVM address regex.  The shape `^0x[0-9a-fA-F]{40}$` is intentional
# (lower OR upper hex accepted; we lowercase before deriving).  Anything else
# — checksum-corrupt, length-off, non-hex — is rejected at the door.
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Polygon RPC default. Per polyecho-ops-runbook: polygon-rpc.com is dead
# (tenant-disabled, proxy-independent). drpc.org is the operator-blessed
# fallback. The container takes POLYECHO_POLYGON_RPC_URL from env when set.
_DEFAULT_POLYGON_RPC = "https://polygon.drpc.org"

# SAFE (type-2) view-call budget. The whole point of fail-soft is that a
# flaky/down Polygon RPC must NEVER block sig0/1/3 from rendering — so we
# wall-clock cap the eth_call. 5s is generous for a single eth_call.
_SAFE_RPC_TIMEOUT_S = 5.0

# Service listen port. The compose healthcheck and the web→detect URL both
# hard-code 8001 — keep them in sync if you ever change this.
_PORT = 8001

_log = logging.getLogger("polyecho.detect")


# ── Handlers ─────────────────────────────────────────────────────────────────
async def _healthz(request: web.Request) -> web.Response:
    """Compose healthcheck endpoint — always 200 once the app is up.

    Returning a tiny JSON body (not just 200 No-Content) so the compose
    healthcheck's ``urllib.request.urlopen().read()`` has something to read
    and fails loud if the route ever gets accidentally renamed.
    """
    return web.json_response({"ok": True})


async def _detect(request: web.Request) -> web.Response:
    """Derive the 4 sig-type funder candidates for an EOA.

    Validates input, computes sig0/1/3 (pure functions, never fail), then
    fail-softs sig2 (needs Polygon RPC). Bad input returns 400 with a
    generic error — never echoes the offending value back.
    """
    eoa = (request.query.get("eoa") or "").strip()
    if not _EVM_ADDRESS_RE.match(eoa):
        # Generic error string — DO NOT echo `eoa` (could be log-poison /
        # XSS reflector / PII leak into web's UI).
        return web.json_response({"error": "invalid eoa"}, status=400)

    eoa_l = eoa.lower()

    # sig0/1/3 are pure compute (keccak/CREATE2 derivations from the EOA).
    # They cannot fail short of an OOM. Compute them up front so an RPC
    # hiccup on sig2 never starves the other three.
    sig0: dict[str, Any] = {
        "addr": eoa_l,
        "mode": WalletMode.EOA,
        "derived": False,
    }
    sig1: dict[str, Any] = {
        "addr": magic_proxy_address(eoa_l),
        "mode": WalletMode.MAGIC,
        "derived": True,
    }
    sig3: dict[str, Any] = {
        "addr": deposit_wallet_address(eoa_l),
        "mode": WalletMode.POLY_1271,
        "derived": True,
    }

    # sig2 (SAFE proxy) requires a Polygon eth_call on the Safe Proxy
    # Factory's `computeProxyAddress(eoa)`. RPC may be down / slow / rate-
    # limited; the response shape's `error` field carries that signal.
    sig2 = await _resolve_safe(eoa_l, request.app[W3_KEY])

    return web.json_response({"sig0": sig0, "sig1": sig1, "sig2": sig2, "sig3": sig3})


async def _resolve_safe(eoa: str, w3: object) -> dict[str, Any]:
    """SAFE (sig-type 2) funder via the Polygon view-call. Fail-soft.

    On timeout / RPC error returns ``{"addr": null, "error": <generic>}``
    so the caller still gets sig0/1/3. The error string is GENERIC — we
    never leak ``exc.args`` (which may contain RPC response bodies / URLs
    / proxy creds / chain context).
    """
    try:
        resolver = make_safe_proxy_resolver(w3)
        addr = await asyncio.wait_for(resolver(eoa), timeout=_SAFE_RPC_TIMEOUT_S)
        return {"addr": addr, "mode": WalletMode.SAFE, "derived": True}
    except TimeoutError:
        # asyncio.TimeoutError == TimeoutError in 3.11+
        _log.warning("detect: SAFE proxy RPC timeout")
        return {
            "addr": None,
            "mode": WalletMode.SAFE,
            "derived": True,
            "error": "RPC timeout",
        }
    except Exception:
        # NEVER include `exc.args` — could leak RPC URL / response body /
        # account hints into the operator UI.
        _log.warning("detect: SAFE proxy RPC error", exc_info=True)
        return {
            "addr": None,
            "mode": WalletMode.SAFE,
            "derived": True,
            "error": "RPC error",
        }


# ── App factory + entrypoint ─────────────────────────────────────────────────
def make_app(rpc_url: str | None = None) -> web.Application:
    """Construct the aiohttp Application.

    Args:
        rpc_url: Optional override for the Polygon RPC URL.  Tests pass a
                 sentinel here so they never touch a real node; production
                 leaves it ``None`` and the URL is read from
                 ``POLYECHO_POLYGON_RPC_URL`` (falling back to drpc.org).

    Returns:
        A configured :class:`aiohttp.web.Application` with the
        :class:`~web3.AsyncWeb3` client stashed at ``app["w3"]``.
    """
    app = web.Application()

    if rpc_url is None:
        rpc_url = os.environ.get("POLYECHO_POLYGON_RPC_URL", _DEFAULT_POLYGON_RPC)

    # Late web3 imports: this keeps importing `polyecho.detect.app` cheap
    # for tests that stub `app["w3"]` directly. (web3.py drags in eth-abi /
    # eth-account / hexbytes / parsimonious — measurable cold-start cost.)
    # NOTE: web3 v7 re-exports AsyncHTTPProvider at the top-level package
    # (canonical home is web3.providers.rpc.async_rpc — import via the
    # top-level alias to stay version-tolerant).
    from web3 import AsyncHTTPProvider, AsyncWeb3

    app[W3_KEY] = AsyncWeb3(AsyncHTTPProvider(rpc_url))

    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/detect", _detect)
    return app


def run() -> None:
    """Run the service on 0.0.0.0:8001 (entrypoint for ``python -m``)."""
    logging.basicConfig(
        level=os.environ.get("POLYECHO_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    web.run_app(make_app(), host="0.0.0.0", port=_PORT)

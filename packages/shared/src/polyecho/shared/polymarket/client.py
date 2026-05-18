"""
polyecho.shared.polymarket.client
=================================
Thin async wrapper around ``py_clob_client_v2.ClobClient``.

Verified SDK shape (context7, 2026-05-17)
------------------------------------------
* Package: ``py-clob-client-v2`` (pip), import: ``py_clob_client_v2``
* ``ClobClient(host, chain_id, key=None, creds=None, signature_type=None, funder=None, ...)``
* ``ApiCreds(api_key, api_secret, api_passphrase)``
* Order placement: ``client.create_and_post_order(order_args, options, order_type)``
* Cancellation: ``client.cancel_order(order_id)``
* The underlying client is **synchronous**; we wrap each call with
  ``asyncio.to_thread`` so callers can ``await`` them without blocking the
  event loop.

Method stubs
------------
All methods raise ``NotImplementedError("Phase N")`` with a docstring
describing the expected behaviour and signature.  Phase 1 = Discovery,
Phase 4 = Live trading.

Usage (Phase 1+)::

    from polyecho.shared.polymarket.client import PolymarketClient
    from polyecho.shared.config import get_settings

    settings = get_settings()
    client = PolymarketClient(
        signature_type=2,          # POLY_GNOSIS_SAFE
        funder="0xFunderAddress",
        creds={
            "api_key": "...",
            "api_secret": "...",
            "api_passphrase": "...",
        },
        private_key="0xPrivKey",   # never logged
    )
    leaderboard = await client.get_leaderboard()
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Iterator
from typing import Any

from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore[import-untyped]

from polyecho.shared.config import get_settings
from polyecho.shared.copytrading import assert_live_allowed

#: The proxy env vars py-clob-client-v2's underlying HTTP layer honours.
#: http(s) only — see polyecho.shared.http for the SOCKS limitation note.
_PROXY_ENV_VARS: tuple[str, ...] = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")

#: R4: ``_scoped_proxy_env`` save/set/restore mutates the *process-global*
#: ``os.environ``.  Two concurrent ``PolymarketClient(...)`` constructions
#: (each ``ClobClient(...)`` is offloaded via ``asyncio.to_thread`` and can
#: run on different threads) would interleave the set/restore and corrupt
#: each other's view — one thread could restore while the other expects the
#: proxy set, or leak/lose a var.  This module-level lock serialises the
#: whole set→yield→restore window so the env mutation is atomic per client
#: construction (constructions are brief; serialising them is acceptable).
_PROXY_ENV_LOCK = threading.Lock()


@contextlib.contextmanager
def _scoped_proxy_env(proxy: str | None) -> Iterator[None]:
    """Apply *proxy* to the proxy env vars **only for the wrapped block**.

    P0-4: ``py_clob_client_v2.ClobClient`` exposes NO proxy / session /
    transport parameter (verified via context7 against py-clob-client-v2
    v1.0.1 — ``ClobClient(host, chain_id, key, creds, signature_type,
    funder, builder_config, use_server_time, retry_on_error)``; no network
    hook).  Its sync HTTP layer only honours the standard proxy env vars, so
    a proxy *must* travel through ``os.environ`` — but the previous
    ``os.environ.setdefault(...)`` mutated the **process global** and
    leaked: it (a) survived past client construction, (b) leaked the proxy
    (with any ``user:pass@`` creds) into every later HTTP call and every
    subprocess, and (c) let one ``PolymarketClient`` contaminate another
    with a different proxy (``setdefault`` is first-write-wins, so instance
    B silently inherited instance A's proxy).

    This context manager instead sets the vars *just* around the single
    ``ClobClient(...)`` construction (the only point where the SDK builds
    its session) and **restores the exact prior environment** on exit —
    including deleting vars that were unset before.  Net effect: no
    process-global mutation survives, two clients with different proxies
    never contaminate each other, and proxy credentials never leak into the
    global env or inherited subprocess environments.  When *proxy* is
    ``None`` the environment is left completely untouched (byte-for-byte
    identical to no proxy configured).
    """
    if not proxy:
        yield
        return

    # R4: serialise the entire save→set→yield→restore window.  Without this
    # lock two concurrent constructions (each ClobClient build runs in its
    # own ``asyncio.to_thread`` thread) interleave their os.environ
    # mutations and corrupt each other's save/restore.  The lock makes the
    # global-env mutation atomic per construction.
    with _PROXY_ENV_LOCK:
        _sentinel = object()
        saved: dict[str, str | object] = {
            var: os.environ.get(var, _sentinel) for var in _PROXY_ENV_VARS
        }
        try:
            for var in _PROXY_ENV_VARS:
                os.environ[var] = proxy
            yield
        finally:
            for var, prev in saved.items():
                if prev is _sentinel:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prev  # type: ignore[assignment]


class PolymarketClient:
    """Async wrapper around ``py_clob_client_v2.ClobClient``.

    All public methods are coroutines. The synchronous SDK calls are
    offloaded to a thread pool via ``asyncio.to_thread``.

    Args:
        signature_type: Wallet signature type (0=EOA, 1=POLY_PROXY,
                        2=POLY_GNOSIS_SAFE, 3=POLY_1271). See PRD §5.1.
        funder:         Address that holds the USDC funds (for proxy wallets).
                        Pass ``None`` for plain EOA (Type 0).
        creds:          Dict with ``api_key``, ``api_secret``, ``api_passphrase``
                        — obtained via ``ClobClient.derive_api_key()``.
        private_key:    Hex-encoded private key string. **Never log or print
                        this value.**  The caller is responsible for clearing
                        it from memory after constructing this object.
    """

    def __init__(
        self,
        *,
        signature_type: int,
        funder: str | None,
        creds: dict[str, str] | None,
        private_key: str | None = None,
    ) -> None:
        settings = get_settings()

        api_creds: ApiCreds | None = None
        if creds is not None:
            api_creds = ApiCreds(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                api_passphrase=creds["api_passphrase"],
            )

        # P0-4: py-clob-client-v2 v1.0.1's ClobClient exposes NO proxy /
        # session / transport parameter (verified via context7). Its sync
        # HTTP layer only honours the standard proxy env vars, so the proxy
        # is applied via _scoped_proxy_env() — set ONLY around this single
        # ClobClient(...) construction (where the SDK builds its session)
        # and the prior environment is restored on exit. No process-global
        # mutation survives: two PolymarketClient instances with different
        # proxies never contaminate each other and proxy creds never leak
        # into the global env / subprocesses. http(s) only — see
        # polyecho.shared.http SOCKS note. PRD §8.1 geo-block.
        from polyecho.shared.http import get_proxy_url

        with _scoped_proxy_env(get_proxy_url()):
            self._client = ClobClient(
                host=settings.polymarket_clob_url,
                chain_id=settings.polymarket_chain_id,
                key=private_key,
                creds=api_creds,
                signature_type=signature_type,
                funder=funder,
            )

    # ------------------------------------------------------------------
    # Public — Discovery / read-only (Phase 1)
    # ------------------------------------------------------------------

    async def get_leaderboard(
        self,
        *,
        window: str = "1m",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch the Polymarket trader leaderboard.

        Args:
            window: Time window for ranking — ``"1d"``, ``"1w"``, ``"1m"``,
                    ``"all"``.
            limit:  Maximum number of entries to return (up to 200).

        Returns:
            List of dicts with trader ranking data.  Shape mirrors the
            ``GET /leaderboard`` response from the Polymarket Data API
            (see PRD §11.1 → Leaderboard API).

        Raises:
            NotImplementedError: Phase 1 deliverable.
        """
        # TODO(verify): confirm endpoint path and query param names against
        #               Polymarket Data API docs (distinct from CLOB API).
        raise NotImplementedError("Phase 1: implement get_leaderboard via Data API")

    async def get_user_activity(
        self,
        wallet_address: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch on-chain trade activity for *wallet_address*.

        Args:
            wallet_address: The trader's wallet (EOA or proxy) address.
            limit:          Page size.
            offset:         Pagination offset.

        Returns:
            List of trade activity dicts (market, side, size, price, timestamp).

        Raises:
            NotImplementedError: Phase 1 deliverable.
        """
        raise NotImplementedError("Phase 1: implement get_user_activity via Data API")

    async def get_positions(
        self,
        wallet_address: str,
    ) -> list[dict[str, Any]]:
        """Fetch current open positions for *wallet_address*.

        Args:
            wallet_address: The trader's wallet address.

        Returns:
            List of open position dicts (market, outcome, size, avg_price).

        Raises:
            NotImplementedError: Phase 1 deliverable.
        """
        raise NotImplementedError("Phase 1: implement get_positions via Data API")

    async def get_orderbook(
        self,
        token_id: str,
    ) -> dict[str, Any]:
        """Fetch the current order book for a given outcome token.

        Args:
            token_id: The Polymarket outcome token ID (``YES`` or ``NO``
                      side of a market).

        Returns:
            Dict with ``bids`` and ``asks`` lists, each entry having
            ``price`` and ``size``.

        Raises:
            NotImplementedError: Phase 1 deliverable.
        """
        raise NotImplementedError("Phase 1: implement get_orderbook via CLOB API")

    # ------------------------------------------------------------------
    # Public — Trading (Phase 4)
    # ------------------------------------------------------------------

    async def place_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "FOK",
        tick_size: str = "0.01",
        neg_risk: bool = False,
    ) -> dict[str, Any]:
        """Place an order on the Polymarket CLOB (Phase-4 W2 live call).

        Signing is mode-agnostic and owned entirely by
        ``py_clob_client_v2`` — this wrapper does NOT hand-roll
        EIP-712/HMAC/rounding (brief §2/§8/§9).  The live SDK call is
        ``ClobClient.create_and_post_order(order_args, options,
        order_type)`` with per-market ``neg_risk`` + ``tick_size`` passed
        through ``options`` (fixes PolyHermes' static-tick gap, brief §2).

        Args:
            token_id:   Outcome token ID (e.g. the YES token for a market).
            side:       ``"BUY"`` or ``"SELL"``.
            price:      Limit price per share (0.01 – 0.99).
            size:       Number of shares.
            order_type: ``"FOK"`` / ``"FAK"`` (taker, brief §2 default) or
                        ``"GTC"`` (maker).  PRD §5.5 copy-trades use FAK/FOK.
            tick_size:  Per-market min price increment — fetched from Gamma
                        per market (brief §2), NOT static.
            neg_risk:   Per-market NegRisk flag — selects the NegRisk
                        Exchange contract (brief §2/§9; orthogonal to wallet
                        mode, passed straight to the SDK).

        Returns:
            Response dict from CLOB including ``orderID`` and ``status``.

        Raises:
            LiveTradingLockedError: If ``POLYECHO_LIVE_TRADING`` is off
                                    (the HARD lock — checked FIRST).
            NotImplementedError:    Phase-4 W2 deliverable (the live SDK
                                    call site).
        """
        # The single live-money chokepoint — MUST be first, before any
        # irreversible work (brief: default-OFF hard lock).
        assert_live_allowed("place_order")
        raise NotImplementedError(
            "Phase-4 W2: ClobClient.create_and_post_order(order_args, "
            "options={neg_risk, tick_size}, order_type) — SDK owns "
            "EIP-712/HMAC/rounding (brief §2/§9)"
        )

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open order by its CLOB order ID (Phase-4 W2).

        Args:
            order_id: The CLOB order ID (hex string returned by
                      :meth:`place_order`).

        Returns:
            Response dict confirming cancellation.

        Raises:
            LiveTradingLockedError: If ``POLYECHO_LIVE_TRADING`` is off.
            NotImplementedError:    Phase-4 W2 deliverable.
        """
        assert_live_allowed("cancel_order")
        raise NotImplementedError(
            "Phase-4 W2: ClobClient.cancel_order(order_id)"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_sync(self, fn: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Run a synchronous SDK call in a thread pool.

        This prevents blocking the asyncio event loop when the underlying
        ``py_clob_client_v2`` makes HTTP requests.
        """
        return await asyncio.to_thread(fn, *args, **kwargs)

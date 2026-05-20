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

Surface area
------------
This wrapper is the **trading** chokepoint only:

* ``place_order`` / ``cancel_order`` — Phase-4 W2 deliverables (still
  ``NotImplementedError``; gated by ``assert_live_allowed`` so they fail
  closed when the HARD live lock is off).

The four read-only methods (``get_leaderboard``, ``get_user_activity``,
``get_positions``, ``get_orderbook``) are **retired stubs**: every
production read path uses :class:`polyecho.shared.datasource.live.LiveDataSource`
(retry + circuit breaker + DTOs) for the Data-API endpoints, and
``orderbook_for`` callable injection (see ``polyecho.core.trader``) for
the CLOB orderbook.  The stubs are kept here only because the public
class is widely imported (proxy-env test, Phase-4 contract test, README,
core/execution.py).  Calling any of the four raises ``RuntimeError``
with a pointer to the canonical alternative.

Usage::

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
    # Trading (Phase 4):
    await client.place_order(token_id="...", side="BUY", price=0.5, size=10)

    # Reads — DO NOT use this client; use LiveDataSource instead:
    from polyecho.shared.datasource.live import LiveDataSource
    async with LiveDataSource() as ds:
        leaders = await ds.get_leaderboard(limit=200)
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
    # Retired read-only stubs (P0-1)
    # ------------------------------------------------------------------
    # These four method signatures are kept ONLY because they are part of
    # the published PolymarketClient surface (proxy-env test, README,
    # Phase-4 contract test).  Production reads do NOT route through this
    # client:
    #
    #   * Data-API reads (leaderboard, activity, positions) go through
    #     polyecho.shared.datasource.live.LiveDataSource — which already
    #     has retry, a circuit breaker, internal pagination, proxy
    #     routing via make_async_client_resolved, and returns typed
    #     DTOs (LeaderboardEntry / ActivityRecord / PositionRecord).
    #     Re-implementing here would duplicate that pipeline with a
    #     weaker (list[dict[str, Any]]) return type.
    #
    #   * The CLOB orderbook is supplied to the trading path via
    #     polyecho.core.trader.Trader's ``orderbook_for`` callable
    #     injection — not a method on this client.
    #
    # Any caller that lands on one of these stubs is a wiring bug.
    # Surface that loudly with a RuntimeError naming the canonical
    # alternative; do NOT raise NotImplementedError (which historically
    # read as "Phase 1 TODO, just hasn't been written yet").

    _RETIRED_READ_HINT = (
        "PolymarketClient.{method}() is intentionally not implemented. "
        "{alt}"
    )

    async def get_leaderboard(
        self,
        *,
        window: str = "1m",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Retired stub — use ``LiveDataSource.get_leaderboard`` instead.

        Args:
            window: (Unused — kept for signature stability.)
            limit:  (Unused — kept for signature stability.)

        Raises:
            RuntimeError: Always.  Use
                :class:`polyecho.shared.datasource.live.LiveDataSource`.
        """
        del window, limit  # mark args as intentionally unused
        raise RuntimeError(
            self._RETIRED_READ_HINT.format(
                method="get_leaderboard",
                alt=(
                    "Use `async with LiveDataSource() as ds: "
                    "await ds.get_leaderboard(...)` "
                    "(polyecho.shared.datasource.live.LiveDataSource) — "
                    "returns typed LeaderboardEntry DTOs with retry / "
                    "circuit-breaker / proxy."
                ),
            )
        )

    async def get_user_activity(
        self,
        wallet_address: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retired stub — use ``LiveDataSource.get_user_activity`` instead.

        Raises:
            RuntimeError: Always.  Use
                :class:`polyecho.shared.datasource.live.LiveDataSource`.
        """
        del wallet_address, limit, offset  # intentionally unused
        raise RuntimeError(
            self._RETIRED_READ_HINT.format(
                method="get_user_activity",
                alt=(
                    "Use `async with LiveDataSource() as ds: "
                    "await ds.get_user_activity(wallet, ...)` "
                    "(polyecho.shared.datasource.live.LiveDataSource) — "
                    "returns typed ActivityRecord DTOs with retry / "
                    "circuit-breaker / proxy / bounded pagination."
                ),
            )
        )

    async def get_positions(
        self,
        wallet_address: str,
    ) -> list[dict[str, Any]]:
        """Retired stub — use ``LiveDataSource.get_positions`` instead.

        Raises:
            RuntimeError: Always.  Use
                :class:`polyecho.shared.datasource.live.LiveDataSource`.
        """
        del wallet_address  # intentionally unused
        raise RuntimeError(
            self._RETIRED_READ_HINT.format(
                method="get_positions",
                alt=(
                    "Use `async with LiveDataSource() as ds: "
                    "await ds.get_positions(wallet)` "
                    "(polyecho.shared.datasource.live.LiveDataSource) — "
                    "returns typed PositionRecord DTOs with retry / "
                    "circuit-breaker / proxy / bounded pagination."
                ),
            )
        )

    async def get_orderbook(
        self,
        token_id: str,
    ) -> dict[str, Any]:
        """Retired stub — orderbook is injected as a callable, not fetched here.

        The trading path (``polyecho.core.trader.Trader``) accepts an
        ``orderbook_for: Callable[[str], Awaitable[dict[str, Any]]]``
        injection (see trader.py); the CLOB orderbook is never read via
        this client.

        Raises:
            RuntimeError: Always.  Inject an ``orderbook_for`` callable
                into ``Trader`` instead.
        """
        del token_id  # intentionally unused
        raise RuntimeError(
            self._RETIRED_READ_HINT.format(
                method="get_orderbook",
                alt=(
                    "Inject an `orderbook_for: Callable[[str], "
                    "Awaitable[dict[str, Any]]]` into "
                    "polyecho.core.trader.Trader — the trading path "
                    "never reads the CLOB orderbook through "
                    "PolymarketClient."
                ),
            )
        )

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

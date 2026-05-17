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
from typing import Any

from py_clob_client_v2 import ApiCreds, ClobClient  # type: ignore[import-untyped]

from polyecho.shared.config import get_settings


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
    ) -> dict[str, Any]:
        """Place a limit order on the Polymarket CLOB.

        Args:
            token_id:   Outcome token ID (e.g. the YES token for a market).
            side:       ``"BUY"`` or ``"SELL"``.
            price:      Limit price per share (0.01 – 0.99).
            size:       Number of shares.
            order_type: ``"GTC"`` (Good-Till-Cancelled), ``"FOK"``
                        (Fill-Or-Kill), or ``"IOC"`` (Immediate-Or-Cancel).
                        PRD §5.5 defaults to FOK for copy-trades.
            tick_size:  Minimum price increment — ``"0.1"``, ``"0.01"``,
                        ``"0.001"``, ``"0.0001"``.

        Returns:
            Response dict from CLOB including ``orderID`` and ``status``.

        Raises:
            NotImplementedError: Phase 4 deliverable.
        """
        raise NotImplementedError(
            "Phase 4: implement place_order via ClobClient.create_and_post_order()"
        )

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an open limit order by its CLOB order ID.

        Args:
            order_id: The CLOB order ID (hex string returned by
                      :meth:`place_order`).

        Returns:
            Response dict confirming cancellation.

        Raises:
            NotImplementedError: Phase 4 deliverable.
        """
        raise NotImplementedError(
            "Phase 4: implement cancel_order via ClobClient.cancel_order()"
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

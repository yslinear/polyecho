"""polyecho.shared.polymarket — Polymarket API integration layer.

Phase-4 W1 surface:
* :class:`PolymarketClient`           — async py-clob-client-v2 wrapper.
* :func:`resolve_wallet`              — per-account ``(sig_type, funder)``
                                        resolver (brief §9).
* :func:`collateral_for` /            — redeem helpers + the frozen
  :func:`index_set_for` / interfaces    :class:`RedeemClient` contract.
"""

from polyecho.shared.polymarket.client import PolymarketClient
from polyecho.shared.polymarket.redeem import (
    BaseRedeemClient,
    RedeemClient,
    collateral_for,
    index_set_for,
)
from polyecho.shared.polymarket.wallet import (
    AccountLike,
    SafeProxyResolver,
    WalletMode,
    magic_proxy_address,
    resolve_wallet,
)

__all__ = [
    "PolymarketClient",
    "resolve_wallet",
    "magic_proxy_address",
    "WalletMode",
    "AccountLike",
    "SafeProxyResolver",
    "collateral_for",
    "index_set_for",
    "RedeemClient",
    "BaseRedeemClient",
]

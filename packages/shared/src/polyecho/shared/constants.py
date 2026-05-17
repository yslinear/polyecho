"""
polyecho.shared.constants
==========================
Centralised Polygon-mainnet on-chain + Polymarket endpoint constants for
Phase-4 live copy-trading (PolyHermes design brief §9 — Phase-4 binding).

Rationale
---------
PolyHermes centralises every CLOB / Gamma / data-api / WSS URL and every
on-chain address in one ``PolymarketConstants`` object (brief §1).  PolyEcho
mirrors that here so the live trading + redeem paths never hardcode
addresses inline (brief §8 "do NOT copy: hardcoded exchange addresses").

This module performs **no** network or DB I/O and has no side effects on
import.  Every constant is checksummed Polygon-mainnet (chain id 137) and
cited to the brief section that fixed it.

.. warning:: The Magic / Safe CREATE2 factory constants are documented in
   the brief §9 table BUT the standing rule (brief §6/§9) still holds:
   **prefer an explicit operator-confirmed funder; treat derivation as a
   convenience to verify, never to trust blindly** (especially type-3
   POLY_1271, which has no PolyHermes reference at all).  These are isolated
   here so a verified value is a one-line change.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
#: Polygon mainnet EVM chain id.  Brief §9 (all addresses below are Polygon).
POLYGON_CHAIN_ID: Final[int] = 137

# ---------------------------------------------------------------------------
# Conditional Tokens Framework (CTF) — redeem / claim-winnings (brief §9)
# ---------------------------------------------------------------------------
#: Gnosis Conditional Tokens contract.  ``redeemPositions`` target for
#: claim-winnings.  Brief §9 ("CTF 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045").
CTF_ADDRESS: Final[str] = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

#: Parent collection id passed to ``redeemPositions`` (always the zero hash
#: for top-level Polymarket conditions).  Brief §9
#: ("redeemPositions(collateral, 0x0(parentCollectionId), conditionId, …)").
PARENT_COLLECTION_ID: Final[str] = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
)

# ---------------------------------------------------------------------------
# Collateral tokens (brief §9 — "highest-risk detail to get right")
# ---------------------------------------------------------------------------
#: pUSD — collateral for **normal** (non-NegRisk) markets.  Brief §9
#: ("Collateral = pUSD 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB").
PUSD_ADDRESS: Final[str] = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

#: WrappedCollateral (WCOL) — collateral for **NegRisk** markets.  NegRisk
#: redeem pays WCOL which must then be ``unwrap(to, amount)``-ed.  Brief §9
#: ("WrappedCollateral 0x3A3BD7bb9528E159577F7C2e685CC81A765002E2").
WRAPPED_COLLATERAL_ADDRESS: Final[str] = (
    "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"
)

# ---------------------------------------------------------------------------
# Exchange contracts (brief §2/§9 — NegRisk selects a different exchange,
# orthogonal to wallet mode; passed through to py-clob-client-v2).
# ---------------------------------------------------------------------------
#: CTF Exchange — normal-market order settlement.  Brief §2/§9.
CTF_EXCHANGE_ADDRESS: Final[str] = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
#: NegRisk CTF Exchange — NegRisk-market order settlement.  Brief §2/§9.
NEG_RISK_CTF_EXCHANGE_ADDRESS: Final[str] = (
    "0xC5d563A36AE78145C45a50134d48A1215220f80a"
)
#: NegRisk Adapter — wraps/unwraps collateral for NegRisk markets.  Brief §9.
NEG_RISK_ADAPTER_ADDRESS: Final[str] = (
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
)

# ---------------------------------------------------------------------------
# Wallet-mode proxy factories (brief §9 per-wallet-mode table)
# ---------------------------------------------------------------------------
#: Magic / email proxy factory (signature_type 1, POLY_PROXY).  CREATE2
#: deployer.  Brief §9 ("Magic proxy factory
#: 0xaB45c5A4B0c941a2F231C04C3f49182e1A254052").
MAGIC_PROXY_FACTORY: Final[str] = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

#: CREATE2 init-code hash for the Magic proxy.  Brief §9 ("initCodeHash
#: 0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b").
MAGIC_PROXY_INIT_CODE_HASH: Final[str] = (
    "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
)

#: Gnosis-Safe proxy factory (signature_type 2, POLY_GNOSIS_SAFE).  Resolve
#: the funder via RPC ``computeProxyAddress(eoa)`` on this factory.  Brief §9
#: ("Safe factory 0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b").
SAFE_PROXY_FACTORY: Final[str] = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"

# ---------------------------------------------------------------------------
# Endpoints (brief §1/§3/§9 — centralised, like PolyHermes PolymarketConstants)
# ---------------------------------------------------------------------------
#: Polymarket CLOB REST base.  Brief §2 (CLOB-direct trading).
CLOB_BASE_URL: Final[str] = "https://clob.polymarket.com"
#: Polymarket Gamma markets API base (negRisk + tickSize lookup).  Brief §2.
GAMMA_BASE_URL: Final[str] = "https://gamma-api.polymarket.com"
#: Polymarket Data API base (activity / positions / leaderboard / redeemable).
#: Brief §3/§9 (redeemable flag is polled here).
DATA_API_BASE_URL: Final[str] = "https://data-api.polymarket.com"

#: Authenticated WS **User Channel** (structured per-user events — brief §3
#: path A, strictly better than the global-stream substring scan).
CLOB_WSS_USER_URL: Final[str] = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
#: Market WS channel (orderbook / price changes).  Brief §3.
CLOB_WSS_MARKET_URL: Final[str] = (
    "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)

#: Default Polygon JSON-RPC (path-B on-chain log cross-check + EOA tx +
#: Safe ``computeProxyAddress``).  Brief §3/§9.
POLYGON_RPC_URL_DEFAULT: Final[str] = "https://polygon-rpc.com"

#: Official Builder-Relayer endpoint for gasless redeem/unwrap (PRD §11.2;
#: brief §9 — Magic PROXY / Safe SafeTx execution).
BUILDER_RELAYER_URL: Final[str] = "https://relayer-v2.polymarket.com"

#: ERC-20 max allowance — ``approve(spender, 2**256-1)`` once at account
#: setup so redeem never needs a per-call approval.  Brief §9.
MAX_UINT256: Final[int] = (1 << 256) - 1


__all__ = [
    "POLYGON_CHAIN_ID",
    "CTF_ADDRESS",
    "PARENT_COLLECTION_ID",
    "PUSD_ADDRESS",
    "WRAPPED_COLLATERAL_ADDRESS",
    "CTF_EXCHANGE_ADDRESS",
    "NEG_RISK_CTF_EXCHANGE_ADDRESS",
    "NEG_RISK_ADAPTER_ADDRESS",
    "MAGIC_PROXY_FACTORY",
    "MAGIC_PROXY_INIT_CODE_HASH",
    "SAFE_PROXY_FACTORY",
    "CLOB_BASE_URL",
    "GAMMA_BASE_URL",
    "DATA_API_BASE_URL",
    "CLOB_WSS_USER_URL",
    "CLOB_WSS_MARKET_URL",
    "POLYGON_RPC_URL_DEFAULT",
    "BUILDER_RELAYER_URL",
    "MAX_UINT256",
]

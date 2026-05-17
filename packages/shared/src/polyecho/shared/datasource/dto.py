"""
polyecho.shared.datasource.dto
==============================
Frozen dataclasses representing the public data-transfer objects (DTOs)
for the datasource layer.  All datetimes are tz-aware UTC; wallet addresses
are normalised to lowercase ``0x…``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class LeaderboardEntry:
    """One row from the Polymarket leaderboard.

    Attributes:
        rank:     1-based rank within the requested query window.
        wallet:   Lowercase proxy-wallet address (``0x…``).
        username: Display name, or ``None`` if not set.
        pnl:      Profit-and-loss in USD over the requested period.
        volume:   Total trading volume in USD over the requested period.
        verified: Whether the account has a verified badge.
    """

    rank: int
    wallet: str
    username: str | None
    pnl: float
    volume: float
    verified: bool


@dataclass(frozen=True)
class ActivityRecord:
    """One activity event for a wallet.

    Attributes:
        wallet:        Lowercase wallet address.
        activity_type: E.g. ``"TRADE"``, ``"REDEEM"``, ``"REWARD"`` …
        condition_id:  Market condition ID, or ``None`` for non-trade events.
        side:          ``"BUY"`` / ``"SELL"``, or ``None``.
        size:          Number of shares/tokens.
        price:         Price per share, or ``None``.
        usdc_size:     USDC notional, or ``None``.
        timestamp:     UTC-aware datetime of the event.
        raw:           Original API response dict (kept for forward-compat).
    """

    wallet: str
    activity_type: str
    condition_id: str | None
    side: str | None
    size: float
    price: float | None
    usdc_size: float | None
    timestamp: datetime
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PositionRecord:
    """One open position for a wallet.

    Attributes:
        wallet:        Lowercase wallet address.
        condition_id:  Market condition ID.
        asset:         Token/asset identifier.
        size:          Number of shares held.
        avg_price:     Average purchase price per share.
        current_value: Current mark-to-market value in USD.
        cash_pnl:      Realised + unrealised PnL in USD.
        percent_pnl:   PnL as a percentage of initial value.
        title:         Human-readable market title, or ``None``.
        outcome:       Outcome name (e.g. ``"Yes"``/``"No"``), or ``None``.
        raw:           Original API response dict.
    """

    wallet: str
    condition_id: str
    asset: str
    size: float
    avg_price: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    title: str | None
    outcome: str | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class MarketSummary:
    """Summary of one Polymarket market.

    Attributes:
        condition_id: The condition ID used across activity/positions APIs.
        market_id:    Polymarket internal market ID.
        question:     Human-readable market question.
        volume_24h:   24-hour trading volume in USD.
        end_date:     Resolution date/time (UTC-aware), or ``None`` if unknown.
        tokens:       List of token dicts with ``token_id``, ``outcome``,
                      ``price`` keys.
        raw:          Original API response dict.
    """

    condition_id: str
    market_id: str
    question: str
    volume_24h: float
    end_date: datetime | None
    tokens: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

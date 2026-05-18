"""
polyecho.core.onchain_reconcile
===============================
On-chain ↔ DB position / balance reconcile (PRD §7.3 / §7.4; remediation
plan Wave C · C-observability "鏈上↔DB 對帳模組").

What this is (and is NOT)
-------------------------
``core.reconcile`` only backfills *leader* trades from the datasource.
This module is the **missing** half: it reads the **on-chain truth**
(USDC / pUSD ERC-20 balance + CTF ERC-1155 conditional-token balances)
for each of our own accounts and compares it against what the DB believes
(``my_trades`` FIFO-derived open positions + the per-account collateral
balance).  Any divergence ("drift") is reported and a single WARNING
event is emitted so the operator / Dashboard sees it.

**Read-only & lock-independent.** This module NEVER builds, signs or
sends a transaction and NEVER touches the order / redeem path.  It only
issues ``eth_call`` (``balanceOf``) reads and ``SELECT``s, so it is safe
to run at any time — at bot startup, on the daily cadence, or on demand
via ``polyecho db reconcile`` — with no live-lock interaction at all.

Fail-safe
---------
A per-account RPC error is logged and that account is skipped; the pass
continues for every other account (mirrors the Wave-B per-leader
isolation in :mod:`polyecho.core.reconcile`).  A reconcile is best-effort
observability — it must never abort or crash a startup.

Frozen contract (C-cli depends on these EXACT names)
----------------------------------------------------
* ``async def run_reconcile(*, session=None) -> ReconcileReport`` — one
  pass; returns the summary dataclass.  Called at bot startup and by
  ``polyecho db reconcile``.
* ``async def run_forever(...)`` — the daily-cadence loop (settings.tz).
* ``python -m polyecho.core.onchain_reconcile [--once]`` — module
  entrypoint; ``--once`` does a single pass and exits 0 on success
  (including "no drift"), non-zero only on a real error.

Injection / testability
------------------------
The on-chain reader and the DB readers are injected (defaulting to the
real web3.py / SQLAlchemy implementations) so the whole module is unit
testable with a fake web3 + a fixture DB and **zero** network.

context7-verified web3.py v7 (``/ethereum/web3.py`` v7.12.0)
------------------------------------------------------------
* ``AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(url))`` — non-persistent HTTP
  provider needs no explicit connect/disconnect.
* ``await w3.is_connected()`` is a coroutine on ``AsyncWeb3``.
* ``w3.eth.contract(address=…, abi=…)`` then
  ``await c.functions.balanceOf(addr).call()`` (coroutine on AsyncWeb3).
* ``Web3.to_checksum_address(addr)`` normalises an address.
* ERC-20 ``balanceOf(owner)`` (1 arg) / ERC-1155
  ``balanceOf(account,id)`` (2 args) minimal ABI fragments below.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polyecho.core.events import EventType, PushPayload, Severity, safe_emit
from polyecho.shared.config import get_settings
from polyecho.shared.constants import PUSD_ADDRESS

logger = logging.getLogger(__name__)

__all__ = [
    "AccountOnChain",
    "BalanceDrift",
    "OnChainReader",
    "PositionDrift",
    "ReconcileReport",
    "TrackedPosition",
    "Web3OnChainReader",
    "main",
    "run_forever",
    "run_reconcile",
]

# ---------------------------------------------------------------------------
# Minimal ABI fragments (context7-verified shapes — view ``balanceOf`` only;
# this module never sends a tx so no nonpayable fragments are needed).
# ---------------------------------------------------------------------------
#: ERC-20 ``balanceOf(address) -> uint256`` (pUSD / USDC collateral).
ERC20_BALANCE_ABI: list[dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
#: ERC-1155 ``balanceOf(address,uint256) -> uint256`` (CTF outcome tokens).
ERC1155_BALANCE_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

#: USDC/pUSD has 6 decimals on Polygon; CTF ERC-1155 outcome shares are
#: integer units (1 share = 1e0).  Tolerances are generous on purpose —
#: this is drift *observability*, not accounting; sub-cent / sub-share
#: float noise from the NUMERIC→Decimal round-trip must not page anyone.
_COLLATERAL_DECIMALS = Decimal(10) ** 6
#: Collateral drift below this many tokens is treated as "matched".
_BALANCE_TOLERANCE = Decimal("0.01")
#: Position (share) drift below this is treated as "matched".
_POSITION_TOLERANCE = Decimal("0.000001")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TrackedPosition:
    """One ``(condition_id, outcome)`` position we believe we hold.

    ``ctf_token_id`` is the ERC-1155 token id (the CTF *positionId*) for
    this outcome.  Deriving it from ``condition_id`` requires the
    collateral + collection-id hashing the CTF performs on-chain; that
    derivation is owned by the redeem/trade path, so here it is supplied
    by the injected DB reader (kept out of this read-only comparator).
    """

    account_id: str
    market_id: str
    condition_id: str | None
    outcome: str
    ctf_token_id: int | None
    db_shares: Decimal


@dataclass(frozen=True)
class AccountOnChain:
    """The on-chain truth read for one account."""

    account_id: str
    funder: str
    collateral_balance: Decimal
    #: ``ctf_token_id -> on-chain ERC-1155 balance`` (shares).
    position_balances: dict[int, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class BalanceDrift:
    """Collateral (USDC/pUSD) expected-vs-onchain mismatch for an account."""

    account_id: str
    funder: str
    db_balance: Decimal
    onchain_balance: Decimal

    @property
    def delta(self) -> Decimal:
        return self.onchain_balance - self.db_balance


@dataclass(frozen=True)
class PositionDrift:
    """A ``(condition_id, outcome)`` share-count expected-vs-onchain miss."""

    account_id: str
    market_id: str
    condition_id: str | None
    outcome: str
    ctf_token_id: int | None
    db_shares: Decimal
    onchain_shares: Decimal

    @property
    def delta(self) -> Decimal:
        return self.onchain_shares - self.db_shares


@dataclass
class ReconcileReport:
    """Summary of one reconcile pass (the frozen public return type).

    ``has_drift`` is the single boolean C-cli / the daily loop key off.
    ``accounts_failed`` records accounts skipped due to an RPC error so a
    partial pass is observable (it is NOT itself "drift").
    """

    started_at: datetime
    accounts_checked: int = 0
    accounts_failed: list[str] = field(default_factory=list)
    matched_balances: int = 0
    matched_positions: int = 0
    balance_drift: list[BalanceDrift] = field(default_factory=list)
    position_drift: list[PositionDrift] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.balance_drift or self.position_drift)

    @property
    def drift_count(self) -> int:
        return len(self.balance_drift) + len(self.position_drift)

    def summary(self) -> dict[str, Any]:
        """A flat, log/event-friendly dict (no nested dataclasses)."""
        return {
            "accounts_checked": self.accounts_checked,
            "accounts_failed": list(self.accounts_failed),
            "matched_balances": self.matched_balances,
            "matched_positions": self.matched_positions,
            "balance_drift": [
                {
                    "account_id": d.account_id,
                    "funder": d.funder,
                    "db": str(d.db_balance),
                    "onchain": str(d.onchain_balance),
                    "delta": str(d.delta),
                }
                for d in self.balance_drift
            ],
            "position_drift": [
                {
                    "account_id": d.account_id,
                    "market_id": d.market_id,
                    "condition_id": d.condition_id,
                    "outcome": d.outcome,
                    "ctf_token_id": d.ctf_token_id,
                    "db": str(d.db_shares),
                    "onchain": str(d.onchain_shares),
                    "delta": str(d.delta),
                }
                for d in self.position_drift
            ],
        }


# ---------------------------------------------------------------------------
# Injected reader protocols
# ---------------------------------------------------------------------------
#: ``async (session) -> {account_id: (funder, db_collateral, [TrackedPosition])}``
#: — the DB side: per-account funder, recorded collateral balance and the
#: FIFO-derived open positions.  Injected so tests pass a fixture DB.
DbStateReader = Callable[
    [Any],
    Awaitable[dict[str, tuple[str, Decimal, list[TrackedPosition]]]],
]


class OnChainReader:
    """Reads on-chain balances for one account.  Protocol (duck-typed).

    Implementations expose a single coroutine
    ``read(funder, ctf_token_ids) -> (collateral, {token_id: shares})``.
    A real implementation talks to Polygon over web3.py; the test double
    is a trivial in-memory map.
    """

    async def read(
        self, funder: str, ctf_token_ids: Sequence[int]
    ) -> tuple[Decimal, dict[int, Decimal]]:  # pragma: no cover - interface
        raise NotImplementedError


class Web3OnChainReader(OnChainReader):
    """Real Polygon reader via web3.py v7 (``AsyncWeb3`` + HTTP provider).

    Lazily constructs ``AsyncWeb3`` so importing this module never touches
    the network and never requires a node (matches every other
    import-side-effect-free module in ``polyecho``).  Read-only: only
    ``eth_call`` (``balanceOf``) is ever issued.

    Args:
        rpc_url:           Polygon JSON-RPC; defaults to
                           ``settings.polygon_rpc_url``.
        collateral_address: ERC-20 collateral to read; defaults to pUSD
                           (the normal-market collateral, brief §9).
        ctf_address:       CTF ERC-1155 contract; defaults to the frozen
                           ``constants.CTF_ADDRESS``.
    """

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        collateral_address: str | None = None,
        ctf_address: str | None = None,
    ) -> None:
        from polyecho.shared.constants import CTF_ADDRESS

        self._rpc_url = rpc_url or get_settings().polygon_rpc_url
        self._collateral_address = collateral_address or PUSD_ADDRESS
        self._ctf_address = ctf_address or CTF_ADDRESS
        self._w3: Any = None

    def _client(self) -> Any:
        if self._w3 is None:
            # Imported here (not at module top) so the module imports
            # with zero web3 side effects and tests need no node.
            from web3 import AsyncWeb3

            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self._rpc_url))
        return self._w3

    async def read(
        self, funder: str, ctf_token_ids: Sequence[int]
    ) -> tuple[Decimal, dict[int, Decimal]]:
        from web3 import Web3

        w3 = self._client()
        account = Web3.to_checksum_address(funder)

        erc20 = w3.eth.contract(
            address=Web3.to_checksum_address(self._collateral_address),
            abi=ERC20_BALANCE_ABI,
        )
        raw_collateral = await erc20.functions.balanceOf(account).call()
        collateral = Decimal(int(raw_collateral)) / _COLLATERAL_DECIMALS

        positions: dict[int, Decimal] = {}
        if ctf_token_ids:
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(self._ctf_address),
                abi=ERC1155_BALANCE_ABI,
            )
            for token_id in ctf_token_ids:
                raw = await ctf.functions.balanceOf(account, int(token_id)).call()
                positions[int(token_id)] = Decimal(int(raw))
        return collateral, positions


# ---------------------------------------------------------------------------
# Default DB reader — FIFO-derived open positions from ``my_trades``
# ---------------------------------------------------------------------------
async def _default_db_state(
    session: Any,
) -> dict[str, tuple[str, Decimal, list[TrackedPosition]]]:
    """Read each account's funder + DB-believed collateral + open positions.

    Open position shares are the FIFO-unconsumed BUY quantity: the sum of
    ``remaining_qty`` over *filled BUY* ``my_trades`` rows, grouped by
    ``(account, market_id)`` — exactly the invariant the trader maintains
    (``remaining_qty`` = unmatched BUY qty still available for FIFO exits;
    a fully sold position nets to 0 and is not reported).

    The recorded collateral balance is **not** a column we persist
    per-account, so DB-side collateral is reported as the realised-PnL
    running total (``SUM(pnl_usd)``) which is the only DB collateral
    signal available; a non-zero on-chain balance with no DB signal is
    surfaced as drift so the operator can reconcile manually (the module
    never invents a "correct" number — it only flags divergence).
    """
    from sqlalchemy import select

    from polyecho.shared.models import Account
    from polyecho.shared.models.my_trade import MyTrade

    out: dict[str, tuple[str, Decimal, list[TrackedPosition]]] = {}

    acct_rows = (await session.execute(select(Account))).scalars().all()
    for acct in acct_rows:
        account_id = str(acct.id)
        funder = (acct.funder_address or acct.eoa_address or "").strip()
        if not funder:
            # No on-chain identity to read against — skip silently
            # (an account mid key-import has no funder yet).
            continue

        trades = (
            (
                await session.execute(
                    select(MyTrade).where(MyTrade.account_id == acct.id)
                )
            )
            .scalars()
            .all()
        )
        # FIFO open shares per market: sum unmatched BUY remaining_qty.
        shares_by_market: dict[str, Decimal] = {}
        pnl_total = Decimal(0)
        for t in trades:
            if t.pnl_usd is not None:
                pnl_total += Decimal(str(t.pnl_usd))
            if (t.fill_status or "") not in ("filled", "partial"):
                continue
            if (t.side or "").upper() != "BUY":
                continue
            market = (t.market_id or "").strip()
            if not market:
                continue
            remaining = t.remaining_qty
            if remaining is None:
                remaining = t.size if t.size is not None else 0
            shares_by_market[market] = shares_by_market.get(
                market, Decimal(0)
            ) + Decimal(str(remaining))

        positions = [
            TrackedPosition(
                account_id=account_id,
                market_id=market,
                condition_id=None,
                outcome="",
                ctf_token_id=None,
                db_shares=shares,
            )
            for market, shares in sorted(shares_by_market.items())
            if shares != 0
        ]
        out[account_id] = (funder, pnl_total, positions)
    return out


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------
async def _reconcile_account(
    *,
    account_id: str,
    funder: str,
    db_collateral: Decimal,
    positions: list[TrackedPosition],
    reader: OnChainReader,
    report: ReconcileReport,
) -> None:
    """Compare one account; append matched/drift to *report*.

    Any exception here is the caller's per-account isolation boundary —
    it is logged and the account is recorded as failed, the pass goes on.
    """
    token_ids = [p.ctf_token_id for p in positions if p.ctf_token_id is not None]
    onchain_collateral, onchain_positions = await reader.read(funder, token_ids)

    report.accounts_checked += 1

    # --- collateral ---------------------------------------------------
    if (db_collateral - onchain_collateral).copy_abs() <= _BALANCE_TOLERANCE:
        report.matched_balances += 1
    else:
        report.balance_drift.append(
            BalanceDrift(
                account_id=account_id,
                funder=funder,
                db_balance=db_collateral,
                onchain_balance=onchain_collateral,
            )
        )

    # --- positions ----------------------------------------------------
    for pos in positions:
        if pos.ctf_token_id is None:
            # We cannot read on-chain shares without the ERC-1155 token
            # id; record it as drift only if the DB thinks it is open so
            # the operator knows it is *unverifiable*, never as matched.
            if pos.db_shares != 0:
                report.position_drift.append(
                    PositionDrift(
                        account_id=account_id,
                        market_id=pos.market_id,
                        condition_id=pos.condition_id,
                        outcome=pos.outcome,
                        ctf_token_id=None,
                        db_shares=pos.db_shares,
                        onchain_shares=Decimal(0),
                    )
                )
            continue
        onchain_shares = onchain_positions.get(pos.ctf_token_id, Decimal(0))
        if (pos.db_shares - onchain_shares).copy_abs() <= _POSITION_TOLERANCE:
            report.matched_positions += 1
        else:
            report.position_drift.append(
                PositionDrift(
                    account_id=account_id,
                    market_id=pos.market_id,
                    condition_id=pos.condition_id,
                    outcome=pos.outcome,
                    ctf_token_id=pos.ctf_token_id,
                    db_shares=pos.db_shares,
                    onchain_shares=onchain_shares,
                )
            )


# ---------------------------------------------------------------------------
# Public API (FROZEN — C-cli depends on these exact signatures)
# ---------------------------------------------------------------------------
async def run_reconcile(
    *,
    session: Any = None,
    reader: OnChainReader | None = None,
    db_state: DbStateReader | None = None,
) -> ReconcileReport:
    """Run one on-chain ↔ DB reconcile pass and return the summary.

    Read-only and lock-independent: issues only ``balanceOf`` ``eth_call``
    reads and ``SELECT``s.  Safe to call at bot startup, on the daily
    cadence, or on demand from ``polyecho db reconcile``.

    Per-account RPC errors are isolated (logged + recorded in
    ``report.accounts_failed``) so one bad account never aborts the pass.
    On any drift a single WARNING event is emitted via the best-effort
    :func:`polyecho.core.events.safe_emit` (it can never raise here).

    Args:
        session:  Optional caller :class:`AsyncSession`; if ``None`` a
                  :func:`polyecho.shared.db.session_scope` is opened.
        reader:   Optional :class:`OnChainReader`; defaults to the real
                  :class:`Web3OnChainReader` (lazy — no node on import).
        db_state: Optional DB reader override (defaults to the
                  ``my_trades`` FIFO derivation).

    Returns:
        A :class:`ReconcileReport`.  ``report.has_drift`` is ``False``
        for the clean "no drift" case (still a success / exit 0).
    """
    reader = reader if reader is not None else Web3OnChainReader()
    db_state = db_state if db_state is not None else _default_db_state
    report = ReconcileReport(started_at=datetime.now(tz=UTC))

    async def _run(sess: Any) -> None:
        state = await db_state(sess)
        for account_id, (funder, db_collateral, positions) in state.items():
            try:
                await _reconcile_account(
                    account_id=account_id,
                    funder=funder,
                    db_collateral=db_collateral,
                    positions=positions,
                    reader=reader,
                    report=report,
                )
            except Exception as exc:  # noqa: BLE001 - per-account isolation
                report.accounts_failed.append(account_id)
                logger.warning(
                    "onchain_reconcile_account_failed_continuing",
                    extra={"account_id": account_id, "error": str(exc)},
                )
                continue

    if session is not None:
        await _run(session)
    else:
        from polyecho.shared.db import session_scope

        async with session_scope() as owned:
            await _run(owned)

    if report.has_drift:
        logger.warning(
            "onchain_reconcile_drift_detected",
            extra=report.summary(),
        )
        await safe_emit(
            event_type=EventType.RISK_STRATEGY_DRIFT,
            severity=Severity.WARNING,
            payload={
                "kind": "onchain_db_reconcile",
                **report.summary(),
            },
            push=PushPayload(
                title="鏈上↔DB 對帳偵測到差異",
                body=(
                    f"{report.drift_count} 筆差異於 "
                    f"{report.accounts_checked} 個帳戶"
                ),
                url="/",
                tag="onchain-reconcile-drift",
            ),
            session=session,
        )
    else:
        logger.info(
            "onchain_reconcile_clean",
            extra=report.summary(),
        )
    return report


async def run_forever(
    *,
    interval_seconds: float = 86_400.0,
    reader: OnChainReader | None = None,
    db_state: DbStateReader | None = None,
    _max_iterations: int | None = None,
) -> None:
    """Daily-cadence reconcile loop (PRD §7.4; ``settings.tz`` aware).

    A loop iteration that raises is logged and the loop continues after
    the interval (observability must never crash the supervised process).
    ``_max_iterations`` is a test-only bound; production passes ``None``
    for an unbounded daily loop.
    """
    settings = get_settings()
    logger.info(
        "onchain_reconcile_loop_start",
        extra={"interval_seconds": interval_seconds, "tz": settings.tz},
    )
    iterations = 0
    while True:
        try:
            await run_reconcile(reader=reader, db_state=db_state)
        except Exception as exc:  # noqa: BLE001 - never crash the daily loop
            logger.warning(
                "onchain_reconcile_loop_iteration_failed_continuing",
                extra={"error": str(exc)},
            )
        iterations += 1
        if _max_iterations is not None and iterations >= _max_iterations:
            return
        await asyncio.sleep(interval_seconds)


# ---------------------------------------------------------------------------
# Module entrypoint (FROZEN — C-cli invokes ``python -m`` with ``--once``)
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    """``python -m polyecho.core.onchain_reconcile`` entrypoint.

    ``--once`` (default) runs a single pass and exits ``0`` on success —
    *including* the clean "no drift" case — and non-zero only on a real
    error (e.g. the DB is unreachable).  Detected drift is a successful
    *observation* (exit 0); the WARNING event / log is the signal.
    """
    parser = argparse.ArgumentParser(
        prog="python -m polyecho.core.onchain_reconcile",
        description="On-chain ↔ DB position/balance reconcile (read-only).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run a single reconcile pass (default).",
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="Run the daily-cadence loop instead of a single pass.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=86_400.0,
        help="Loop interval when --forever (default: 86400 = 1 day).",
    )
    args = parser.parse_args(argv)

    try:
        if args.forever:
            asyncio.run(run_forever(interval_seconds=args.interval_seconds))
            return 0
        report = asyncio.run(run_reconcile())
    except Exception:
        logger.exception("onchain_reconcile_failed")
        return 1

    # Drift is a successful observation — exit 0; the WARNING event is
    # the operator signal (a non-zero here would wedge bot startup).
    logger.info(
        "onchain_reconcile_done",
        extra={"has_drift": report.has_drift, "drift_count": report.drift_count},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

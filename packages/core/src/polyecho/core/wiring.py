"""
polyecho.core.wiring
====================
C-wire — inject the real DB-backed collaborators into the trader-core
supervised loop / entrypoint (remediation plan Wave C · "C-wire").

Several Wave-B/C collaborators are *implemented + tested* but were
**injectable-and-not-injected** (their hooks defaulted to no-op / None),
so they were DORMANT in production.  This module is the thin, side-effect
free seam that builds the concrete implementations and runs them — WITHOUT
rewriting the engine:

* :func:`make_heartbeat_writer` — a real ``HeartbeatWriter`` that upserts
  the ``settings`` row ``trader_heartbeat`` with the exact shape the web
  ``lib/settings-keys.ts`` ``coerceHeartbeat`` reads (ISO ``ts`` + phase).
  Injected into :class:`~polyecho.core.trader.TraderCore` so EVERY
  supervised cycle + startup writes liveness.
* :func:`make_cursor_writer` — a real ``CursorWriter`` persisting the
  per-leader reconcile cursor to ``follow_relations.last_seen_ts`` so
  reconcile is genuinely restart-safe.
* :func:`build_redeem_worker` — the real :class:`RedeemWorker` wired with
  its DB-backed :class:`RedeemablePoller` + :class:`DbJobUpserter` + the
  EOA :class:`Web3RedeemClient` backend, gated on
  ``POLYECHO_AUTO_REDEEM`` (with the flag off the worker does zero
  on-chain / zero DB job side-effects beyond a logged dry-run).
  MAGIC/SAFE/POLY_1271 stay the abstract ``RelayerNotConfiguredError``
  path (untouched).
* :func:`run_supervised` — drives startup ``onchain_reconcile.run_reconcile``
  (read-only), the engine's own supervised pass, the real RedeemWorker,
  then the daily reconcile cadence — all best-effort.

**Fail-safe contract:** every factory and the loop are best-effort — a
wiring or collaborator error is logged and swallowed; it must NEVER
crash the supervised loop.  The ``--once`` exit-code contract is
untouched (this module is only reached on the supervised path;
``--once`` still calls ``core.run_once()``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from polyecho.core.redeem import (
    DbJobUpserter,
    RedeemablePoller,
    RedeemWorker,
)
from polyecho.shared.config import get_settings
from polyecho.shared.copytrading import WalletBinding

logger = logging.getLogger(__name__)

#: settings key the web Dashboard reads (mirrors
#: ``packages/web/lib/settings-keys.ts`` ``TRADER_HEARTBEAT_KEY``).
TRADER_HEARTBEAT_KEY = "trader_heartbeat"

#: C5: the global soft-pause kill-switch settings key.  Mirrors
#: ``packages/web/lib/settings-keys.ts`` ``GLOBAL_PAUSED_KEY`` exactly —
#: the web /settings page writes the SAME row (bare string ``"true"`` /
#: ``"false"``).  trader-core's geo-restriction emergency-stop auto-trips
#: it on a CLOB region-block.
GLOBAL_PAUSED_KEY = "global_paused"

#: operator-supplied passphrase for the encrypted EOA key, sourced from
#: the host env on the LIVE redeem path only.  Never weakens the lock:
#: the double live-lock short-circuits before the key is ever resolved,
#: so with locks off this is never read; with locks on but unset the
#: resolver fails closed (raises) rather than guessing.
_KEY_PASSPHRASE_ENV = "POLYECHO_KEY_PASSPHRASE"


# ---------------------------------------------------------------------------
# 1. HeartbeatWriter — upsert the ``settings`` ``trader_heartbeat`` row
# ---------------------------------------------------------------------------
def make_heartbeat_writer() -> Any:
    """Build a real ``HeartbeatWriter`` (best-effort settings upsert).

    The returned ``async (info: dict) -> None`` upserts the ``settings``
    row keyed ``trader_heartbeat`` with ``info`` verbatim as the JSONB
    value.  ``info`` is already the exact shape ``coerceHeartbeat`` reads
    (``ts`` ISO-8601 + ``phase`` + counters); we store it bare (the web
    ``coerceHeartbeat`` tolerates both bare and ``{value: ...}``).

    Best-effort: any DB error is logged and swallowed — a heartbeat write
    must NEVER crash or stall the trading loop (the engine's
    ``_write_heartbeat`` also already guards this; this is defence in
    depth so a transient DB blip degrades to "stale" on the Dashboard,
    never a crash).
    """

    async def _write(info: dict[str, Any]) -> None:
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from polyecho.shared.db import session_scope
            from polyecho.shared.models import Setting

            stmt = (
                pg_insert(Setting)
                .values(key=TRADER_HEARTBEAT_KEY, value=info)
                .on_conflict_do_update(
                    index_elements=["key"],  # PK column (settings.key)
                    set_={"value": info},
                )
            )
            async with session_scope() as session:
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001 - best-effort liveness
            logger.warning(
                "heartbeat_settings_upsert_failed_continuing",
                extra={"error": str(exc)},
            )

    return _write


# ---------------------------------------------------------------------------
# 1b. GlobalPauseWriter — upsert the ``settings`` ``global_paused`` row (C5)
# ---------------------------------------------------------------------------
def make_global_pause_writer() -> Any:
    """Build a real ``GlobalPauseWriter`` (best-effort settings upsert).

    C5: the returned ``async (paused: bool) -> None`` upserts the
    ``settings`` row keyed ``global_paused`` with the **bare string**
    ``"true"`` / ``"false"`` — the EXACT same shape + key the web
    /settings page writes (``packages/web/app/settings/actions.ts``
    ``setGlobalPaused``), so the geo-restriction emergency-stop trips the
    documented soft-pause kill-switch identically to an operator clicking
    "全域暫停".

    Best-effort: any DB error is logged and swallowed — the engine's
    in-proc ``_geo_halted`` latch already stops new submissions even if
    this durable write fails, so a transient DB blip can never let an
    order through and can never crash the loop.
    """

    async def _write(paused: bool) -> None:
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from polyecho.shared.db import session_scope
            from polyecho.shared.models import Setting

            value = "true" if paused else "false"
            stmt = (
                pg_insert(Setting)
                .values(key=GLOBAL_PAUSED_KEY, value=value)
                .on_conflict_do_update(
                    index_elements=["key"],  # PK column (settings.key)
                    set_={"value": value},
                )
            )
            async with session_scope() as session:
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001 - best-effort emergency-stop
            logger.warning(
                "global_paused_settings_upsert_failed_continuing",
                extra={"paused": paused, "error": str(exc)},
            )

    return _write


# ---------------------------------------------------------------------------
# 1c. TradeRecorder — persist a durable ``leader_trades`` row per copy
#     attempt (A3, PolyHermes deep-dive — the processed_trade.status slot).
# ---------------------------------------------------------------------------
def make_trade_recorder() -> Any:
    """Build a real ``TradeRecorder`` (A3 — durable per-trade copy record).

    The ``leader_trades`` table + :class:`LeaderTrade` ORM model existed
    but were NEVER written: a copy attempt — success OR failure — left no
    durable per-trade record, so only the in-process LRU + the
    ``follow_relations.last_seen_ts`` cursor guarded against a re-attempt
    of a *failed* trade.  This factory closes that gap (A3 in
    ``docs/reference/polyhermes-deep-dive.md``): the returned
    ``async (evaluated, info) -> None`` hook is invoked by
    :meth:`TraderCore._record_trade` on BOTH the placed and the
    failed-attempt path and upserts one ``leader_trades`` row carrying the
    copy-attempt ``status`` (``"success"`` / ``"failed"``).

    Idempotency — the write is an ``ON CONFLICT DO UPDATE`` keyed on the
    existing ``(leader_address, tx_hash)`` UNIQUE (``uq_leader_trades_leader_tx``),
    the SAME dedup anchor detection.py documents.  A WS/poll redelivery of
    the same tx therefore never double-inserts; a row already marked
    ``status="failed"`` is the durable "do not retry" marker PolyHermes'
    ``processed_trade`` has and PolyEcho previously lacked.  We do NOT
    change the dedup key.

    Best-effort: any DB error is logged and swallowed — a trade-record
    write must NEVER crash or stall the trading loop.  The durable record
    is for forensics + the cross-restart dup guard; the in-proc LRU + the
    persisted reconcile cursor still bound re-attempts even if this write
    transiently fails.
    """

    async def _write(evaluated: Any, info: dict[str, Any]) -> None:
        try:
            from datetime import UTC, datetime
            from decimal import Decimal, InvalidOperation

            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from polyecho.shared.db import session_scope
            from polyecho.shared.models import LeaderTrade

            leader_address = str(getattr(evaluated, "leader_address", "")).lower()
            tx_hash = str(info.get("tx_hash") or getattr(evaluated, "tx_hash", "") or "")
            if not leader_address or not tx_hash:
                logger.warning(
                    "leader_trade_record_skipped_missing_key",
                    extra={"leader": leader_address, "tx_hash": tx_hash},
                )
                return

            status = info.get("status")
            if status not in ("success", "failed"):
                # Defence-in-depth: never write a value the CHECK rejects.
                status = "failed"

            def _num(key: str) -> Decimal | None:
                raw = info.get(key)
                if raw is None:
                    return None
                try:
                    return Decimal(str(raw))
                except (InvalidOperation, ValueError, TypeError):
                    return None

            now = datetime.now(tz=UTC)
            values: dict[str, Any] = {
                # trade_id has its own UNIQUE; the detected-trade tx_hash
                # IS the fixture/live trade id (detection.py sets both
                # equal), so reuse it — keeps the row self-consistent.
                "trade_id": tx_hash,
                "leader_address": leader_address,
                "condition_id": str(getattr(evaluated, "condition_id", "") or "") or None,
                "market_id": str(getattr(evaluated, "condition_id", "") or "") or None,
                "tx_hash": tx_hash,
                "side": info.get("side"),
                "size": _num("size"),
                "price": _num("price"),
                "timestamp": now,
                "status": status,
                "processed_at": now,
                "raw": dict(getattr(evaluated, "raw", {}) or {}),
            }
            # Idempotent on the EXISTING (leader_address, tx_hash) UNIQUE —
            # a redelivery updates the outcome in place, never duplicates.
            stmt = (
                pg_insert(LeaderTrade)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_leader_trades_leader_tx",
                    set_={
                        "status": status,
                        "processed_at": now,
                        "side": values["side"],
                        "size": values["size"],
                        "price": values["price"],
                    },
                )
            )
            async with session_scope() as session:
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001 - best-effort durable record
            logger.warning(
                "leader_trade_record_write_failed_continuing",
                extra={"error": str(exc)},
            )

    return _write


# ---------------------------------------------------------------------------
# 2. CursorWriter — persist ``follow_relations.last_seen_ts``
# ---------------------------------------------------------------------------
def make_cursor_writer() -> Any:
    """Build a real ``CursorWriter`` (best-effort cursor persistence).

    The returned ``async (leader_address, last_seen_ts) -> None`` persists
    the per-leader reconcile cursor to **every** active
    ``follow_relations`` row for that leader (the column the model
    exposes — :attr:`FollowRelation.last_seen_ts`).  This is what makes
    reconcile genuinely restart-safe: on restart the engine seeds its
    in-proc cursor store from these persisted snapshots, so a WS gap /
    crash backfills missed trades instead of losing them.

    Best-effort: the engine's ``_reconcile_once`` already moved its
    in-proc store BEFORE calling us, so a persistence failure here only
    means the *next* tick re-attempts the durable write — never a missed
    or double-processed trade, never a crash.
    """

    async def _write(leader_address: str, last_seen_ts: datetime) -> None:
        try:
            from sqlalchemy import update

            from polyecho.shared.db import session_scope
            from polyecho.shared.models import FollowRelation

            stmt = (
                update(FollowRelation)
                .where(FollowRelation.leader_address == leader_address)
                .values(last_seen_ts=last_seen_ts)
            )
            async with session_scope() as session:
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001 - best-effort persistence
            logger.warning(
                "reconcile_cursor_followrelation_persist_failed_continuing",
                extra={"leader": leader_address, "error": str(exc)},
            )

    return _write


# ---------------------------------------------------------------------------
# 3. RedeemWorker — real DB-backed poller + upserter + EOA web3 backend,
#    behind the UNCHANGED double live-lock.
# ---------------------------------------------------------------------------
class _AsyncWeb3Backend:
    """Thin structural :class:`~polyecho.shared.polymarket.redeem.Web3Backend`.

    Lazily constructs web3.py v7 ``AsyncWeb3`` (no node / no import side
    effect until first use — mirrors :class:`Web3OnChainReader`).  Only
    ever reached on the EOA LIVE path (the double lock + worker
    short-circuit keep it uncalled by default).

    context7-verified web3.py v7 (``/ethereum/web3.py`` v7.12.0): build a
    contract-function tx with ``build_transaction``, sign with
    ``eth_account`` (``w3.eth.account.sign_transaction`` →
    ``raw_transaction``), broadcast via ``send_raw_transaction``, await
    ``wait_for_transaction_receipt``; read-only via ``.call()``.
    """

    def __init__(self, *, rpc_url: str | None = None) -> None:
        self._rpc_url = rpc_url or get_settings().polygon_rpc_url
        self._w3: Any = None

    def _client(self) -> Any:
        if self._w3 is None:
            from web3 import AsyncWeb3

            self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(self._rpc_url))
        return self._w3

    async def send_signed(
        self,
        *,
        contract_address: str,
        abi: tuple[dict[str, Any], ...],
        fn_name: str,
        args: tuple[Any, ...],
        signer_private_key: str,
        from_address: str,
        chain_id: int,
    ) -> str:
        from web3 import Web3

        w3 = self._client()
        sender = Web3.to_checksum_address(from_address)
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=list(abi),
        )
        fn = contract.functions[fn_name](*args)
        nonce = await w3.eth.get_transaction_count(sender)
        tx = await fn.build_transaction(
            {
                "from": sender,
                "nonce": nonce,
                "chainId": chain_id,
            }
        )
        signed = w3.eth.account.sign_transaction(tx, signer_private_key)
        tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    async def wait_receipt(self, tx_hash: str) -> dict[str, Any]:
        w3 = self._client()
        receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
        return dict(receipt)

    async def call(
        self,
        *,
        contract_address: str,
        abi: tuple[dict[str, Any], ...],
        fn_name: str,
        args: tuple[Any, ...],
    ) -> Any:
        from web3 import Web3

        w3 = self._client()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=list(abi),
        )
        return await contract.functions[fn_name](*args).call()


def _build_safe_proxy_resolver() -> Any:
    """Construct the injected :data:`SafeProxyResolver` for SAFE (sig 2).

    Lazily builds an ``AsyncWeb3`` (no node touched until the first SAFE
    account is resolved) wired to the verified Polymarket Safe Proxy
    Factory (``constants.SAFE_PROXY_FACTORY`` — brief §9).  The factory's
    ``computeProxyAddress(eoa)`` is a *view* function (no gas), so this
    only ever issues a read-only ``eth_call``.  Returns ``None`` if web3
    is unavailable or settings cannot be loaded, so the SAFE path falls
    back to its existing fail-closed behaviour (operator must supply
    ``funder_address``) — never silently fabricates an address.
    """
    try:
        from web3 import AsyncWeb3

        from polyecho.shared.polymarket.wallet import make_safe_proxy_resolver

        rpc_url = get_settings().polygon_rpc_url
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))
        return make_safe_proxy_resolver(w3)
    except Exception as exc:  # noqa: BLE001 - fail-soft: stays fail-closed
        logger.warning(
            "safe_proxy_resolver_unavailable_falling_back_to_fail_closed",
            extra={"error": str(exc)},
        )
        return None


async def _resolve_account(
    account_id: uuid.UUID,
) -> tuple[str, WalletBinding] | None:
    """Best-effort ``(funder, WalletBinding)`` for one account.

    Reads the ``accounts`` row and runs the FROZEN
    :func:`polyecho.shared.polymarket.wallet.resolve_wallet` (single
    source of truth for the 4 wallet modes; fail-closed on unknown).
    For SAFE (sig 2) accounts that don't have an explicit
    ``funder_address``, injects a :func:`_build_safe_proxy_resolver`
    that issues a *view* ``computeProxyAddress(eoa)`` RPC call on the
    verified Polymarket Safe Proxy Factory (brief §9) — a gas-free
    eth_call.  An explicit operator-supplied funder is still preferred
    (resolve_wallet short-circuits before the RPC).  Returns ``None``
    (skip this account) on any error — never raises into the redeem
    loop.  Only called on the live path.
    """
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Account
        from polyecho.shared.polymarket.wallet import resolve_wallet

        async with session_scope() as session:
            row = (
                await session.execute(select(Account).where(Account.id == account_id))
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "redeem_account_not_found_skipping",
                    extra={"account_id": str(account_id)},
                )
                return None
            binding = await resolve_wallet(row, safe_proxy_resolver=_build_safe_proxy_resolver())
            return binding.funder, binding
    except Exception as exc:  # noqa: BLE001 - fail-soft per account
        logger.warning(
            "redeem_account_resolve_failed_skipping",
            extra={"account_id": str(account_id), "error": str(exc)},
        )
        return None


async def _redeem_accounts() -> list[tuple[uuid.UUID, str]]:
    """``[(account_id, funder)]`` for every ``ready`` account.

    Best-effort: returns ``[]`` on any DB error so a degraded DB simply
    means "no redeem candidates this pass", never a crash.
    """
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Account

        out: list[tuple[uuid.UUID, str]] = []
        async with session_scope() as session:
            rows = (
                await session.execute(select(Account).where(Account.status == "ready"))
            ).scalars()
            for row in rows:
                resolved = await _resolve_account(row.id)
                if resolved is not None:
                    out.append((row.id, resolved[0]))
        return out
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning(
            "redeem_accounts_query_failed_continuing",
            extra={"error": str(exc)},
        )
        return []


def _make_key_resolver() -> Any:
    """Build the EOA ``PrivateKeyResolver`` (live path only).

    The decrypted key never persists in this module: ``Web3RedeemClient``
    only calls this on the EOA live path, which the double lock keeps
    uncalled by default.  Sources the passphrase from
    ``POLYECHO_KEY_PASSPHRASE``; **fails closed (raises)** if unset rather
    than guessing — never weakens the lock.
    """

    async def _resolve(binding: WalletBinding) -> str:
        passphrase = os.environ.get(_KEY_PASSPHRASE_ENV)
        if not passphrase:
            raise RuntimeError(
                "live redeem reached but "
                f"{_KEY_PASSPHRASE_ENV} is not set — refusing to decrypt "
                "the key (fail-closed)"
            )
        from sqlalchemy import select

        from polyecho.shared.crypto import decrypt_private_key
        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Account

        async with session_scope() as session:
            row = (
                await session.execute(select(Account).where(Account.eoa_address == binding.funder))
            ).scalar_one_or_none()
            if row is None or row.encrypted_private_key is None:
                raise RuntimeError(f"no encrypted key for funder {binding.funder} (fail-closed)")
            return decrypt_private_key(bytes(row.encrypted_private_key), passphrase=passphrase)

    return _resolve


def build_redeem_worker(
    follows: Sequence[Any],
) -> RedeemWorker:
    """Build the REAL :class:`RedeemWorker` gated on ``auto_redeem``.

    Wires the concrete DB-backed collaborators:

    * :class:`RedeemablePoller` over the live datasource ``get_positions``
      (data-api ``redeemable`` flag — brief §9 API-trust), polling every
      ``ready`` account's funder.
    * :class:`DbJobUpserter` — idempotent ``redeem_jobs`` upsert + status
      machine on ``(account, condition, outcome)``.
    * EOA :class:`Web3RedeemClient` over :class:`_AsyncWeb3Backend`.

    The worker gates the on-chain dispatch on ``auto_redeem``
    (``poll_once`` logs a dry-run and submits NOTHING unless
    ``self._auto_redeem`` is True).  With ``auto_redeem`` off the
    poller/upserter still run but every outcome is PENDING with zero
    on-chain calls.
    """
    from polyecho.core.redeem import RedeemCandidate
    from polyecho.shared.datasource import get_datasource
    from polyecho.shared.polymarket.redeem import Web3RedeemClient

    settings = get_settings()

    async def _candidate_source() -> list[RedeemCandidate]:
        # Resolve accounts fresh each pass (a newly-imported account is
        # picked up without a restart); best-effort.
        accounts = await _redeem_accounts()
        if not accounts:
            return []
        poller = RedeemablePoller(
            datasource=get_datasource(),
            accounts=accounts,
        )
        return await poller()

    upserter = DbJobUpserter(session_factory=_session_factory)

    client = Web3RedeemClient(
        web3=_AsyncWeb3Backend(),
        private_key_resolver=_make_key_resolver(),
        chain_id=settings.polymarket_chain_id,
    )

    async def _binding_for(account_id: uuid.UUID) -> WalletBinding:
        resolved = await _resolve_account(account_id)
        if resolved is None:
            raise RuntimeError(f"could not resolve wallet binding for {account_id}")
        return resolved[1]

    return RedeemWorker(
        candidate_source=_candidate_source,
        job_upserter=upserter.upsert,
        binding_for=_binding_for,
        redeem_client=client,
        auto_redeem=bool(settings.auto_redeem),
        status_writer=upserter.record,
        event_emitter=_redeem_event_emitter,
    )


def _session_factory() -> Any:
    """``() -> AsyncSession`` context manager for :class:`DbJobUpserter`."""
    from polyecho.shared.db import AsyncSessionLocal

    return AsyncSessionLocal()


async def _redeem_event_emitter(ok: bool, spec: Any, result: Any) -> None:
    """Best-effort redeem success/failure observability (never raises).

    The frozen ``EventType`` taxonomy (``packages/shared`` — FROZEN) has
    no redeem-specific member and we must not invent one, so this is a
    structured log only (the worker's ``status_writer`` already persists
    the durable ``redeem_jobs`` state-machine transition; this is purely
    operator-visible breadcrumbs).  Swallows everything — a logging hiccup
    must never stall the redeem loop.
    """
    try:
        logger.info(
            "redeem_outcome",
            extra={
                "ok": ok,
                "account_id": str(spec.account_id),
                "condition_id": spec.condition_id,
                "outcome_index": spec.outcome_index,
                "status": result.status.value,
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning(
            "redeem_event_emit_failed_continuing",
            extra={"error": str(exc)},
        )


# ---------------------------------------------------------------------------
# 4. Supervised orchestration — startup reconcile + engine + redeem +
#    daily reconcile cadence.  All best-effort / fail-safe.
# ---------------------------------------------------------------------------
async def startup_reconcile() -> None:
    """Run ONE on-chain ↔ DB reconcile at engine startup (best-effort).

    Read-only & lock-independent (only ``balanceOf`` ``eth_call`` reads +
    ``SELECT``s; per-account RPC errors are already isolated inside
    ``run_reconcile``).  A reconcile error must NOT abort startup — it is
    logged and swallowed here as a final guard.
    """
    try:
        from polyecho.core.onchain_reconcile import run_reconcile

        report = await run_reconcile()
        logger.info(
            "startup_onchain_reconcile_done",
            extra={
                "has_drift": report.has_drift,
                "drift_count": report.drift_count,
            },
        )
    except Exception as exc:  # noqa: BLE001 - never abort startup
        logger.warning(
            "startup_onchain_reconcile_failed_continuing",
            extra={"error": str(exc)},
        )


async def _redeem_loop(worker: RedeemWorker, *, interval_seconds: float) -> None:
    """Poll the real RedeemWorker on a cadence (best-effort, fail-safe).

    The worker enforces the double live-lock internally; with locks off
    every pass is a logged dry-run with zero on-chain / zero real redeem.
    A pass that raises is logged and the loop continues — the redeem
    cadence must never crash the supervised process.
    """
    # R9 I-4: this loop must TRULY never exit.  If it ever returned or
    # raised, the supervised group would tear down the sibling reconcile
    # loop and the redeem cadence would silently stop.  ``while True`` +
    # ``except BaseException`` (re-raising ONLY KeyboardInterrupt /
    # SystemExit / CancelledError so a real shutdown still works) keeps the
    # cadence alive across ANY iteration fault.
    while True:
        try:
            outcomes = await worker.poll_once()
            if outcomes:
                logger.info(
                    "redeem_pass_complete",
                    extra={"outcomes": len(outcomes)},
                )
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "redeem_pass_failed_continuing",
                extra={"error": str(exc)},
            )
        await asyncio.sleep(interval_seconds)


async def _daily_reconcile_loop(*, interval_seconds: float) -> None:
    """Daily on-chain reconcile cadence (delegates to ``run_forever``).

    R9 I-4: ``run_forever`` already logs+continues on a failing
    *iteration*, but if it ever RETURNS (e.g. a future test bound) or
    raises at setup, the old wrapper just fell through and exited — which
    under the supervised group would silently kill the sibling redeem
    cadence too.  This wrapper is now itself a true never-exit loop:
    ``run_forever`` returning/raising only makes us re-enter it after the
    interval; only KeyboardInterrupt / SystemExit / CancelledError (a real
    shutdown) propagate.
    """
    from polyecho.core.onchain_reconcile import run_forever

    while True:
        try:
            await run_forever(interval_seconds=interval_seconds)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except BaseException as exc:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "daily_onchain_reconcile_loop_failed_continuing",
                extra={"error": str(exc)},
            )
        # run_forever returned/raised — wait then re-enter so the daily
        # cadence is genuinely perpetual (never silently stops).
        await asyncio.sleep(interval_seconds)


async def run_supervised(
    core: Any,
    *,
    redeem_interval_seconds: float = 300.0,
    reconcile_interval_seconds: float = 86_400.0,
) -> None:
    """Supervised entrypoint: startup reconcile + engine + redeem + daily.

    Runs (all best-effort, none may bypass the lock or crash the loop):

    1. ONE startup on-chain reconcile (read-only, lock-independent).
    2. The engine's own supervised pass (``core.run_once`` — heartbeat +
       cursor writers already injected at construction, so every cycle
       writes the ``trader_heartbeat`` row + persists the reconcile
       cursor).  The engine's ``--once`` contract is untouched: this
       path is ONLY the supervised loop; ``--once`` calls
       ``core.run_once()`` directly in ``__main__``.
    3. A long-lived ``asyncio.gather`` parking the process while the
       real RedeemWorker (behind the unchanged double live-lock), the
       daily on-chain reconcile cadence, AND the PRD §7.1 integration
       connectivity probe (``run_all_probes`` → ``write_integration_status``
       plus the non-frozen WS reachability probes) run as independent
       fail-soft loops.  The probe is attached HERE because this is the
       deployed supervised entrypoint (``__main__`` calls
       ``run_supervised``, not ``TraderCore.run``); omitting it left the
       ``integration_status`` row — and the /status connectivity panel —
       permanently empty.

    Mirrors :meth:`TraderCore.run` (run one bounded pass then park) but
    additionally drives the real DB-backed RedeemWorker + reconcile
    cadence WITHOUT modifying the engine.
    """
    await startup_reconcile()
    # The engine writes the startup + cycle heartbeat and persists the
    # reconcile cursor itself (writers injected at construction).
    await core.run_once()

    worker = build_redeem_worker(getattr(core, "_follows", []))
    # R9 I-4: ``asyncio.gather(return_exceptions=True)`` instead of a
    # TaskGroup so the two cadence loops survive INDEPENDENTLY.  A
    # TaskGroup cancels every sibling the instant one task exits/raises —
    # so a fault in the daily reconcile loop would silently stop the
    # redeem cadence (and vice-versa).  With ``gather`` each loop is a
    # genuine never-exit ``while True`` (above) and a stray return/raise
    # in one is captured as a result without disturbing the other.
    # §7.1 integration connectivity probe — attached as a third
    # independent never-exit fail-soft loop.  ``TraderCore.run`` attaches
    # it via its own TaskGroup, but ``__main__`` deploys THIS entrypoint,
    # so without this line the deployed trader-core never writes the
    # ``integration_status`` row (the dashboard /status connectivity panel
    # stays empty).  Same ``gather(return_exceptions=True)`` rationale:
    # one loop faulting must never stop the siblings.
    # ``core._park()`` is the W2 idle keepalive: it re-stamps a
    # ``phase="idle"`` trader_heartbeat every ~90s.  ``TraderCore.run``
    # attaches it via its own TaskGroup, but ``__main__`` deploys THIS
    # entrypoint, which calls ``run_once`` ONCE then parks here — so
    # without this line trader_heartbeat freezes at the single startup
    # pass and the dashboard false-flags Trader as 逾時/stale ~5 min
    # later (exact same deployed-entrypoint gap as the §7.1 probe above).
    await asyncio.gather(  # pragma: no cover - supervised loop
        _redeem_loop(worker, interval_seconds=redeem_interval_seconds),
        _daily_reconcile_loop(interval_seconds=reconcile_interval_seconds),
        core._integration_probe_loop(),
        core._park(),
        return_exceptions=True,
    )


# Imported lazily by callers; re-exported for tests.
__all__ = [
    "GLOBAL_PAUSED_KEY",
    "TRADER_HEARTBEAT_KEY",
    "build_redeem_worker",
    "make_cursor_writer",
    "make_global_pause_writer",
    "make_heartbeat_writer",
    "make_trade_recorder",
    "run_supervised",
    "startup_reconcile",
]

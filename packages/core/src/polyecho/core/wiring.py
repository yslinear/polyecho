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
  EOA :class:`Web3RedeemClient` backend, **behind the unchanged double
  live-lock** (it short-circuits unless BOTH ``POLYECHO_LIVE_TRADING`` and
  ``POLYECHO_AUTO_REDEEM``; with locks off it does zero on-chain / zero DB
  job side-effects beyond a logged dry-run, exactly as its own tests
  assert).  MAGIC/SAFE/POLY_1271 stay the abstract
  ``RelayerNotConfiguredError`` path (untouched).
* :func:`run_supervised` — drives startup ``onchain_reconcile.run_reconcile``
  (read-only, lock-independent), the engine's own supervised pass, the
  real RedeemWorker, then the daily reconcile cadence — all best-effort.

**Fail-safe contract (money-path safety is paramount):** every factory
and the loop are best-effort — a wiring or collaborator error is logged
and swallowed; it must NEVER crash the supervised loop and must NEVER
bypass the lock.  The default (locks off, nothing provisioned) stays a
clean dry-run that places / moves / persists nothing on-chain.  The
``--once`` exit-code contract is untouched (this module is only reached
on the supervised path; ``--once`` still calls ``core.run_once()``).
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


async def _resolve_account(
    account_id: uuid.UUID,
) -> tuple[str, WalletBinding] | None:
    """Best-effort ``(funder, WalletBinding)`` for one account.

    Reads the ``accounts`` row and runs the FROZEN
    :func:`polyecho.shared.polymarket.wallet.resolve_wallet` (single
    source of truth for the 4 wallet modes; fail-closed on unknown).
    Returns ``None`` (skip this account) on any error — never raises into
    the redeem loop.  Only called on the live path.
    """
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Account
        from polyecho.shared.polymarket.wallet import resolve_wallet

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Account).where(Account.id == account_id)
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "redeem_account_not_found_skipping",
                    extra={"account_id": str(account_id)},
                )
                return None
            binding = await resolve_wallet(row)
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
                await session.execute(
                    select(Account).where(Account.status == "ready")
                )
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
                await session.execute(
                    select(Account).where(
                        Account.eoa_address == binding.funder
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.encrypted_private_key is None:
                raise RuntimeError(
                    "no encrypted key for funder "
                    f"{binding.funder} (fail-closed)"
                )
            return decrypt_private_key(
                bytes(row.encrypted_private_key), passphrase=passphrase
            )

    return _resolve


def build_redeem_worker(
    follows: Sequence[Any],
) -> RedeemWorker:
    """Build the REAL :class:`RedeemWorker` behind the unchanged double lock.

    Wires the concrete DB-backed collaborators:

    * :class:`RedeemablePoller` over the live datasource ``get_positions``
      (data-api ``redeemable`` flag — brief §9 API-trust), polling every
      ``ready`` account's funder.
    * :class:`DbJobUpserter` — idempotent ``redeem_jobs`` upsert + status
      machine on ``(account, condition, outcome)``.
    * EOA :class:`Web3RedeemClient` over :class:`_AsyncWeb3Backend`.

    The worker enforces the DOUBLE live-lock itself (``poll_once`` logs a
    ``LIVE_LOCKED`` dry-run and submits NOTHING unless BOTH
    ``is_live_enabled()`` AND ``auto_redeem``); even past that
    ``BaseRedeemClient.redeem`` calls ``assert_live_allowed`` FIRST.  We
    do not change any of that — we only construct the real collaborators.
    With locks off the poller/upserter still run but every outcome is
    PENDING / LIVE_LOCKED with zero on-chain calls, exactly as
    ``test_default_lock_off_zero_onchain_and_idempotent`` asserts.
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
            raise RuntimeError(
                f"could not resolve wallet binding for {account_id}"
            )
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


async def _redeem_event_emitter(
    ok: bool, spec: Any, result: Any
) -> None:
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


async def _redeem_loop(
    worker: RedeemWorker, *, interval_seconds: float
) -> None:
    """Poll the real RedeemWorker on a cadence (best-effort, fail-safe).

    The worker enforces the double live-lock internally; with locks off
    every pass is a logged dry-run with zero on-chain / zero real redeem.
    A pass that raises is logged and the loop continues — the redeem
    cadence must never crash the supervised process.
    """
    while True:
        try:
            outcomes = await worker.poll_once()
            if outcomes:
                logger.info(
                    "redeem_pass_complete",
                    extra={"outcomes": len(outcomes)},
                )
        except Exception as exc:  # noqa: BLE001 - never crash the loop
            logger.warning(
                "redeem_pass_failed_continuing",
                extra={"error": str(exc)},
            )
        await asyncio.sleep(interval_seconds)


async def _daily_reconcile_loop(*, interval_seconds: float) -> None:
    """Daily on-chain reconcile cadence (delegates to ``run_forever``).

    ``run_forever`` already logs+continues on a failing iteration; this
    wrapper adds a final guard so even an unexpected error in the loop
    setup never crashes the supervised process.
    """
    try:
        from polyecho.core.onchain_reconcile import run_forever

        await run_forever(interval_seconds=interval_seconds)
    except Exception as exc:  # noqa: BLE001 - never crash the loop
        logger.warning(
            "daily_onchain_reconcile_loop_failed_continuing",
            extra={"error": str(exc)},
        )


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
    3. A long-lived TaskGroup parking the process while the real
       RedeemWorker (behind the unchanged double live-lock) and the
       daily on-chain reconcile cadence run.

    Mirrors :meth:`TraderCore.run` (run one bounded pass then park) but
    additionally drives the real DB-backed RedeemWorker + reconcile
    cadence WITHOUT modifying the engine.
    """
    await startup_reconcile()
    # The engine writes the startup + cycle heartbeat and persists the
    # reconcile cursor itself (writers injected at construction).
    await core.run_once()

    worker = build_redeem_worker(getattr(core, "_follows", []))
    async with asyncio.TaskGroup() as tg:  # pragma: no cover - supervised loop
        tg.create_task(
            _redeem_loop(worker, interval_seconds=redeem_interval_seconds)
        )
        tg.create_task(
            _daily_reconcile_loop(
                interval_seconds=reconcile_interval_seconds
            )
        )


# Imported lazily by callers; re-exported for tests.
__all__ = [
    "TRADER_HEARTBEAT_KEY",
    "build_redeem_worker",
    "make_cursor_writer",
    "make_heartbeat_writer",
    "run_supervised",
    "startup_reconcile",
]

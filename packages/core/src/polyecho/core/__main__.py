"""Entry-point: ``python -m polyecho.core`` (PRD §4.2/§5.5).

Modes:

* ``--once``  — a single dry-run fixture pass.  This is the CI-smoke
  path: it builds the engine, runs one bounded
  detection→evaluate→risk→(dry-run)execute→reconcile→redeem pass against
  the bundled fixture datasource, places NOTHING (the hard live lock is
  off by default), then exits.

  **Exit-code contract (P1-12)** — so CI smoke can detect a wholly-broken
  pipeline, ``--once`` no longer always returns 0:

  * ``0`` — a healthy or legitimately idle pass: either no follows are
    configured (nothing to do), OR follows exist and trades were
    detected (even if every one was skipped — e.g. all ``LIVE_LOCKED``
    in the default dry-run, or legitimate risk rejects; skips are the
    expected default-off outcome, never a failure).
  * ``2`` — a *degenerate* pass: follows ARE configured but **zero**
    trades were detected across both detection paths + reconcile (the
    pipeline is broken — datasource/fixture/wiring), OR every detected
    trade errored.  See :meth:`TraderCore.degenerate_once_pass`.
* default     — the supervised loop (asyncio TaskGroup: detection /
  reconcile / redeem).  In fixture mode it runs one pass then idles
  (restart-safe; reconcile is the durable backfill).

Structured JSON logging is configured first; private keys / secrets are
never logged (``polyecho.shared.logging`` redacts them defence-in-depth).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from decimal import Decimal

from polyecho.core.evaluator import TemplateConfig
from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.shared.config import get_settings
from polyecho.shared.copytrading import WalletBinding
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)


def _demo_follows() -> list[FollowSpec]:
    """Fixture follow relations so ``--once`` exercises the full pipeline.

    Uses a leader present in the bundled activity fixtures.  EOA binding
    (signature_type 0) so no derivation/RPC is needed; the live lock keeps
    this a pure dry-run regardless.
    """
    leader = "0xswing1000000000000000000000000000000001"
    acct = uuid.uuid5(uuid.NAMESPACE_DNS, "polyecho.core.demo")
    return [
        FollowSpec(
            account_id=acct,
            leader_address=leader,
            template=TemplateConfig(
                copy_ratio=Decimal("0.01"),
                max_position_usd=Decimal("50"),
                min_trade_size_usd=Decimal("1"),
                max_concurrent_positions=10,
            ),
            binding=WalletBinding(
                signature_type=0,
                funder=leader,
                wallet_mode="eoa",
                derived=False,
            ),
        )
    ]


async def main(argv: list[str] | None = None) -> int:
    """Configure logging, build the engine, run the requested mode."""
    args = argv if argv is not None else sys.argv[1:]
    settings = get_settings()
    configure_logging(settings.log_level)
    once = "--once" in args

    logger.info(
        "starting trader-core",
        extra={
            "mode": "once" if once else "supervised",
            "datasource": settings.datasource,
            "live_trading": settings.live_trading,
        },
    )
    core = TraderCore(follows=_demo_follows())

    if once:
        stats = await core.run_once()
        # P1-12: a degenerate pass (follows configured but 0 detected, or
        # all-error) returns non-zero so CI smoke fails on a broken
        # pipeline; a healthy / legitimately-idle pass returns 0.
        degenerate = core.degenerate_once_pass()
        logger.info(
            "trader-core --once complete",
            extra={
                "detected": stats.detected,
                "copied": stats.copied,
                "skipped": stats.skipped,
                "placed": stats.placed,
                "errors": stats.errors,
                "degenerate": degenerate,
            },
        )
        if degenerate is not None:
            logger.error(
                "trader-core --once DEGENERATE pass",
                extra={"reason": degenerate},
            )
            return 2
        return 0

    await core.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

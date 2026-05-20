"""Entry-point: ``python -m polyecho.core`` (PRD §4.2/§5.5).

Modes:

* ``--once``  — a single fixture pass.  This is the CI-smoke
  path: it builds the engine, runs one bounded
  detection→evaluate→risk→execute→reconcile→redeem pass against
  the bundled fixture datasource, then exits.

  **Exit-code contract (P1-12)** — so CI smoke can detect a wholly-broken
  pipeline, ``--once`` no longer always returns 0:

  * ``0`` — a healthy or legitimately idle pass: either no follows are
    configured (nothing to do), OR follows exist and trades were
    detected (even if every one was skipped — e.g. legitimate risk /
    filter rejects; skips are expected, never a failure).
  * ``2`` — a *degenerate* pass: follows ARE configured but **zero**
    trades were detected across both detection paths + reconcile (the
    pipeline is broken — datasource/fixture/wiring), OR every detected
    trade errored.  See :meth:`TraderCore.degenerate_once_pass`.
* default     — the supervised loop (``polyecho.core.wiring.run_supervised``):
  a startup on-chain reconcile (read-only), the engine's own bounded
  pass (heartbeat + reconcile-cursor writers injected ⇒ every cycle
  persists liveness + the restart-safe cursor), then a parked TaskGroup
  running the REAL DB-backed RedeemWorker — gated on
  ``POLYECHO_AUTO_REDEEM`` (default OFF ⇒ logged dry-run, zero
  on-chain) — plus the daily on-chain reconcile cadence.

Structured JSON logging is configured first; private keys / secrets are
never logged (``polyecho.shared.logging`` redacts them defence-in-depth).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from polyecho.core.trader import FollowSpec, TraderCore
from polyecho.core.wiring import (
    make_cursor_writer,
    make_heartbeat_writer,
    run_supervised,
)
from polyecho.shared.config import get_settings
from polyecho.shared.logging import configure_logging

logger = logging.getLogger(__name__)


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
        },
    )
    # C-wire: inject the REAL DB-backed liveness + reconcile-cursor
    # writers (were injectable-and-not-injected ⇒ dormant in production).
    # Both are best-effort: in fixture mode / no DB they log+continue, so
    # the ``--once`` exit-code contract and dry-run semantics are
    # unchanged (they place / move / persist nothing on-chain).
    #
    # 防再種 — trader-core NEVER fabricates follow relations.  There is no
    # demo/fixture follow generator: a deployment follows ONLY the
    # operator-configured leaders (none here ⇒ empty).  This guarantees
    # no synthetic ``copy_fill`` / fill / risk events can ever be
    # generated into the operator DB from a bundled fixture leader.
    # ``degenerate_once_pass`` treats "no follows configured" as a valid
    # idle pass (exit 0), so the ``--once`` / CI-smoke contract is
    # preserved; the fixture DATASOURCE read path itself is untouched.
    follows: list[FollowSpec] = []
    core = TraderCore(
        follows=follows,
        heartbeat_writer=make_heartbeat_writer(),
        cursor_writer=make_cursor_writer(),
    )

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

    # C-wire: the supervised path now ALSO drives a startup on-chain
    # reconcile (read-only) and the REAL DB-backed RedeemWorker (gated
    # on ``POLYECHO_AUTO_REDEEM`` — flag off ⇒ logged dry-run, zero
    # on-chain) + the daily reconcile cadence.  The engine itself is
    # untouched; ``run_supervised`` runs ``core.run_once`` then parks
    # alongside the redeem + reconcile tasks.
    await run_supervised(core)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

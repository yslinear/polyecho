"""
polyecho.shared.datasource.market_cache
========================================
``MarketMetadataCache`` — a persisted market-metadata cache (C4, PolyHermes
deep-dive).

WHY THIS EXISTS
---------------
``LiveDataSource`` (``datasource/live.py``) hits the Polymarket data-api /
Gamma live every cycle for market metadata (question, slug, ``neg_risk``,
``tick_size``, ``end_date``, outcomes) with NO persistence.  PolyHermes
keeps a ``markets`` DB table behind a two-tier Caffeine cache with a
cache -> DB -> remote read path and an immutable settled-price tier.

This module ports that pattern WITHOUT touching the FROZEN ``live.py``:
it is a NEW module that *composes* with a datasource.  A read for a
market's metadata is layered:

1. **in-process cache** — an LRU-bounded dict keyed by ``condition_id``.
2. **``markets`` DB table** — checked on a cache miss; a hit warms the
   in-process cache.
3. **remote API** — the wrapped datasource's ``get_markets_by_volume``,
   used only on a DB miss; a remote hit is written back to BOTH the DB
   table and the in-process cache.

Immutability tier
-----------------
A market that is RESOLVED/settled is immutable: its metadata never
changes again, so a cached row (in-process or DB) is served FOREVER with
no remote re-fetch.  An unresolved market uses a short freshness window
(``_FRESH_WINDOW``); a row older than that window is treated as a miss
and refreshed opportunistically.

Fail-soft contract
------------------
A DB error never aborts a metadata read — it degrades to a remote fetch
(with one ``logger.warning``).  A write-back failure is likewise logged
and swallowed: the in-process cache still holds the value, so the read
itself always succeeds when the remote does.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from polyecho.shared.db import session_scope
from polyecho.shared.models.market import Market

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: Max markets held in the in-process LRU (PolyHermes uses LRU(200)).
_LRU_MAXSIZE = 200
#: Freshness window for an UNRESOLVED market. A DB/cache row older than this
#: is treated as a miss and re-fetched. A RESOLVED market ignores this
#: entirely (immutable — cached forever).
_FRESH_WINDOW = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# MarketMetadata — the value object returned by the cache
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketMetadata:
    """One market's cached metadata (the cache read/write unit).

    Attributes:
        condition_id:  THE market identifier (``0x...`` 64-hex).
        market_id:     Polymarket internal market id, or ``None``.
        question:      Human-readable market question/title, or ``None``.
        slug:          URL slug, or ``None``.
        neg_risk:      NegRisk flag (selects the NegRisk Exchange contract).
        tick_size:     CLOB price tick size as a decimal string (e.g.
                       ``"0.01"``), or ``None``.
        end_date:      Resolution date/time (UTC-aware), or ``None``.
        outcomes:      Outcome token dicts.
        is_resolved:   ``True`` once the market has settled — the row is
                       then immutable (cached forever, no remote re-fetch).
        settled_price: Settled winning-outcome price, or ``None``.
        raw:           Full upstream payload (forward-compat / debugging).
    """

    condition_id: str
    market_id: str | None = None
    question: str | None = None
    slug: str | None = None
    neg_risk: bool | None = None
    tick_size: str | None = None
    end_date: datetime | None = None
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    is_resolved: bool = False
    settled_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def _from_orm(row: Market) -> MarketMetadata:
    """Build a :class:`MarketMetadata` from a ``markets`` ORM row."""
    return MarketMetadata(
        condition_id=row.condition_id,
        market_id=row.market_id,
        question=row.question,
        slug=row.slug,
        neg_risk=row.neg_risk,
        tick_size=row.tick_size,
        end_date=row.end_date,
        outcomes=list(row.outcomes or []),
        is_resolved=bool(row.is_resolved),
        settled_price=(float(row.settled_price) if row.settled_price is not None else None),
        raw=dict(row.raw or {}),
    )


def _from_raw(condition_id: str, raw: dict[str, Any]) -> MarketMetadata:
    """Build a :class:`MarketMetadata` from a raw Polymarket market dict.

    Defensive: every field is optional and unknown shapes degrade to
    ``None`` / defaults rather than raising — mirroring ``live.py``'s
    JSON -> DTO discipline.
    """

    def _truthy(*keys: str) -> Any:
        for k in keys:
            v = raw.get(k)
            if v is not None:
                return v
        return None

    end_raw = _truthy("end_date", "endDate", "end_date_iso")
    end_date: datetime | None = None
    if isinstance(end_raw, (int, float)):
        end_date = datetime.fromtimestamp(float(end_raw), tz=UTC)
    elif isinstance(end_raw, str):
        try:
            dt = datetime.fromisoformat(end_raw)
            end_date = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            end_date = None

    neg_raw = _truthy("neg_risk", "negRisk", "enableNegRisk")
    tick_raw = _truthy("tick_size", "tickSize", "minimum_tick_size")

    # A market is resolved when the API marks it closed/resolved.
    resolved = bool(
        raw.get("resolved")
        or raw.get("closed")
        or raw.get("is_resolved")
        or raw.get("umaResolutionStatus") == "resolved"
    )
    settled_raw = _truthy("settled_price", "settledPrice")
    settled_price: float | None = None
    if settled_raw is not None:
        try:
            settled_price = float(settled_raw)
        except (TypeError, ValueError):
            settled_price = None

    outcomes = raw.get("tokens") or raw.get("outcomes") or []
    return MarketMetadata(
        condition_id=condition_id,
        market_id=(str(_truthy("market_id", "id") or "") or None),
        question=(str(_truthy("question", "title") or "") or None),
        slug=(str(raw.get("slug") or "") or None),
        neg_risk=(bool(neg_raw) if neg_raw is not None else None),
        tick_size=(str(tick_raw) if tick_raw is not None else None),
        end_date=end_date,
        outcomes=list(outcomes) if isinstance(outcomes, list) else [],
        is_resolved=resolved,
        settled_price=settled_price,
        raw=dict(raw),
    )


# ---------------------------------------------------------------------------
# MarketMetadataCache
# ---------------------------------------------------------------------------
class MarketMetadataCache:
    """cache -> DB -> remote market-metadata layering (C4).

    Composes with (does not subclass) a datasource: pass any object that
    exposes the frozen ``get_markets_by_volume`` signature.  The wrapped
    datasource is FROZEN and untouched — this class is the new seam.

    Args:
        datasource: A datasource exposing ``get_markets_by_volume`` (used
            only as the remote tier).  ``None`` is allowed for a
            DB+cache-only deployment (a remote miss then returns ``None``).
        fresh_window: Freshness window for an UNRESOLVED market; a row
            older than this is a miss.  A resolved market ignores it.
        use_db: When ``False`` the DB tier is skipped (cache + remote
            only) — useful in tests / DB-less contexts.
    """

    def __init__(
        self,
        datasource: Any = None,
        *,
        fresh_window: timedelta = _FRESH_WINDOW,
        use_db: bool = True,
    ) -> None:
        self._ds = datasource
        self._fresh_window = fresh_window
        self._use_db = use_db
        # condition_id -> MarketMetadata, LRU-ordered (most-recent last).
        self._mem: OrderedDict[str, MarketMetadata] = OrderedDict()
        # condition_id -> the wall-clock time the in-process entry was last
        # confirmed fresh (separate from the DB ``updated_at`` so a row
        # warmed from the DB keeps an effective freshness clock for the
        # UNRESOLVED window; a resolved row ignores this entirely).
        self._mem_fresh: dict[str, datetime] = {}
        # Per-instance counters (observability / test assertions).
        self.remote_calls = 0
        self.db_hits = 0
        self.mem_hits = 0

    # ------------------------------------------------------------------
    # In-process LRU helpers
    # ------------------------------------------------------------------
    def _mem_get(self, condition_id: str) -> MarketMetadata | None:
        meta = self._mem.get(condition_id)
        if meta is not None:
            self._mem.move_to_end(condition_id)
        return meta

    def _mem_put(self, meta: MarketMetadata) -> None:
        self._mem[meta.condition_id] = meta
        self._mem.move_to_end(meta.condition_id)
        while len(self._mem) > _LRU_MAXSIZE:
            self._mem.popitem(last=False)

    @staticmethod
    def _is_fresh(updated_at: datetime | None, window: timedelta) -> bool:
        """True if *updated_at* is within *window* of now (UTC)."""
        if updated_at is None:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return datetime.now(tz=UTC) - updated_at <= window

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_market(self, condition_id: str) -> MarketMetadata | None:
        """Return metadata for *condition_id*, layered cache -> DB -> remote.

        * A RESOLVED market is immutable: an in-process or DB hit is
          returned with NO remote call, regardless of age.
        * An UNRESOLVED in-process / DB hit within the freshness window is
          returned without a remote call.
        * A miss (or a stale unresolved row) falls through to the remote
          datasource and, on a remote hit, is written back to BOTH the DB
          table and the in-process cache.

        Returns ``None`` when the market is unknown to every tier.
        """
        if not condition_id:
            return None

        # Tier 1 — in-process LRU.
        mem = self._mem_get(condition_id)
        if mem is not None and (mem.is_resolved or self._is_fresh_mem(condition_id)):
            self.mem_hits += 1
            return mem

        # Tier 2 — markets DB table.
        db_meta, db_updated_at = await self._db_get(condition_id)
        if db_meta is not None and (
            db_meta.is_resolved or self._is_fresh(db_updated_at, self._fresh_window)
        ):
            self.db_hits += 1
            self._mem_put(db_meta)
            self._mem_fresh[condition_id] = datetime.now(tz=UTC)
            return db_meta

        # Tier 3 — remote API; write back on a hit.
        remote = await self._remote_get(condition_id)
        if remote is not None:
            await self._db_put(remote)
            self._mem_put(remote)
            self._mem_fresh[condition_id] = datetime.now(tz=UTC)
            return remote

        # Nothing anywhere — return the stale DB/mem row if we have one
        # (better a stale answer than none for an unresolved market the
        # remote could not refresh).
        return db_meta if db_meta is not None else mem

    def _is_fresh_mem(self, condition_id: str) -> bool:
        """True if the in-process entry for *condition_id* is within window."""
        return self._is_fresh(self._mem_fresh.get(condition_id), self._fresh_window)

    # ------------------------------------------------------------------
    # DB tier
    # ------------------------------------------------------------------
    async def _db_get(self, condition_id: str) -> tuple[MarketMetadata | None, datetime | None]:
        """Read one ``markets`` row; fail-soft to ``(None, None)``."""
        if not self._use_db:
            return None, None
        try:
            async with session_scope() as session:
                row = await session.get(Market, condition_id)
                if row is None:
                    return None, None
                return _from_orm(row), row.updated_at
        except Exception as exc:  # noqa: BLE001 - fail-soft to remote
            logger.warning(
                "market_cache_db_read_failed",
                extra={"condition_id": condition_id, "error": str(exc)},
            )
            return None, None

    async def _db_put(self, meta: MarketMetadata) -> None:
        """Upsert one ``markets`` row; fail-soft (log + swallow).

        A write-back failure does not fail the read: the in-process cache
        still holds the value.  ``updated_at`` is bumped on every write so
        the unresolved freshness window is measured from the last refresh.
        """
        if not self._use_db:
            return
        now = datetime.now(tz=UTC)
        try:
            async with session_scope() as session:
                stmt = pg_insert(Market).values(
                    condition_id=meta.condition_id,
                    market_id=meta.market_id,
                    question=meta.question,
                    slug=meta.slug,
                    neg_risk=meta.neg_risk,
                    tick_size=meta.tick_size,
                    end_date=meta.end_date,
                    outcomes=meta.outcomes,
                    is_resolved=meta.is_resolved,
                    settled_price=meta.settled_price,
                    raw=meta.raw,
                    cached_at=now,
                    updated_at=now,
                )
                # ON CONFLICT: refresh every mutable column + updated_at.
                # cached_at is NOT touched (first-cached timestamp).
                stmt = stmt.on_conflict_do_update(
                    index_elements=[Market.condition_id],
                    set_={
                        "market_id": stmt.excluded.market_id,
                        "question": stmt.excluded.question,
                        "slug": stmt.excluded.slug,
                        "neg_risk": stmt.excluded.neg_risk,
                        "tick_size": stmt.excluded.tick_size,
                        "end_date": stmt.excluded.end_date,
                        "outcomes": stmt.excluded.outcomes,
                        "is_resolved": stmt.excluded.is_resolved,
                        "settled_price": stmt.excluded.settled_price,
                        "raw": stmt.excluded.raw,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001 - fail-soft, read survives
            logger.warning(
                "market_cache_db_write_failed",
                extra={"condition_id": meta.condition_id, "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Remote tier
    # ------------------------------------------------------------------
    async def _remote_get(self, condition_id: str) -> MarketMetadata | None:
        """Fetch one market from the wrapped datasource; fail-soft to ``None``.

        The frozen ``get_markets_by_volume`` returns a volume-ranked list
        of :class:`MarketSummary`; we scan it for the requested
        ``condition_id``.  This is intentionally the only remote path used
        — it reuses the FROZEN datasource verbatim, no new endpoint.
        """
        if self._ds is None:
            return None
        try:
            self.remote_calls += 1
            summaries = await self._ds.get_markets_by_volume(min_volume_24h=0.0, limit=500)
        except Exception as exc:  # noqa: BLE001 - fail-soft to None
            logger.warning(
                "market_cache_remote_fetch_failed",
                extra={"condition_id": condition_id, "error": str(exc)},
            )
            return None
        for summary in summaries:
            if getattr(summary, "condition_id", None) == condition_id:
                raw = dict(getattr(summary, "raw", {}) or {})
                # Carry the typed MarketSummary fields into raw so
                # _from_raw picks them up even when raw is sparse.
                raw.setdefault("question", getattr(summary, "question", None))
                raw.setdefault("market_id", getattr(summary, "market_id", None))
                raw.setdefault("end_date", getattr(summary, "end_date", None))
                raw.setdefault("tokens", getattr(summary, "tokens", []))
                return _from_raw(condition_id, raw)
        return None

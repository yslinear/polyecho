"""
polyecho.shared.health
======================
F0 foundational gate — the **cross-language status / health contract** the
six PolyEcho services (core / discovery / backtest / push / web / cli) build
against (API-status feature).

Why this module exists
----------------------
PolyHermes (the PRD §11.3 Phase-4 reference) surfaces liveness by writing
**heartbeats into the DB** and exposing a **centralised status** the dashboard
reads on a scheduler tick (analysis brief §1 "centralised constants", §5
"append-only event log + cursor", §7.5 "persist a per-leader cursor; reconcile
on tick"; §4 "distinguish *zero* vs *unavailable*").  PolyEcho already does
exactly this for the trader via the ``settings`` row ``trader_heartbeat``
(``packages/core`` ``Trader._write_heartbeat`` → ``wiring.make_heartbeat_writer``
→ web ``lib/settings-keys.ts`` ``coerceHeartbeat``).  This module **generalises
that one proven pattern** to every worker plus an integration-probe snapshot,
with ZERO schema change — it reuses the existing ``settings`` table
(``key`` TEXT PK → ``value`` JSONB), exactly like ``trader_heartbeat``.

We adapt the *patterns*, not the Java: no Spring ``@Scheduled``/event-bus —
plain asyncio + the existing ``settings`` upsert; fail-soft probes (a failed
probe is data, never an exception — brief §4 "zero vs unavailable").

================================================================================
FROZEN ``settings``-key JSON contract  (cross-language; web + cli MIRROR this)
================================================================================
The ``settings`` row convention is **bare-JSONB, tolerant of ``{"value": …}``**
— IDENTICAL to ``trader_heartbeat`` / the Polymarket-proxy row.  Readers MUST
unwrap a one-level ``{"value": <obj>}`` envelope (see :func:`_unwrap`) and
treat every field as optional/defensive (external producer; row may be absent
or partial).

----------------------------------------------------------------------------
1. Per-worker heartbeat rows
----------------------------------------------------------------------------
Keys (one row each):

* ``trader_heartbeat``     — **already produced by ``packages/core``**; this
  module only *reads* it (does NOT change its producer or shape).
* ``discovery_heartbeat``  — produced by ``packages/discovery``.
* ``backtest_heartbeat``   — produced by ``packages/backtest``.
* ``push_heartbeat``       — produced by ``packages/push``.

JSON shape (superset; ``ts`` is the ONLY load-bearing field — it drives the
liveness pill, exactly as the web ``coerceHeartbeat`` already treats
``trader_heartbeat``)::

    {
      "ts":            "2026-05-19T12:34:56.789012+00:00",  # ISO-8601 UTC, REQUIRED
      "phase":         "supervised",     # free string: startup|supervised|shutdown|idle|...
      "datasource":    "fixture",        # "fixture" | "live"
      "live_trading":  false,            # bool — master live-money lock state
      "counters":      {"detected": 0, "copied": 0, ...},  # OPEN dict[str,int|float]
      ...                                # producers MAY add extra keys; readers ignore unknowns
    }

Back-compat note: ``packages/core`` historically writes the counter fields
(``detected``/``copied``/``skipped``/``placed``/``followed_leaders``) at the
**top level**, NOT under ``counters``.  :class:`WorkerHeartbeat` accepts BOTH:
it reads a nested ``counters`` dict if present and ALSO folds any top-level
numeric extras into ``counters`` so the existing ``trader_heartbeat`` row
parses unchanged and new workers can use the tidy nested form.  New producers
SHOULD use :func:`write_heartbeat` (nested ``counters``).

----------------------------------------------------------------------------
2. Integration-probe snapshot row
----------------------------------------------------------------------------
Key: ``integration_status`` — one row, written by whichever worker runs the
scheduled probe sweep (typically core).  JSON shape::

    {
      "checked_at": "2026-05-19T12:34:56+00:00",   # ISO-8601 UTC; sweep time
      "probes": {
        "config":              <ProbeResult>,
        "database":            <ProbeResult>,
        "polymarket_clob":     <ProbeResult>,
        "polymarket_data_api": <ProbeResult>,
        "polygon_rpc":         <ProbeResult>,
        "vpn_egress":          <ProbeResult>
      }
    }

  where each ``<ProbeResult>`` is the 4-key object documented just below
  (one entry per probe; ``config`` & ``database`` are local, the rest are
  network probes routed through the operator proxy).

Each probe value is a :class:`ProbeResult`:

    { "ok": bool, "detail": str, "latency_ms": float|null, "checked_at": ISO-8601|null }

``ok=false`` + a human ``detail`` means "checked and unavailable" (NEVER an
exception — brief §4 "distinguish zero vs unavailable").  ``ok`` absent/None
in a *consumed* row means "not yet probed".

----------------------------------------------------------------------------
3. Aggregated snapshot returned by :func:`get_status`
----------------------------------------------------------------------------
:func:`get_status` reads all of the above from ``settings`` and returns a
:class:`StatusSnapshot`::

    {
      "generated_at": ISO-8601 UTC,
      "workers": { "trader": WorkerHeartbeat|null, "discovery": ..., "backtest": ..., "push": ... },
      "integration": IntegrationSnapshot|null
    }

================================================================================
Public API (FROZEN — the 6 dependent agents build against these names)
================================================================================
Models      : ``ProbeResult``, ``WorkerHeartbeat``, ``IntegrationSnapshot``,
              ``StatusSnapshot``
Keys/consts : ``TRADER_HEARTBEAT_KEY``, ``DISCOVERY_HEARTBEAT_KEY``,
              ``BACKTEST_HEARTBEAT_KEY``, ``PUSH_HEARTBEAT_KEY``,
              ``INTEGRATION_STATUS_KEY``, ``WORKER_HEARTBEAT_KEYS``,
              ``HEARTBEAT_STALE_SECONDS``
Probes      : ``probe_config``, ``probe_database``, ``probe_polymarket_clob``,
              ``probe_polymarket_data_api``, ``probe_polygon_rpc``,
              ``probe_vpn_egress``, ``run_all_probes``
Writers     : ``write_heartbeat``, ``write_integration_status``
Aggregator  : ``get_status``

Fail-soft contract: every probe returns a :class:`ProbeResult` and NEVER
raises; every DB writer/reader is best-effort and degrades to ``None`` / a
``ok=false`` :class:`ProbeResult` (a transient DB blip shows "stale", never a
crash — mirrors ``wiring.make_heartbeat_writer``).  No module-import side
effects; no network/DB on import.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical ``settings`` keys (cross-language constants — mirror in TS/CLI)
# ---------------------------------------------------------------------------
#: Trader liveness row — **already produced by ``packages/core``**
#: (``wiring.TRADER_HEARTBEAT_KEY`` / web ``settings-keys.ts``).  Re-exported
#: here so every consumer imports the key from one Python place.
TRADER_HEARTBEAT_KEY = "trader_heartbeat"
#: Discovery worker liveness row.
DISCOVERY_HEARTBEAT_KEY = "discovery_heartbeat"
#: Backtest worker liveness row.
BACKTEST_HEARTBEAT_KEY = "backtest_heartbeat"
#: Web-push sender liveness row.
PUSH_HEARTBEAT_KEY = "push_heartbeat"
#: Integration-probe snapshot row (one row; whole sweep).
INTEGRATION_STATUS_KEY = "integration_status"

#: Logical worker name → its ``settings`` heartbeat key.  The aggregator and
#: the dependent agents iterate this so adding a worker is a one-line change.
WORKER_HEARTBEAT_KEYS: dict[str, str] = {
    "trader": TRADER_HEARTBEAT_KEY,
    "discovery": DISCOVERY_HEARTBEAT_KEY,
    "backtest": BACKTEST_HEARTBEAT_KEY,
    "push": PUSH_HEARTBEAT_KEY,
}

#: A heartbeat older than this many seconds is "stale" (worker treated DOWN).
#: 300 s == the web ``HEARTBEAT_STALE_MINUTES = 5`` — kept identical so the
#: Python ``get_status`` verdict and the TS dashboard pill never disagree.
HEARTBEAT_STALE_SECONDS: float = 300.0

#: Bounded per-probe network timeout (connect + read).  Short on purpose: a
#: status sweep must never hang the scheduler tick.
_PROBE_TIMEOUT_S: float = 10.0

#: Probe identifiers carried inside ``integration_status.probes``.
ProbeName = Literal[
    "config",
    "database",
    "polymarket_clob",
    "polymarket_data_api",
    "polygon_rpc",
    "vpn_egress",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    """Current instant as an ISO-8601 UTC string (the contract's ``ts``)."""
    return datetime.now(tz=UTC).isoformat()


def _unwrap(value: Any) -> Any:
    """Tolerate the ``{"value": <obj>}`` JSONB envelope (one level).

    The ``settings`` rows are bare-JSONB but the web layer may persist
    ``{"value": …}`` (IDENTICAL to ``coerceHeartbeat`` / the proxy row).
    Readers MUST go through this so the contract is symmetric across
    Python and TS.  Never raises.
    """
    if isinstance(value, dict) and "value" in value and isinstance(value["value"], dict):
        return value["value"]
    return value


# ---------------------------------------------------------------------------
# Typed contract models  (pydantic v2 — defensive, all fields optional)
# ---------------------------------------------------------------------------
class ProbeResult(BaseModel):
    """One integration probe outcome.

    ``ok=True`` → reachable; ``ok=False`` + ``detail`` → checked & unavailable
    (NEVER an exception — brief §4 "distinguish zero vs unavailable").
    ``model_dump(mode="json")`` yields the exact frozen probe JSON.
    """

    model_config = ConfigDict(extra="ignore")

    ok: bool = False
    detail: str = ""
    latency_ms: float | None = None
    checked_at: str | None = None


class WorkerHeartbeat(BaseModel):
    """A worker's last heartbeat (``*_heartbeat`` ``settings`` row).

    ``ts`` is the only load-bearing field (drives the liveness verdict —
    same rule the web ``coerceHeartbeat`` already applies to
    ``trader_heartbeat``).  Accepts BOTH the legacy top-level-counters shape
    (what ``packages/core`` writes today) and the tidy nested ``counters``
    shape (what :func:`write_heartbeat` writes) — see :meth:`from_settings`.
    """

    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    phase: str | None = None
    datasource: str | None = None
    live_trading: bool | None = None
    counters: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_settings(cls, value: Any) -> WorkerHeartbeat | None:
        """Coerce a raw ``settings.value`` to a heartbeat, or ``None``.

        Returns ``None`` when the row is absent / not an object / has no
        usable ISO ``ts`` (mirrors the web ``coerceHeartbeat`` → ``null``
        contract so the cross-language liveness verdict is identical).
        Folds legacy top-level numeric fields into ``counters`` so the
        existing ``trader_heartbeat`` row parses unchanged.
        """
        obj = _unwrap(value)
        if not isinstance(obj, dict):
            return None
        ts = obj.get("ts")
        if not isinstance(ts, str) or _parse_iso(ts) is None:
            return None

        known = {"ts", "phase", "datasource", "live_trading", "counters"}
        counters: dict[str, Any] = {}
        nested = obj.get("counters")
        if isinstance(nested, dict):
            counters.update(nested)
        # Legacy/back-compat: top-level numeric extras (detected, copied,
        # placed, …) and followed_leaders[] are folded into counters so the
        # current core producer needs zero changes.
        for k, v in obj.items():
            if k in known:
                continue
            if isinstance(v, bool):
                continue  # never coerce a bool into a numeric counter
            if isinstance(v, (int, float)) or isinstance(v, list):
                counters[k] = v
        try:
            return cls(
                ts=ts,
                phase=obj.get("phase"),
                datasource=obj.get("datasource"),
                live_trading=obj.get("live_trading"),
                counters=counters,
            )
        except Exception:  # noqa: BLE001 - external producer; never raise
            return None

    def is_stale(
        self,
        *,
        now: datetime | None = None,
        stale_seconds: float = HEARTBEAT_STALE_SECONDS,
    ) -> bool:
        """``True`` when the heartbeat is older than ``stale_seconds``.

        A heartbeat with no parseable ``ts`` is treated as stale (DOWN) —
        fail-safe: an unreadable liveness signal must never read as healthy.
        """
        parsed = _parse_iso(self.ts) if self.ts else None
        if parsed is None:
            return True
        ref = now or datetime.now(tz=UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)
        return (ref - parsed).total_seconds() > stale_seconds


class IntegrationSnapshot(BaseModel):
    """The ``integration_status`` row: when the sweep ran + each probe."""

    model_config = ConfigDict(extra="ignore")

    checked_at: str | None = None
    probes: dict[str, ProbeResult] = Field(default_factory=dict)

    @classmethod
    def from_settings(cls, value: Any) -> IntegrationSnapshot | None:
        """Coerce a raw ``settings.value`` to a snapshot, or ``None``."""
        obj = _unwrap(value)
        if not isinstance(obj, dict):
            return None
        raw_probes = obj.get("probes")
        probes: dict[str, ProbeResult] = {}
        if isinstance(raw_probes, dict):
            for name, pv in raw_probes.items():
                if isinstance(pv, dict):
                    try:
                        probes[name] = ProbeResult.model_validate(pv)
                    except Exception:  # noqa: BLE001 - tolerate junk rows
                        continue
        try:
            return cls(checked_at=obj.get("checked_at"), probes=probes)
        except Exception:  # noqa: BLE001
            return None


class StatusSnapshot(BaseModel):
    """Top-level aggregate the web + cli render (return of :func:`get_status`)."""

    model_config = ConfigDict(extra="ignore")

    generated_at: str = Field(default_factory=_now_iso)
    workers: dict[str, WorkerHeartbeat | None] = Field(default_factory=dict)
    integration: IntegrationSnapshot | None = None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a UTC-aware ``datetime``, else ``None``.

    Mirrors ``LiveDataSource._parse_utc``'s tolerance: a naive timestamp is
    assumed UTC.  Never raises.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Probes — fail-soft, bounded-timeout, proxy-aware.  NEVER raise.
# ---------------------------------------------------------------------------
class _RpcError(RuntimeError):
    """Internal: a 2xx response carrying a JSON-RPC ``error`` body.

    Raised by the polygon-RPC ``ok_detail`` callback so a transport-level
    2xx but logical-error response is reported ``ok=false`` with the RPC
    error.  :func:`_http_probe` lets this propagate (it is NOT a transport
    failure) so :func:`probe_polygon_rpc` can phrase the detail.
    """


def _result(ok: bool, detail: str, *, latency_ms: float | None = None) -> ProbeResult:
    return ProbeResult(
        ok=ok,
        detail=detail,
        latency_ms=latency_ms,
        checked_at=_now_iso(),
    )


def probe_config() -> ProbeResult:
    """Validate that ``Settings`` loads and the required vars are present.

    DRY source for ``cli/doctor._check_config``.  Parses the DSN
    *structurally* (SQLAlchemy ``make_url``) — never string-splits a
    connection string, never echoes the password (P3 hardening parity).
    """
    t0 = time.perf_counter()
    try:
        from polyecho.shared.config import get_settings

        settings = get_settings()
        db_url_value = settings.db_url.get_secret_value()
        missing: list[str] = []
        if not db_url_value:
            missing.append("POLYECHO_DB_URL")
        if not settings.polymarket_clob_url:
            missing.append("POLYECHO_POLYMARKET_CLOB_URL")
        if not settings.polymarket_data_api_url:
            missing.append("POLYECHO_POLYMARKET_DATA_API_URL")
        if missing:
            return _result(
                False,
                f"Missing required env vars: {', '.join(missing)}",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )

        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            parsed = make_url(db_url_value)
        except ArgumentError:
            return _result(
                False,
                "POLYECHO_DB_URL is not a valid SQLAlchemy URL.",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        return _result(
            True,
            f"driver={parsed.drivername} host={parsed.host or '-'} log_level={settings.log_level}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: report, never raise
        return _result(
            False,
            f"Failed to load settings: {exc}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )


async def probe_database() -> ProbeResult:
    """Open an async connection and run ``SELECT 1``; report latency.

    DRY source for ``cli/doctor._async_check_db``.
    """
    t0 = time.perf_counter()
    try:
        from sqlalchemy import text

        from polyecho.shared.db import get_engine

        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = (time.perf_counter() - t0) * 1000
        return _result(True, "SELECT 1 OK", latency_ms=latency_ms)
    except Exception as exc:  # noqa: BLE001 - fail-soft
        return _result(
            False,
            f"Connection failed: {exc}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )


async def _http_probe(
    label: str,
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    ok_detail: Any = None,
) -> ProbeResult:
    """Shared bounded-timeout, proxy-aware HTTP probe core.

    Routes through ``make_async_client_resolved`` so the operator proxy
    (PRD §8.1 — Polymarket geo-blocked in Taiwan) is honoured EXACTLY like
    every other Polymarket-bound client (the ``shared.http`` chokepoint).
    Catches every httpx failure mode into a ``ok=false`` result with a
    human ``detail`` — never raises (brief §4).
    """
    from polyecho.shared.http import make_async_client_resolved

    timeout = httpx.Timeout(_PROBE_TIMEOUT_S, connect=_PROBE_TIMEOUT_S)
    t0 = time.perf_counter()
    try:
        client = await make_async_client_resolved(timeout=timeout)
        async with client:
            if method == "POST":
                resp = await client.post(url, json=json_body)
            else:
                resp = await client.get(url)
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.is_success:
            detail = ok_detail(resp) if callable(ok_detail) else f"HTTP {resp.status_code}"
            return _result(True, str(detail), latency_ms=latency_ms)
        return _result(
            False,
            f"{label}: HTTP {resp.status_code}",
            latency_ms=latency_ms,
        )
    except httpx.TimeoutException:
        return _result(
            False,
            f"{label}: timed out after {_PROBE_TIMEOUT_S:.0f}s",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    except httpx.ConnectError:
        return _result(
            False,
            f"{label}: network unreachable (check connection / proxy)",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    except httpx.TransportError as exc:
        return _result(
            False,
            f"{label}: transport error: {exc}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
    except _RpcError:
        # NOT a transport failure: a 2xx with a logical RPC error. Let the
        # caller (probe_polygon_rpc) phrase the ``ok=false`` detail.
        raise
    except Exception as exc:  # noqa: BLE001 - fail-soft: never raise
        return _result(
            False,
            f"{label}: unexpected error: {exc}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )


async def probe_polymarket_clob() -> ProbeResult:
    """GET the CLOB ``/time`` health endpoint (proxy-aware, fail-soft).

    DRY source for ``cli/doctor._check_polymarket``.
    """
    try:
        from polyecho.shared.config import get_settings

        base = get_settings().polymarket_clob_url.rstrip("/")
    except Exception as exc:  # noqa: BLE001
        return _result(False, f"polymarket_clob: config error: {exc}")
    return await _http_probe("polymarket_clob", f"{base}/time")


async def probe_polymarket_data_api() -> ProbeResult:
    """GET a cheap Data-API endpoint (proxy-aware, fail-soft).

    Uses a tiny ``/leaderboard?limit=1`` GET — the same host the
    ``LiveDataSource`` reads, so this probe reflects the real data path's
    reachability (incl. the operator proxy / VPN egress).
    """
    try:
        from polyecho.shared.config import get_settings

        base = get_settings().polymarket_data_api_url.rstrip("/")
    except Exception as exc:  # noqa: BLE001
        return _result(False, f"polymarket_data_api: config error: {exc}")
    return await _http_probe(
        "polymarket_data_api",
        f"{base}/v1/leaderboard?category=OVERALL&timePeriod=MONTH&orderBy=PNL&limit=1&offset=0",
    )


async def probe_polygon_rpc() -> ProbeResult:
    """JSON-RPC ``eth_blockNumber`` against the Polygon RPC (fail-soft).

    Proves the on-chain path (path-B cross-check / EOA tx / Safe
    ``computeProxyAddress``) is reachable.  A 2xx with a ``result`` is OK;
    a JSON-RPC ``error`` body is reported as ``ok=false``.

    Endpoint iteration (advanced): ``POLYECHO_POLYGON_RPC_URL`` may be a
    comma-separated list (e.g. ``polygon.drpc.org,publicnode.com,1rpc.io``).
    This probe iterates the list in order, advancing on any transport-level
    failure (timeout/connect-error/HTTP-error) or JSON-RPC ``error`` body,
    and reports success (with detail naming the URL that worked) on the
    first endpoint that responds with a usable result.  Reports ``ok=false``
    only when EVERY endpoint fails (last error wins in the detail string).
    The single-URL form is fully back-compatible — a one-element list
    behaves exactly as before.
    """
    try:
        from polyecho.shared.config import get_settings

        urls = list(get_settings().polygon_rpc_urls)
    except Exception as exc:  # noqa: BLE001
        return _result(False, f"polygon_rpc: config error: {exc}")
    if not urls:
        return _result(False, "polygon_rpc: no endpoint configured")

    def _ok_factory(endpoint: str) -> Any:
        def _ok(resp: httpx.Response) -> str:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                return f"HTTP 200 (non-JSON body) via {endpoint}"
            if isinstance(body, dict) and body.get("error"):
                # 2xx but JSON-RPC-level error — surface it (not "ok").
                raise _RpcError(str(body.get("error")))
            result = body.get("result") if isinstance(body, dict) else None
            if result:
                return f"block {result} via {endpoint}" if len(urls) > 1 else f"block {result}"
            return f"HTTP 200 via {endpoint}" if len(urls) > 1 else "HTTP 200"

        return _ok

    last: ProbeResult | None = None
    for endpoint in urls:
        try:
            attempt = await _http_probe(
                "polygon_rpc",
                endpoint,
                method="POST",
                json_body={
                    "jsonrpc": "2.0",
                    "method": "eth_blockNumber",
                    "params": [],
                    "id": 1,
                },
                ok_detail=_ok_factory(endpoint),
            )
        except _RpcError as exc:
            attempt = _result(False, f"polygon_rpc: RPC error: {exc}")
        if attempt.ok:
            return attempt
        last = attempt
    # Every endpoint failed — return the LAST failure verbatim (preserves
    # the existing single-URL detail shape; in multi-URL mode the operator
    # can re-check the latency_ms / detail to identify the last-tried node).
    assert last is not None  # loop ran at least once (urls non-empty)
    return last


async def probe_vpn_egress() -> ProbeResult:
    """Report the **public egress IP** seen through the configured proxy.

    Geo-routing health (PRD §8.1): the operator runs behind a proxy/VPN
    because Polymarket is geo-blocked in Taiwan.  This shows the egress IP
    so the dashboard can confirm traffic is actually leaving via the
    proxy.  ``ok`` only means "an egress IP was resolved", not that it is
    in an allowed region (no geo-IP dependency is bundled).  Fail-soft.
    """
    return await _http_probe(
        "vpn_egress",
        "https://api.ipify.org?format=json",
        ok_detail=lambda r: (
            f"egress {r.json().get('ip')}"
            if isinstance(r.json(), dict) and r.json().get("ip")
            else "egress resolved"
        ),
    )


async def run_all_probes() -> dict[str, ProbeResult]:
    """Run every probe and return ``{ProbeName: ProbeResult}``.

    Sequential on purpose — a status sweep is infrequent and sequential
    keeps proxy connection pressure predictable; every probe is already
    individually bounded and fail-soft so the whole sweep cannot hang or
    raise.
    """
    return {
        "config": probe_config(),
        "database": await probe_database(),
        "polymarket_clob": await probe_polymarket_clob(),
        "polymarket_data_api": await probe_polymarket_data_api(),
        "polygon_rpc": await probe_polygon_rpc(),
        "vpn_egress": await probe_vpn_egress(),
    }


# ---------------------------------------------------------------------------
# Writers — best-effort ``settings`` upsert (mirrors wiring.make_heartbeat_writer)
# ---------------------------------------------------------------------------
async def _upsert_setting(key: str, value: dict[str, Any]) -> bool:
    """Upsert one ``settings`` row (``key`` → JSONB ``value``).

    Best-effort, exactly like ``core.wiring.make_heartbeat_writer``: any
    DB/import error is logged and swallowed (returns ``False``).  A
    heartbeat / status write must NEVER crash or stall its caller — a
    transient DB blip degrades the dashboard to "stale", never a crash.
    """
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Setting

        stmt = (
            pg_insert(Setting)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=["key"],  # settings.key PK
                set_={"value": value},
            )
        )
        async with session_scope() as session:
            await session.execute(stmt)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort liveness
        logger.warning(
            "health_settings_upsert_failed_continuing",
            extra={"key": key, "error": str(exc)},
        )
        return False


def _heartbeat_key(kind: str) -> str:
    """Resolve a worker ``kind`` to its ``settings`` key.

    Accepts a logical name (``trader``/``discovery``/``backtest``/``push``)
    or a full ``*_heartbeat`` key (idempotent).  Raises ``ValueError`` on an
    unknown kind — a producer wiring typo must fail loudly at the call site
    (this is the *producer* side; the *reader* side stays fail-soft).
    """
    if kind in WORKER_HEARTBEAT_KEYS:
        return WORKER_HEARTBEAT_KEYS[kind]
    if kind in set(WORKER_HEARTBEAT_KEYS.values()):
        return kind
    raise ValueError(
        f"unknown heartbeat kind {kind!r}; expected one of "
        f"{sorted(WORKER_HEARTBEAT_KEYS)} or a *_heartbeat key"
    )


async def write_heartbeat(
    kind: str,
    *,
    phase: str,
    datasource: str | None = None,
    live_trading: bool | None = None,
    counters: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Write a worker heartbeat to its ``settings`` row (best-effort).

    ``kind`` is ``"trader"|"discovery"|"backtest"|"push"`` (or a full
    ``*_heartbeat`` key).  Produces the FROZEN nested-``counters`` shape;
    ``ts`` is stamped here (ISO-8601 UTC) so every producer is consistent.
    Returns ``True`` on a successful upsert, ``False`` if it was swallowed
    (DB down) — callers SHOULD ignore the result (fire-and-forget liveness),
    it is returned only for tests / optional logging.

    NOTE: ``packages/core`` already writes ``trader_heartbeat`` via its own
    ``wiring.make_heartbeat_writer`` (top-level-counters legacy shape).  That
    producer is intentionally LEFT UNCHANGED; :class:`WorkerHeartbeat` reads
    both shapes.  New workers (discovery/backtest/push) SHOULD use this.
    """
    key = _heartbeat_key(kind)
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "phase": phase,
        "counters": dict(counters or {}),
    }
    if datasource is not None:
        payload["datasource"] = datasource
    if live_trading is not None:
        payload["live_trading"] = live_trading
    if extra:
        # Extra top-level keys are allowed by the contract (readers ignore
        # unknowns); never let them clobber the load-bearing fields.
        for k, v in extra.items():
            payload.setdefault(k, v)
    return await _upsert_setting(key, payload)


async def write_integration_status(
    probes: dict[str, ProbeResult] | None = None,
) -> IntegrationSnapshot:
    """Run the probe sweep (unless *probes* supplied) and persist it.

    Writes ``integration_status`` and returns the :class:`IntegrationSnapshot`
    that was written (so the caller / a scheduler can log/emit it without a
    re-read).  Best-effort persistence — the snapshot is returned even if the
    DB upsert was swallowed.
    """
    results = probes if probes is not None else await run_all_probes()
    snapshot = IntegrationSnapshot(checked_at=_now_iso(), probes=results)
    await _upsert_setting(INTEGRATION_STATUS_KEY, snapshot.model_dump(mode="json"))
    return snapshot


# ---------------------------------------------------------------------------
# Aggregator — the single read the web + cli render
# ---------------------------------------------------------------------------
async def get_status() -> StatusSnapshot:
    """Read every heartbeat + the integration row → typed snapshot.

    Single ``settings`` ``SELECT … WHERE key IN (...)`` (all keys at once),
    coerced through the defensive models.  Fail-soft: if the DB is
    unreachable every worker is ``None`` and ``integration`` is ``None``
    (the dashboard renders "missing", never errors) — the SAME contract the
    web ``coerceHeartbeat`` → ``null`` path already relies on.
    """
    snapshot = StatusSnapshot(
        workers={name: None for name in WORKER_HEARTBEAT_KEYS},
        integration=None,
    )
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Setting

        wanted = list(WORKER_HEARTBEAT_KEYS.values()) + [INTEGRATION_STATUS_KEY]
        async with session_scope() as session:
            rows = (
                (await session.execute(select(Setting).where(Setting.key.in_(wanted))))
                .scalars()
                .all()
            )
        by_key: dict[str, Any] = {r.key: r.value for r in rows}
    except Exception as exc:  # noqa: BLE001 - fail-soft: empty snapshot
        logger.warning(
            "health_get_status_read_failed_returning_empty",
            extra={"error": str(exc)},
        )
        return snapshot

    for name, key in WORKER_HEARTBEAT_KEYS.items():
        if key in by_key:
            snapshot.workers[name] = WorkerHeartbeat.from_settings(by_key[key])
    if INTEGRATION_STATUS_KEY in by_key:
        snapshot.integration = IntegrationSnapshot.from_settings(by_key[INTEGRATION_STATUS_KEY])
    return snapshot


__all__ = [
    # keys / constants
    "TRADER_HEARTBEAT_KEY",
    "DISCOVERY_HEARTBEAT_KEY",
    "BACKTEST_HEARTBEAT_KEY",
    "PUSH_HEARTBEAT_KEY",
    "INTEGRATION_STATUS_KEY",
    "WORKER_HEARTBEAT_KEYS",
    "HEARTBEAT_STALE_SECONDS",
    "ProbeName",
    # models
    "ProbeResult",
    "WorkerHeartbeat",
    "IntegrationSnapshot",
    "StatusSnapshot",
    # probes
    "probe_config",
    "probe_database",
    "probe_polymarket_clob",
    "probe_polymarket_data_api",
    "probe_polygon_rpc",
    "probe_vpn_egress",
    "run_all_probes",
    # writers
    "write_heartbeat",
    "write_integration_status",
    # aggregator
    "get_status",
]

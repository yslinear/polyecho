"""
polyecho.shared.http
====================
Single source of truth for outbound HTTP clients.

Every Polymarket-bound httpx client in PolyEcho is constructed here so that an
**optional, operator-configured proxy** (PRD §8.1 — the operator is geo-blocked
from Polymarket in Taiwan) is applied *consistently* across the
``LiveDataSource``, the ``fetch_usdc_balance`` probe, and the CLI ``doctor``
reachability check.  Behaviour is byte-for-byte identical to before when no
proxy is configured.

The proxy is read from ``Settings.polymarket_proxy_url``
(env ``POLYECHO_POLYMARKET_PROXY_URL``):

* ``None`` / empty  → clients are built **exactly as before** (no proxy kwarg
  is passed to httpx at all).
* ``http://`` / ``https://``  → passed straight through to httpx.
* ``socks5://``  → see the SOCKS limitation note below.

httpx version note
------------------
The repo locks **httpx 0.28.1** (``uv.lock``).  httpx ≥ 0.28 takes a single
``proxy=`` keyword (the legacy ``proxies=`` mapping was *removed* in 0.28).  We
therefore pass ``proxy=<url>``.  If the lock is ever rolled back to 0.26/0.27,
``proxy=`` is still accepted there, so this stays correct.

SOCKS limitation — ``# TODO(verify)``
-------------------------------------
httpx only speaks SOCKS when the optional ``socksio`` extra
(``httpx[socks]``) is installed.  ``socksio`` is **not** in ``uv.lock`` and the
project rule forbids adding a dependency / changing the lockfile, so a
``socks5://`` proxy URL is accepted by config but **cannot be honoured by the
httpx clients**: httpx will raise ``ImportError`` at request time telling the
operator to install ``httpx[socks]``.  http/https proxies are fully supported.
The CLOB (``requests``-based) path *can* use SOCKS if the operator has
``requests[socks]`` available, but that is likewise not locked.  Operators in
geo-blocked regions should use an ``http://``/``https://`` proxy.
"""

from __future__ import annotations

from typing import Any

import httpx

from polyecho.shared.config import get_settings

__all__ = [
    "make_async_client",
    "make_async_client_resolved",
    "get_proxy_url",
    "get_effective_proxy_url",
    "redact_proxy_url",
    "PROXY_SETTING_KEY",
]


def get_proxy_url() -> str | None:
    """Return the configured Polymarket proxy URL, or ``None`` if unset.

    Reads the process-wide ``Settings`` singleton.  An empty string is
    normalised to ``None`` so a blank ``POLYECHO_POLYMARKET_PROXY_URL`` behaves
    exactly like an unset one.
    """
    raw = get_settings().polymarket_proxy_url
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def redact_proxy_url(url: str | None) -> str:
    """Redact any embedded credentials in a proxy URL for safe logging.

    A proxy URL may carry ``user:pass@`` userinfo.  This never logs/returns
    the userinfo — only ``scheme://***@host:port`` (or ``scheme://host:port``
    when there is no userinfo).  Used so the proxy is never emitted at info
    level with credentials.
    """
    if not url:
        return "<none>"
    try:
        parsed = httpx.URL(url)
        host = parsed.host or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        userinfo = "***@" if (parsed.username or parsed.password) else ""
        return f"{parsed.scheme}://{userinfo}{host}{port}"
    except Exception:  # noqa: BLE001 - redaction must never raise
        return "<redacted>"


def make_async_client(**overrides: Any) -> httpx.AsyncClient:
    """Build an :class:`httpx.AsyncClient`, applying the optional proxy.

    This is the **only** place an outbound httpx client for Polymarket is
    constructed.  All keyword arguments are forwarded verbatim to
    ``httpx.AsyncClient`` (callers still pass ``base_url``, ``timeout``,
    ``headers``, ``transport`` for tests, …).

    Proxy behaviour:

    * ``polymarket_proxy_url`` unset/blank → **no** ``proxy`` kwarg is passed,
      so the client is identical to constructing ``httpx.AsyncClient(...)``
      directly (preserves exact pre-existing behaviour).
    * set → ``proxy=<url>`` is passed (httpx 0.28 single-proxy kwarg).

    An explicit ``proxy=`` in *overrides* always wins (lets a test force a
    specific value); an explicit ``transport=`` (e.g. ``MockTransport``) is
    forwarded untouched so tests stay network-free.
    """
    kwargs: dict[str, Any] = dict(overrides)
    if "proxy" not in kwargs:
        proxy = get_proxy_url()
        if proxy is not None:
            # httpx 0.28.1: single `proxy=` kwarg (the `proxies=` mapping was
            # removed in 0.28; `proxy=` is also valid on 0.26/0.27).
            kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


#: ``settings`` DB-table key the web /settings page writes; it overrides the
#: env-level proxy at runtime (PRD §5.7 "安全寫入" — control via DB settings).
PROXY_SETTING_KEY = "polymarket_proxy_url"


def _coerce_proxy(value: Any) -> str | None:
    """Coerce a stored ``settings.value`` into a proxy URL or ``None``.

    Tolerates a bare string or ``{"value"|"url": "..."}`` (the web layer may
    persist either shape into the JSONB column).
    """
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, dict):
        for k in ("value", "url", "polymarket_proxy_url"):
            inner = value.get(k)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


async def get_effective_proxy_url(session: Any = None) -> str | None:
    """Effective proxy: the ``settings`` DB row overrides the env value.

    Order: ``settings`` table key ``polymarket_proxy_url`` (set from the web
    /settings page) → ``POLYECHO_POLYMARKET_PROXY_URL`` env → ``None``.
    Never raises — any DB/import problem degrades to the env value so the
    Polymarket data path keeps working and tests / the web build need no DB.
    """
    try:
        from sqlalchemy import select

        from polyecho.shared.db import session_scope
        from polyecho.shared.models import Setting

        async def _read(sess: Any) -> str | None:
            row = (
                await sess.execute(
                    select(Setting).where(Setting.key == PROXY_SETTING_KEY)
                )
            ).scalar_one_or_none()
            return _coerce_proxy(row.value) if row is not None else None

        if session is not None:
            db_val = await _read(session)
        else:
            async with session_scope() as sess:
                db_val = await _read(sess)
        if db_val:
            return db_val
    except Exception:  # noqa: BLE001 - DB optional; fall back to env silently
        pass
    return get_proxy_url()


async def make_async_client_resolved(
    *, session: Any = None, **overrides: Any
) -> httpx.AsyncClient:
    """Like :func:`make_async_client` but the effective proxy is resolved from
    the ``settings`` DB row first (env fallback) — so the web /settings page
    takes effect at runtime for the live Polymarket data paths. An explicit
    ``proxy=`` / ``transport=`` in *overrides* still wins.
    """
    if "proxy" not in overrides:
        proxy = await get_effective_proxy_url(session)
        if proxy is not None:
            overrides["proxy"] = proxy
    return httpx.AsyncClient(**overrides)

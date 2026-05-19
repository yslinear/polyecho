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

import logging
import re
from typing import Any

import httpx

from polyecho.shared.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "make_async_client",
    "make_async_client_resolved",
    "get_proxy_url",
    "get_effective_proxy_url",
    "redact_proxy_url",
    "validate_proxy_url",
    "set_polymarket_proxy",
    "clear_polymarket_proxy",
    "ProxyValidationError",
    "PROXY_SETTING_KEY",
]


class ProxyValidationError(ValueError):
    """A proxy URL failed scheme / SSRF validation.

    Raised by :func:`validate_proxy_url` (and therefore
    :func:`set_polymarket_proxy`) so the CLI can surface one clear message
    and write nothing.  The same rules the web ``setPolymarketProxy``
    server action enforces are applied here so the CLI and the web /settings
    UI validate a proxy URL **identically** (single source of truth — both
    persist into the same ``settings`` row).
    """


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


# ---------------------------------------------------------------------------
# SSRF / scheme validation — a faithful Python port of the web
# ``setPolymarketProxy`` server action
# (``packages/web/app/settings/actions.ts``).  trader-core dials whatever is
# stored, so a loopback / RFC1918 / link-local / cloud-metadata target would
# let the dashboard pivot into internal services.  The CLI and the web MUST
# reject the same set of URLs (single source of truth: the same ``settings``
# row), so this mirrors that action's checks **exactly**:
#   * any scheme other than http/https (socks*, file:, gopher:, …) → reject
#   * empty host → reject
#   * literal IPv4 in loopback / RFC1918 / link-local / 0.0.0.0/8 → reject
#   * the cloud metadata IP 169.254.169.254 → reject
#   * local/internal IPv6 (::1, ::, fe80::/10, fc00::/7, IPv4-mapped local)
#   * the localhost-ish names: localhost, *.local, *.internal
#   * unparseable URL (e.g. "http://") → reject
# Residual (documented, identical to the web): a hostname that is NOT a
# literal IP / blocked name could still DNS-resolve to a private IP
# (DNS-rebind).  DNS is not resolved here; that residual is accepted and
# called out — full mitigation belongs to the dialing side (trader-core).
_SOCKS_RE = re.compile(r"^socks\d?://", re.IGNORECASE)
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _ipv4_parts(host: str) -> list[int] | None:
    """Parse a dotted-quad host into 4 ints in 0..255, else ``None``.

    Mirrors the web action's ``ipv4ToParts`` (rejects out-of-range octets).
    """
    m = _IPV4_RE.match(host)
    if not m:
        return None
    parts = [int(g) for g in m.groups()]
    if any(n < 0 or n > 255 for n in parts):
        return None
    return parts


def _is_blocked_ipv4(parts: list[int]) -> bool:
    """``True`` for loopback / RFC1918 / link-local / 0.0.0.0/8.

    Byte-for-byte the web action's ``isBlockedIpv4`` ranges.
    """
    a, b = parts[0], parts[1]
    if a == 127:  # 127.0.0.0/8 loopback
        return True
    if a == 10:  # 10.0.0.0/8 RFC1918
        return True
    if a == 172 and 16 <= b <= 31:  # 172.16.0.0/12 RFC1918
        return True
    if a == 192 and b == 168:  # 192.168.0.0/16 RFC1918
        return True
    if a == 169 and b == 254:  # 169.254.0.0/16 link-local (+ metadata)
        return True
    if a == 0:  # 0.0.0.0/8 "this host"
        return True
    return False


def _ssrf_reject(raw_hostname: str) -> str | None:
    """Return a rejection reason if *raw_hostname* is SSRF-dangerous.

    Same decision tree (and the same zh-TW messages) as the web action's
    ``ssrfReject`` so a URL accepted/rejected by the CLI is accepted/
    rejected identically by the web /settings form.
    """
    host = raw_hostname.strip().lower()
    if not host:
        return "代理位址缺少主機名稱"

    # Obvious localhost-ish / internal names.
    if host == "localhost":
        return "不可使用 localhost 作為代理主機"
    if host.endswith(".local") or host.endswith(".internal"):
        return "不可使用 .local / .internal 內網名稱作為代理主機"

    # IPv6 literals (httpx.URL.host strips the [] brackets, like URL.hostname).
    if ":" in host:
        v6 = re.sub(r"^\[|\]$", "", host)
        if (
            v6 == "::1"  # loopback
            or v6 == "::"  # unspecified
            or v6 == "0:0:0:0:0:0:0:1"
            or v6 == "0:0:0:0:0:0:0:0"
            or v6.startswith("fe80:")  # link-local fe80::/10
            or v6.startswith("fc")  # unique-local fc00::/7
            or v6.startswith("fd")
            or v6.startswith("::ffff:127.")  # IPv4-mapped loopback
            or v6.startswith("::ffff:10.")
            or v6.startswith("::ffff:192.168.")
            or v6.startswith("::ffff:169.254.")
        ):
            return "不可使用本機/內網 IPv6 位址作為代理主機"
        return None  # some other global IPv6 — allowed

    # IPv4 literal.
    v4 = _ipv4_parts(host)
    if v4 is not None:
        if v4[0] == 169 and v4[1] == 254 and v4[2] == 169 and v4[3] == 254:
            return "不可使用雲端中繼資料位址 169.254.169.254 作為代理主機"
        if _is_blocked_ipv4(v4):
            return "不可使用回送/內網/連結本地 IP 作為代理主機"

    return None


def validate_proxy_url(url: str) -> str:
    """Validate a non-empty proxy URL exactly as the web action does.

    Returns the trimmed, accepted URL (a bare string — the shape persisted
    into ``settings.value``).  Raises :class:`ProxyValidationError` with a
    clear message on any scheme / parse / SSRF failure, writing nothing.
    Callers that want "empty → clear" must handle the empty case *before*
    calling this (an empty string is rejected here, mirroring the web split
    where ``""`` deletes the row instead of validating).

    The check order matches ``setPolymarketProxy``: reject SOCKS → require
    http(s) prefix → parse + re-check the normalised scheme → SSRF-screen
    the host.
    """
    trimmed = url.strip()
    if not trimmed:
        raise ProxyValidationError(
            "Proxy URL is empty; use clear to remove the proxy."
        )

    # Reject SOCKS explicitly (httpx transport is http(s)-only).
    if _SOCKS_RE.match(trimmed):
        raise ProxyValidationError("SOCKS not supported; use http(s)")

    # Must be http:// or https://.
    if not _HTTP_RE.match(trimmed):
        raise ProxyValidationError(
            "Proxy URL must start with http:// or https://"
        )

    # Reject anything that is not a parseable URL (e.g. "http://"), and
    # re-check the normalised scheme (defence in depth, like the web action).
    try:
        parsed = httpx.URL(trimmed)
        host = parsed.host
        if not host:
            raise ValueError("no host")
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError("non-http(s) scheme")
    except ProxyValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalised to one clear message
        raise ProxyValidationError(
            "Enter a valid http(s) URL, e.g. http://203.0.113.10:8080"
        ) from exc

    reason = _ssrf_reject(host)
    if reason:
        raise ProxyValidationError(
            f"{reason}。請改用對外可路由的 http(s) 代理位址。"
        )

    return trimmed


# ---------------------------------------------------------------------------
# Write helpers — the Python counterpart to the web ``setPolymarketProxy``.
# Mirrors the proven optional-session select-then-update/add-then-flush
# pattern of ``polyecho.shared.danger_passphrase.set_danger_passphrase``.
# Both the CLI (via these) and the web action write the SAME single
# ``settings`` row keyed :data:`PROXY_SETTING_KEY` with the SAME bare-string
# JSONB shape that :func:`_coerce_proxy` / :func:`get_effective_proxy_url`
# reads — so CLI, web /settings, and the live trader-core path are one
# source of truth.
# ---------------------------------------------------------------------------
async def set_polymarket_proxy(url: str | None, *, session: Any = None) -> None:
    """Set (or clear) the operator Polymarket proxy in the ``settings`` row.

    ``url`` is SSRF/scheme-validated with :func:`validate_proxy_url` (same
    rules as the web) and, on success, the **bare trimmed string** is
    upserted into the single ``settings`` row keyed
    :data:`PROXY_SETTING_KEY` — byte-identical to the JSONB shape the web
    ``setPolymarketProxy`` writes and :func:`_coerce_proxy` reads.

    An empty / whitespace-only / ``None`` *url* means "no proxy": the row is
    **deleted** (matching the web action's "empty → delete" and the
    env-fallback semantics — once the DB row is gone,
    :func:`get_effective_proxy_url` falls back to
    ``POLYECHO_POLYMARKET_PROXY_URL`` then ``None``).  Delegating to
    :func:`clear_polymarket_proxy` keeps the delete path single-sourced.

    Args:
        url:     The proxy URL, or empty/``None`` to clear.
        session: Optional caller session; ``None`` → a new
                 :func:`~polyecho.shared.db.session_scope`.

    Raises:
        ProxyValidationError: If *url* is non-empty but fails validation.
                              Nothing is written in that case.
    """
    if url is None or not url.strip():
        await clear_polymarket_proxy(session=session)
        return

    value = validate_proxy_url(url)  # raises ProxyValidationError on reject

    from sqlalchemy import select

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Setting

    async def _write(sess: Any) -> None:
        row = (
            await sess.execute(
                select(Setting).where(Setting.key == PROXY_SETTING_KEY)
            )
        ).scalar_one_or_none()
        if row is None:
            sess.add(Setting(key=PROXY_SETTING_KEY, value=value))
        else:
            row.value = value
        await sess.flush()
        logger.info(
            "polymarket_proxy_set",
            extra={
                "updated": row is not None,
                "proxy": redact_proxy_url(value),
            },
        )

    if session is not None:
        await _write(session)
        return
    async with session_scope() as owned:
        await _write(owned)


async def clear_polymarket_proxy(*, session: Any = None) -> None:
    """Delete the ``settings`` proxy row → direct Polymarket connection.

    Removes the single row keyed :data:`PROXY_SETTING_KEY` (idempotent — a
    missing row is a no-op).  This matches the web action's "empty → delete"
    behaviour; afterwards :func:`get_effective_proxy_url` resolves via the
    ``POLYECHO_POLYMARKET_PROXY_URL`` env fallback, then ``None`` (direct).

    Args:
        session: Optional caller session; ``None`` → a new
                 :func:`~polyecho.shared.db.session_scope`.
    """
    from sqlalchemy import delete

    from polyecho.shared.db import session_scope
    from polyecho.shared.models import Setting

    async def _delete(sess: Any) -> None:
        await sess.execute(
            delete(Setting).where(Setting.key == PROXY_SETTING_KEY)
        )
        await sess.flush()
        logger.info("polymarket_proxy_cleared")

    if session is not None:
        await _delete(session)
        return
    async with session_scope() as owned:
        await _delete(owned)


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

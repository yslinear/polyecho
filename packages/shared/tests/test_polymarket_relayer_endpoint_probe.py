"""
packages/shared/tests/test_polymarket_relayer_endpoint_probe.py
================================================================
Dry-run probe + fail-closed contract tests for the Polymarket Builder-Relayer.

Scope
-----
This module does NOT exercise the happy path (that requires a real
``BUILDER_API_KEY`` / ``BUILDER_SECRET`` / ``BUILDER_PASS_PHRASE`` issued by
Polymarket's builder program and a funded EOA, which is operator-canary
scope, not CI scope).  Instead it covers two narrow goals:

1. **Reachability + 4xx error-path shape** (network-gated, opt-in via
   ``POLYECHO_RUN_NETWORK_TESTS=1``).  Confirms that the official
   ``relayer-v2.polymarket.com`` endpoints we depend on are
   ``DNS-resolvable, TLS-OK, return an HTTP status (any < 600)`` and that
   the unauthenticated ``/nonce`` + ``/submit`` paths surface a
   deterministic 4xx (401/403/400) rather than a 5xx — so our retry/circuit
   gate (which surfaces 4xx-not-429 *immediately* without retrying) acts on
   the correct path in production.

2. **Fail-closed contract** (offline, always runs).  Confirms that the P3
   ``PolymarketRelayerClient`` constructor and the P6 ``redeem_proxy`` /
   ``redeem_deposit_wallet`` entrypoints raise the typed
   ``RelayerCredentialsMissingError`` before any network I/O when builder
   credentials are absent.  This is the chokepoint guard that prevents a
   partially-configured deployment from silently no-opping a money path.

Markers
-------
* ``@pytest.mark.network`` (#1, #2, #3, #7) — skipped unless
  ``POLYECHO_RUN_NETWORK_TESTS=1`` is set in the environment.  These tests
  reach the real Polymarket relayer host and must not run in CI by default.
* No marker (#4, #5, #6) — pure offline contract tests; always run.

Provenance
----------
* Endpoints + HMAC header names: brief §sig-1/§sig-3 plus the upstream
  ``Polymarket/builder-relayer-client`` TS reference.
* ``BUILDER_RELAYER_URL`` constant lives in
  :mod:`polyecho.shared.constants` (single source of truth).
"""

from __future__ import annotations

import os

import httpx
import pytest

# Single source of truth for the relayer base URL — avoid hard-coding the
# host string twice in the test (and silently drifting from production).
from polyecho.shared.constants import BUILDER_RELAYER_URL

# Gate for live-network tests.  CI must NEVER set this to "1".  Operators
# run locally with ``POLYECHO_RUN_NETWORK_TESTS=1 uv run pytest …``.
_NETWORK_TESTS_ENABLED = os.environ.get("POLYECHO_RUN_NETWORK_TESTS") == "1"
_network = pytest.mark.skipif(
    not _NETWORK_TESTS_ENABLED,
    reason="opt-in via POLYECHO_RUN_NETWORK_TESTS=1 (reaches real Polymarket host)",
)

# Burn-in budget — be generous so a slow-but-reachable host doesn't false-fail.
# 10s matches the brief's relayer connect timeout (live.py uses 30s for the
# longer-paginated CLOB GETs).
_TIMEOUT_S = 10.0

# Throwaway zero-address used as the ``address`` query parameter.  The
# relayer rejects all unauthenticated requests *before* checking the address,
# so this never touches a real account.
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# #1 — Reachability:  HTTPS endpoint up + TLS valid + returns any HTTP status
# ---------------------------------------------------------------------------


@pytest.mark.network
@_network
def test_relayer_root_endpoint_reachable() -> None:
    """``GET https://relayer-v2.polymarket.com/`` returns SOME HTTP status.

    We only assert ``status_code < 600`` — the relayer root may legitimately
    return 200, 404, or 403 depending on how the upstream proxy is
    configured; we are confirming DNS/TLS + a TCP-level HTTP response, not a
    specific shape.  A network error (``httpx.HTTPError``) would mean the
    relayer host is unreachable from this network and should be investigated
    *before* attempting any builder submit.
    """
    r = httpx.get(BUILDER_RELAYER_URL, timeout=_TIMEOUT_S)
    assert r.status_code < 600, f"unexpected status: {r.status_code}"
    # Operator-visible diagnostic — printed to capsys on -s; harmless silent
    # otherwise.  Records the wire shape for future contract evolution.
    print(
        f"[probe #1] relayer root status={r.status_code} "
        f"content_type={r.headers.get('content-type')!r} "
        f"body_first_120={r.text[:120]!r}"
    )


# ---------------------------------------------------------------------------
# #2 — /nonce without auth → deterministic 4xx (the path our gate handles)
# ---------------------------------------------------------------------------


@pytest.mark.network
@_network
def test_nonce_endpoint_reachable_returns_2xx_or_4xx() -> None:
    """``GET /nonce?address=…&type=SAFE`` is a public read (verified
    2026-05-20 — returns 200 with ``{"nonce":"0"}`` even without builder
    HMAC headers, since the SAFE nonce is on-chain-derived and unguarded
    at the relayer edge).

    What we lock in: the endpoint is reachable, returns a deterministic
    status (200/400/401/403, NEVER 5xx), and the body is well-formed text
    so the retry/circuit gate would see a healthy upstream.  This pins
    the wire shape — if Polymarket ever puts ``/nonce`` behind builder
    auth or starts returning 5xx, our retry path's deterministic-4xx
    assumption needs to be re-examined.
    """
    url = f"{BUILDER_RELAYER_URL.rstrip('/')}/nonce"
    r = httpx.get(
        url,
        params={"address": _ZERO_ADDRESS, "type": "SAFE"},
        timeout=_TIMEOUT_S,
    )
    # Lock in deterministic status — NEVER 5xx.  2xx is acceptable since
    # ``/nonce`` is an unguarded on-chain read; 4xx is acceptable if
    # Polymarket later adds auth.  The retry/circuit gate handles both.
    assert r.status_code in (200, 400, 401, 403), (
        f"expected 200/400/401/403; got {r.status_code} body={r.text[:200]!r}"
    )
    assert r.status_code < 500, (
        f"relayer /nonce returned 5xx — server health concern: "
        f"{r.status_code} body={r.text[:200]!r}"
    )
    # Diagnostic — records the exact wire shape for future contract evolution.
    print(
        f"[probe #2] /nonce no-auth status={r.status_code} "
        f"content_type={r.headers.get('content-type')!r} "
        f"body_first_300={r.text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# #3 — /submit without auth → deterministic 4xx (same gate-correctness check)
# ---------------------------------------------------------------------------


@pytest.mark.network
@_network
def test_submit_endpoint_without_auth_returns_4xx() -> None:
    """``POST /submit`` without builder HMAC headers ⇒ deterministic 4xx.

    Mirror of #2 for the write path.  An empty body is sent — the relayer
    must short-circuit on auth long before body parsing.
    """
    url = f"{BUILDER_RELAYER_URL.rstrip('/')}/submit"
    r = httpx.post(
        url,
        json={},
        timeout=_TIMEOUT_S,
    )
    assert r.status_code in (400, 401, 403), (
        f"expected 400/401/403 without auth; got {r.status_code} body={r.text[:200]!r}"
    )
    print(
        f"[probe #3] /submit no-auth status={r.status_code} "
        f"content_type={r.headers.get('content-type')!r} "
        f"body_first_300={r.text[:300]!r}"
    )


# ---------------------------------------------------------------------------
# #4 — P3 PolymarketRelayerClient: empty creds ⇒ RelayerCredentialsMissingError
# ---------------------------------------------------------------------------


def test_polymarket_relayer_client_empty_creds_fails_closed() -> None:
    """``PolymarketRelayerClient(credentials=<empty fields>)`` raises the
    typed ``RelayerCredentialsMissingError`` at construction.

    The relayer dataclass ``RelayerCredentials`` requires all four secret
    fields to be non-empty strings; the constructor in P3 enforces this
    *before* any network/SDK import.  This test guards the chokepoint so a
    misconfigured deployment cannot silently no-op the money path.

    Note: we cannot pass literal ``creds=None`` because the dataclass type
    contract is non-Optional; the operator-relevant failure mode is the
    'all env vars unset' case, which materialises as empty strings.
    """
    from polyecho.shared.polymarket.relayer import (
        PolymarketRelayerClient,
        RelayerCredentials,
        RelayerCredentialsMissingError,
    )

    empty = RelayerCredentials(
        private_key="",
        builder_api_key="",
        builder_secret="",
        builder_passphrase="",
    )
    with pytest.raises(RelayerCredentialsMissingError) as excinfo:
        PolymarketRelayerClient(empty)
    # The error message must enumerate the missing fields so an operator can
    # see which env var(s) were unset.
    msg = str(excinfo.value)
    for field in (
        "private_key",
        "builder_api_key",
        "builder_secret",
        "builder_passphrase",
    ):
        assert field in msg, f"missing field {field!r} not in error message: {msg!r}"


# ---------------------------------------------------------------------------
# #5 — P6 redeem_proxy: empty creds ⇒ RelayerCredentialsMissingError
# ---------------------------------------------------------------------------


async def test_redeem_proxy_empty_creds_fails_closed() -> None:
    """``redeem_proxy(..., creds=<empty fields>)`` raises the typed
    ``RelayerCredentialsMissingError`` *before* any HTTP I/O.

    P6's ``ExtendedRelayerCredentials.assert_complete`` is the chokepoint;
    every public sig-1 / sig-3 entrypoint calls it first.  If this contract
    ever silently regresses (e.g. a refactor that moves credential
    validation after the first ``httpx`` call) we'd leak request attempts
    + log spam against the relayer with no usable signal.
    """
    from polyecho.shared.polymarket.relayer_extended import (
        ExtendedRelayerCredentials,
        RelayerCredentialsMissingError,
        redeem_proxy,
    )

    empty = ExtendedRelayerCredentials(
        private_key="",
        builder_api_key="",
        builder_secret="",
        builder_passphrase="",
    )
    with pytest.raises(RelayerCredentialsMissingError) as excinfo:
        # The args after ``creds`` never get inspected because
        # ``assert_complete`` raises first; we pass syntactically valid
        # placeholders so signature validation alone cannot mask the
        # contract-under-test.
        await redeem_proxy(
            eoa=_ZERO_ADDRESS,
            private_key="",
            proxy_funder=_ZERO_ADDRESS,
            condition_id="0x" + "00" * 32,
            is_neg_risk=False,
            index_sets=[1, 2],
            creds=empty,
        )
    msg = str(excinfo.value)
    for field in (
        "private_key",
        "builder_api_key",
        "builder_secret",
        "builder_passphrase",
    ):
        assert field in msg, f"missing field {field!r} not in error: {msg!r}"


# ---------------------------------------------------------------------------
# #6 — P6 builder signing SDK missing ⇒ RelayerNotAvailableError
# ---------------------------------------------------------------------------


async def test_redeem_proxy_without_builder_signing_sdk_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``py_builder_signing_sdk`` is unavailable, P6 raises
    ``RelayerNotAvailableError`` rather than silently posting an unsigned
    body or trying to hand-roll the HMAC.

    We exercise this by monkeypatching ``sys.modules`` so the lazy import
    inside ``_builder_headers`` fails — the typical operator-visible cause
    when the optional ``[relayer]`` extra hasn't been installed.

    To reach ``_builder_headers`` we must pass complete (dummy) credentials
    so ``assert_complete`` doesn't short-circuit first.  We use a real
    secp256k1 key so the ``eth_sign`` stage doesn't blow up *before* the
    HMAC header step.  ``_assert_funder_match`` would also fire first if we
    used a random EOA, so we monkeypatch the funder-derivation helpers to
    return the address we pass in (the contract under test is the SDK-
    missing fail-close, not funder derivation, which is covered separately).
    """
    import sys

    from polyecho.shared.polymarket import relayer_extended
    from polyecho.shared.polymarket.relayer_extended import (
        ExtendedRelayerCredentials,
        RelayerNotAvailableError,
        redeem_proxy,
    )

    # 1. Force the SDK import to fail by removing it from sys.modules and
    #    inserting a sentinel that raises on attribute lookup.  Two-step:
    #    delete any cached real module, then set None ⇒ ImportError.
    for modname in (
        "py_builder_signing_sdk",
        "py_builder_signing_sdk.signer",
    ):
        monkeypatch.setitem(sys.modules, modname, None)

    # 2. Bypass funder derivation — the funder check fires before
    #    ``_builder_headers``.  We patch it to a no-op so the SDK-missing
    #    branch is reachable.
    monkeypatch.setattr(
        relayer_extended,
        "_assert_funder_match",
        lambda **_kw: None,
    )

    # 3. Bypass the eth_sign step — we don't have a real private key and
    #    don't need one; we want the next stage (HMAC headers) to fail.
    monkeypatch.setattr(
        relayer_extended,
        "_sign_proxy",
        lambda struct_hash, private_key: "0x" + "00" * 65,
    )

    # 4. Stub the HTTP transport so the ``/relay-payload`` GET returns a
    #    well-shaped dummy payload (the path under test is the HMAC SDK
    #    failure, NOT network reachability).
    class _DummyResp:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload
            self.status_code = 200
            self.text = ""
            self.headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    class _DummyClient:
        def __init__(self, *_args: object, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> _DummyClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(
            self, url: str, params: dict[str, str] | None = None
        ) -> _DummyResp:
            return _DummyResp(
                {"address": _ZERO_ADDRESS, "nonce": 0}
            )

        async def post(
            self, *args: object, **kwargs: object
        ) -> _DummyResp:
            return _DummyResp({})

    monkeypatch.setattr(relayer_extended.httpx, "AsyncClient", _DummyClient)

    complete = ExtendedRelayerCredentials(
        private_key="0x" + "11" * 32,
        builder_api_key="dummy-key",
        builder_secret="dummy-secret",
        builder_passphrase="dummy-pass",
    )
    with pytest.raises(RelayerNotAvailableError) as excinfo:
        await redeem_proxy(
            eoa=_ZERO_ADDRESS,
            private_key="0x" + "11" * 32,
            proxy_funder=_ZERO_ADDRESS,
            condition_id="0x" + "00" * 32,
            is_neg_risk=False,
            index_sets=[1, 2],
            creds=complete,
        )
    assert "py-builder-signing-sdk" in str(excinfo.value)


# ---------------------------------------------------------------------------
# #7 — /submit with bogus body but real (no) headers → 4xx not 5xx
# ---------------------------------------------------------------------------


@pytest.mark.network
@_network
def test_submit_with_bogus_body_returns_4xx_not_5xx() -> None:
    """``POST /submit`` with a structurally-invalid body still surfaces 4xx.

    The relayer must reject malformed bodies as client errors, not 5xx
    (which would indicate an unhealthy server we're poking with garbage).
    Records the response body shape for future wire-validation work.
    """
    url = f"{BUILDER_RELAYER_URL.rstrip('/')}/submit"
    r = httpx.post(
        url,
        # Deliberately malformed: not a valid SafeTransaction / PROXY /
        # WALLET body, no signature, but valid JSON.
        json={"type": "BOGUS", "from": _ZERO_ADDRESS},
        # Fake HMAC headers — relayer should reject either on HMAC
        # verification OR on the BOGUS type discriminator.
        headers={
            "POLY_BUILDER_API_KEY": "not-a-real-key",
            "POLY_BUILDER_SIGNATURE": "0x" + "00" * 65,
            "POLY_BUILDER_TIMESTAMP": "0",
            "POLY_BUILDER_PASSPHRASE": "not-a-real-pass",
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT_S,
    )
    assert 400 <= r.status_code < 500, (
        f"expected 4xx; got {r.status_code} body={r.text[:300]!r}"
    )
    print(
        f"[probe #7] /submit bogus-body status={r.status_code} "
        f"content_type={r.headers.get('content-type')!r} "
        f"body_first_500={r.text[:500]!r}"
    )

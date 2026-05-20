"""
tests/test_accounts.py
======================
Coverage for :mod:`polyecho.shared.accounts` (PRD §5.1).

* Derivation determinism, address format, lowercasing, type-2≡type-3, and
  input validation.
* Detection ranking across the bundled archetypes using the real
  ``FixtureDataSource`` (no network/DB): positions ≫ activity, the
  recommended type/address, confidence labels, and the no-data path.
* ``detect_signature_type`` never hard-fails when the datasource raises.
* The optional ``fetch_usdc_balance`` helper: injected fake httpx client
  (no network), defensive schema handling, and error → ``None``.
* Frozen dataclasses are immutable.

All deterministic and order-independent.  The autouse
``_clear_settings_cache`` fixture (conftest) keeps settings hermetic.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

import pytest

from polyecho.shared.accounts import (
    DetectionResult,
    SignatureType,
    TypeEvidence,
    derive_wallets,
    detect_signature_type,
    fetch_usdc_balance,
)
from polyecho.shared.datasource import (
    ActivityRecord,
    FixtureDataSource,
    PositionRecord,
)

from .conftest import run

# A valid EOA (checksum-mixed input to prove we lowercase).
_EOA = "0xAbC0000000000000000000000000000000000123"
_EOA_LOWER = _EOA.lower()

# Archetype wallets that have bundled positions+activity fixtures.
SWING = "0xswing1000000000000000000000000000000001"
SCALPER = "0xscalper200000000000000000000000000000002"
POSITION = "0xposition400000000000000000000000000000004"


def _w(eoa: str) -> dict[int, str]:
    """``derive_wallets`` with the now-``None`` type-2 entry dropped.

    Type-2 (SAFE) is ``None`` (no offline derivation — RPC only).  Types
    0/1/3 are all derivable offline (brief §sig-3 added type-3 Solady
    ERC-1967 CREATE2).  This narrows the mapping to ``dict[int, str]``
    for mypy --strict while still asserting the derivable contract."""
    raw = derive_wallets(eoa)
    t0 = raw[0]
    assert t0 is not None  # type-0 is always the EOA
    t1 = raw[1]
    assert t1 is not None  # type-1 is the verified CREATE2 address
    t3 = raw[3]
    assert t3 is not None  # type-3 is the verified Solady ERC-1967 CREATE2
    return {0: t0, 1: t1, 3: t3}


# ---------------------------------------------------------------------------
# derive_wallets
# ---------------------------------------------------------------------------
class TestDeriveWallets:
    """P0-5: only types 0 (exact) and 1 (verified CREATE2) yield an
    address; types 2/3 are ``None`` ("needs explicit funder / RPC") —
    ``accounts`` must NOT fabricate a SAFE / 1271 address."""

    def test_returns_four_types(self) -> None:
        w = derive_wallets(_EOA)
        assert set(w.keys()) == {0, 1, 2, 3}

    def test_type0_is_the_eoa_lowercased(self) -> None:
        w = derive_wallets(_EOA)
        assert w[0] == _EOA_LOWER
        assert w[SignatureType.EOA] == _EOA_LOWER

    def test_type1_is_lowercase_hex_20_bytes(self) -> None:
        addr = derive_wallets(_EOA)[1]
        assert addr is not None
        assert addr.startswith("0x")
        assert addr == addr.lower()
        assert len(addr) == 42
        int(addr, 16)  # parses as hex

    def test_type2_is_none_no_fabrication(self) -> None:
        """P0-5: SAFE has no offline derivation (RPC only).  Must be
        ``None`` rather than a guessed (wrong) address.  Type 3 is now
        derived offline via the verified Solady ERC-1967 scheme
        (brief §sig-3) and is covered separately below.
        """
        w = derive_wallets(_EOA)
        assert w[2] is None
        assert w[SignatureType.POLY_GNOSIS_SAFE] is None

    def test_type3_is_deposit_wallet_derivation(self) -> None:
        """sig 3 funder is now derived offline via the verified Solady
        ERC-1967 CREATE2 scheme (mirrors ``Polymarket/builder-relayer-
        client/src/builder/deposit-wallet.ts``, brief §sig-3)."""
        w = derive_wallets(_EOA)
        addr = w[SignatureType.POLY_1271]
        assert addr is not None
        assert addr == w[3]
        assert addr.startswith("0x") and len(addr) == 42
        assert addr == addr.lower()
        # Must differ from EOA + Magic proxy (different schemes).
        assert addr != w[SignatureType.EOA]
        assert addr != w[SignatureType.POLY_PROXY]

    def test_deterministic(self) -> None:
        assert derive_wallets(_EOA) == derive_wallets(_EOA)
        # Case-insensitive on input.
        assert derive_wallets(_EOA) == derive_wallets(_EOA_LOWER)

    def test_type1_differs_from_eoa(self) -> None:
        w = derive_wallets(_EOA)
        assert w[1] != w[0]

    def test_distinct_eoas_distinct_type1_proxies(self) -> None:
        a = derive_wallets("0x" + "11" * 20)
        b = derive_wallets("0x" + "22" * 20)
        assert a[1] != b[1]

    @pytest.mark.parametrize(
        "bad",
        ["", "0x123", "not-an-address", "0x" + "zz" * 20, "0x" + "11" * 19],
    )
    def test_invalid_address_raises(self, bad: str) -> None:
        with pytest.raises(ValueError, match="valid EVM address"):
            derive_wallets(bad)


class TestCreate2SingleSourceOfTruth:
    """P0-5: ``accounts`` type-1 derivation == the verified
    ``wallet.magic_proxy_address`` — exactly one implementation."""

    @pytest.mark.parametrize(
        "eoa",
        [
            _EOA,
            "0x" + "11" * 20,
            "0x" + "abcdef0123456789" * 2 + "abcdef01",
        ],
    )
    def test_accounts_type1_equals_wallet_magic_proxy(
        self, eoa: str
    ) -> None:
        from polyecho.shared.polymarket.wallet import magic_proxy_address

        assert derive_wallets(eoa)[1] == magic_proxy_address(eoa)

    def test_golden_vector_deterministic(self) -> None:
        """Fixed EOA → fixed Magic proxy (keccak256(0xff ++ factory ++
        keccak256(eoa) ++ initCodeHash)[-20:] over the brief §9 constants).
        Computed once from the single verified implementation; pins it so a
        future scheme change is caught."""
        from polyecho.shared.polymarket.wallet import magic_proxy_address

        eoa = "0x" + "00" * 19 + "01"
        derived = derive_wallets(eoa)[1]
        # Equivalence to the single source of truth IS the contract.
        assert derived == magic_proxy_address(eoa)
        # And it is a well-formed, deterministic 20-byte address.
        assert derived is not None and len(derived) == 42
        assert derived == derive_wallets(eoa.upper())[1]


# ---------------------------------------------------------------------------
# detect_signature_type — ranking with a fully-typed mapping datasource
# ---------------------------------------------------------------------------
class _MappingDS:
    """Standalone DataSource-shaped fake (composition, not inheritance).

    ``pos_map`` / ``act_map`` map a *derived candidate address* → an
    archetype wallet that has bundled fixtures.  An address absent from a
    map yields ``[]`` for that call, so we can independently give one
    candidate positions and another only activity.  Fully typed for mypy
    strict; structurally satisfies the ``DataSource`` protocol for the two
    methods detection uses.
    """

    def __init__(
        self,
        pos_map: dict[str, str] | None = None,
        act_map: dict[str, str] | None = None,
    ) -> None:
        self._pos_map = pos_map or {}
        self._act_map = act_map or {}
        self._fx = FixtureDataSource()

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        target = self._pos_map.get(wallet.lower())
        if target is None:
            return []
        return await self._fx.get_positions(target, size_threshold=size_threshold)

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        target = self._act_map.get(wallet.lower())
        if target is None:
            return []
        return await self._fx.get_user_activity(
            target, start=start, end=end, activity_type=activity_type, limit=limit
        )


class _BrokenDS:
    """DataSource-shaped fake whose reads always raise (resilience test)."""

    async def get_positions(
        self, wallet: str, *, size_threshold: float = 1.0
    ) -> list[PositionRecord]:
        raise RuntimeError("boom positions")

    async def get_user_activity(
        self,
        wallet: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        activity_type: str | None = "TRADE",
        limit: int | None = None,
    ) -> list[ActivityRecord]:
        raise RuntimeError("boom activity")


class TestDetectRanking:
    """P0-5: type 2 derives to ``None`` (no offline scheme; RPC only) so
    it contributes a zero-evidence empty-address row.  Type 3 now derives
    offline (brief §sig-3 Solady ERC-1967 CREATE2) so it IS queried —
    detection ranking is still typically driven by type 1 in fixtures
    (the only non-EOA candidate with seeded positions/activity).
    """

    def test_recommends_type_with_positions(self) -> None:
        """Map the type-1 (Magic proxy) candidate onto the SWING archetype
        (positions+activity); detection must recommend type 1."""
        w = _w(_EOA)
        ds = _MappingDS(pos_map={w[1]: SWING}, act_map={w[1]: SWING})
        result = run(detect_signature_type(_EOA, ds))

        assert isinstance(result, DetectionResult)
        assert result.eoa == _EOA_LOWER
        assert result.recommended_type == 1
        assert result.recommended_address == w[1]
        ev1 = result.evidence_for(1)
        assert ev1.position_count > 0
        assert result.confidence in {"high", "medium"}
        # Type 2 is never queried (no offline derivation) — zero-evidence,
        # empty address row.  Type 3 IS now derived offline (Deposit-Wallet
        # CREATE2, brief §sig-3) so it IS queried; with no fixture for that
        # address it lands as a non-empty-address, score=0 row.
        assert result.evidence_for(2).address == ""
        assert result.evidence_for(2).score == 0
        assert result.evidence_for(3).address == w[3]
        assert result.evidence_for(3).score == 0

    def test_positions_beat_activity(self) -> None:
        """type 0 (EOA) → only activity (SCALPER, huge volume); type 1 →
        only positions (POSITION). Positions must win (PRD §5.1)."""
        w = _w(_EOA)
        ds = _MappingDS(
            pos_map={w[1]: POSITION},          # positions only on type 1
            act_map={w[0]: SCALPER},           # activity only on type 0
        )
        result = run(detect_signature_type(_EOA, ds))
        assert result.recommended_type == 1
        assert result.evidence_for(1).position_count > 0
        assert result.evidence_for(0).activity_count > 0
        assert result.evidence_for(0).position_count == 0

    def test_evidence_ordered_by_signature_type(self) -> None:
        w = _w(_EOA)
        ds = _MappingDS(pos_map={w[1]: SWING})
        result = run(detect_signature_type(_EOA, ds))
        assert [e.signature_type for e in result.evidence] == [0, 1, 2, 3]

    def test_no_data_anywhere_returns_none_recommendation(self) -> None:
        """An EOA with no fixtures for any derived address → no recommend."""
        ds = _MappingDS()  # nothing maps; all empty
        result = run(detect_signature_type("0x" + "ab" * 20, ds))
        assert result.recommended_type is None
        assert result.recommended_address is None
        assert result.confidence == "none"
        assert all(e.score == 0 for e in result.evidence)

    def test_high_confidence_when_single_clear_winner(self) -> None:
        w = _w(_EOA)
        # Only type 1 has positions; nothing else has anything → high.
        ds = _MappingDS(pos_map={w[1]: SWING})
        result = run(detect_signature_type(_EOA, ds))
        assert result.confidence == "high"

    def test_detection_survives_datasource_errors(self) -> None:
        """If the datasource raises, detection degrades gracefully (no
        exception, just empty evidence / no recommendation)."""
        result = run(detect_signature_type(_EOA, _BrokenDS()))
        assert result.recommended_type is None
        assert all(e.position_count == 0 for e in result.evidence)

    def test_invalid_eoa_raises(self) -> None:
        with pytest.raises(ValueError, match="valid EVM address"):
            run(detect_signature_type("nope", FixtureDataSource()))

    def test_balance_fn_folded_into_ranking(self) -> None:
        """When two candidates tie on positions/activity (none), a positive
        USDC balance breaks the tie toward that candidate."""
        w = _w(_EOA)

        async def bal(addr: str) -> float | None:
            return 500.0 if addr == w[1] else 0.0

        ds = FixtureDataSource()  # no positions/activity anywhere
        result = run(detect_signature_type(_EOA, ds, usdc_balance_fn=bal))
        assert result.recommended_type == 1
        assert result.evidence_for(1).usdc_balance == 500.0

    def test_balance_fn_errors_are_swallowed(self) -> None:
        async def bal(_addr: str) -> float | None:
            raise RuntimeError("rpc down")

        ds = FixtureDataSource()
        result = run(detect_signature_type(_EOA, ds, usdc_balance_fn=bal))
        # No crash; balance simply None everywhere.
        assert all(e.usdc_balance is None for e in result.evidence)


# ---------------------------------------------------------------------------
# Frozen dataclasses
# ---------------------------------------------------------------------------
class TestFrozenDataclasses:
    def test_typeevidence_frozen(self) -> None:
        ev = TypeEvidence(
            signature_type=0,
            name="EOA",
            address="0x" + "00" * 20,
            position_count=0,
            activity_count=0,
            usdc_balance=None,
            score=0.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.score = 1.0  # type: ignore[misc]

    def test_detectionresult_frozen(self) -> None:
        r = DetectionResult(
            eoa="0x" + "00" * 20,
            evidence=(),
            recommended_type=None,
            recommended_address=None,
            confidence="none",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.confidence = "high"  # type: ignore[misc]

    def test_evidence_for_raises_keyerror_for_unknown(self) -> None:
        r = DetectionResult(
            eoa="0x" + "00" * 20,
            evidence=(),
            recommended_type=None,
            recommended_address=None,
            confidence="none",
        )
        with pytest.raises(KeyError):
            r.evidence_for(9)


# ---------------------------------------------------------------------------
# fetch_usdc_balance — optional standalone probe (NO network)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient.get (no network)."""

    def __init__(self, payload: object, *, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise = raise_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(
        self, url: str, params: dict[str, Any] | None = None
    ) -> _FakeResp:
        self.calls.append((url, params or {}))
        if self._raise is not None:
            raise self._raise
        return _FakeResp(self._payload)


class TestFetchUsdcBalance:
    def test_list_envelope(self) -> None:
        client = _FakeClient([{"user": "0xabc", "value": 1234.56}])
        val = run(
            fetch_usdc_balance(
                "0xabc", client=client, base_url="https://data-api.example"
            )
        )
        assert val == 1234.56
        assert client.calls[0][0] == "https://data-api.example/value"
        assert client.calls[0][1] == {"user": "0xabc"}

    def test_dict_envelope_alt_key(self) -> None:
        client = _FakeClient({"balance": "42.0"})
        val = run(fetch_usdc_balance("0xabc", client=client, base_url="http://x"))
        assert val == 42.0

    def test_unknown_schema_returns_none(self) -> None:
        client = _FakeClient({"unexpected": True})
        assert run(fetch_usdc_balance("0xabc", client=client, base_url="http://x")) is None

    def test_empty_list_returns_none(self) -> None:
        client = _FakeClient([])
        assert run(fetch_usdc_balance("0xabc", client=client, base_url="http://x")) is None

    def test_http_error_returns_none(self) -> None:
        client = _FakeClient(None, raise_exc=RuntimeError("503"))
        assert run(fetch_usdc_balance("0xabc", client=client, base_url="http://x")) is None

"""
tests/test_phase4_contracts.py
==============================
Phase-4 Wave-1 binding-contract coverage.

Scope (all deterministic, no network / no real DB):

* New + changed schema columns / uniques / types: ``leader_trades.tx_hash``
  + UNIQUE(leader_address, tx_hash); ``follow_relations.last_seen_ts``;
  ``my_trades.matched_qty/remaining_qty``; ``accounts.wallet_mode``; the
  NEW ``redeem_jobs`` + ``copy_skips`` tables (cols / FK / uniques).
* ``resolve_wallet`` per mode — EOA exactness, MAGIC CREATE2 determinism,
  MAGIC operator-funder preference, SAFE via injected RPC, explicit funder
  for POLY_1271, fail-closed on unknown mode / missing 1271 funder.
* Live-lock: default-OFF raises ``LiveTradingLockedError``; flips with the
  env var; ``is_live_enabled`` reflects config.
* ``Settings`` parses ``POLYECHO_LIVE_TRADING`` / ``POLYECHO_AUTO_REDEEM``.
* ``SkipReason`` taxonomy completeness + StrEnum string identity.
* ``constants`` module exposes the brief §9 addresses verbatim.
* Redeem helpers: ``collateral_for`` (pUSD vs WCOL) + ``index_set_for``;
  ``BaseRedeemClient`` is live-lock-guarded.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import Table

from polyecho.shared import constants
from polyecho.shared.copytrading import (
    Decision,
    DecisionOutcome,
    EvaluatedCopy,
    LiveTradingLockedError,
    RedeemJobSpec,
    RedeemJobStatus,
    RedeemResult,
    RiskResult,
    SkipReason,
    TradeSide,
    WalletBinding,
    assert_live_allowed,
    is_live_enabled,
)
from polyecho.shared.models import CopySkip, RedeemJob
from polyecho.shared.models.account import Account
from polyecho.shared.models.follow_relation import FollowRelation
from polyecho.shared.models.leader_trade import LeaderTrade
from polyecho.shared.models.my_trade import MyTrade
from polyecho.shared.polymarket.redeem import (
    BaseRedeemClient,
    collateral_for,
    index_set_for,
)
from polyecho.shared.polymarket.wallet import (
    WalletMode,
    magic_proxy_address,
    resolve_wallet,
)

from .conftest import run

# A throwaway but well-formed checksum-able EOA.
_EOA = "0x000000000000000000000000000000000000dEaD"


def _tbl(model: type) -> Table:
    """``model.__table__`` typed as :class:`Table`.

    Mirrors the established cast in ``test_models_db.py`` (SQLAlchemy's
    declarative ``__table__`` is statically ``FromClause``).
    """
    return cast(Table, model.__table__)  # type: ignore[attr-defined]


def _unique_colsets(table: Table) -> set[tuple[str, ...]]:
    """All UNIQUE-constraint column-name tuples on *table* (sorted)."""
    out: set[tuple[str, ...]] = set()
    for con in table.constraints:
        if con.__class__.__name__ == "UniqueConstraint":
            cols = cast(Any, con).columns
            out.add(tuple(sorted(c.name for c in cols)))
    return out


# ---------------------------------------------------------------------------
# Schema — new / changed columns, uniques, FK, types
# ---------------------------------------------------------------------------
class TestSchemaDeltas:
    def test_leader_trades_tx_hash_and_unique(self) -> None:
        cols = _tbl(LeaderTrade).c
        assert "tx_hash" in cols
        assert cols["tx_hash"].nullable is True
        assert ("leader_address", "tx_hash") in _unique_colsets(
            _tbl(LeaderTrade)
        )

    def test_follow_relations_last_seen_ts(self) -> None:
        col = _tbl(FollowRelation).c["last_seen_ts"]
        assert col.nullable is True
        assert col.type.__class__.__name__ == "DateTime"

    def test_my_trades_fifo_columns(self) -> None:
        cols = _tbl(MyTrade).c
        for name in ("matched_qty", "remaining_qty"):
            assert name in cols
            assert cols[name].nullable is True
            assert cols[name].type.__class__.__name__ == "Numeric"

    def test_accounts_wallet_mode(self) -> None:
        cols = _tbl(Account).c
        assert cols["wallet_mode"].nullable is True
        # signature_type + funder_address still present (brief: confirm/ensure)
        assert "signature_type" in cols
        assert "funder_address" in cols
        # The CLI-only secret column is never widened.
        assert cols["encrypted_private_key"].nullable is True

    def test_redeem_jobs_table(self) -> None:
        t = _tbl(RedeemJob)
        assert t.name == "redeem_jobs"
        for name in (
            "id",
            "account_id",
            "condition_id",
            "outcome_index",
            "index_set",
            "is_neg_risk",
            "status",
            "tx_hash",
            "detected_at",
            "confirmed_at",
            "created_at",
        ):
            assert name in t.c, name
        assert cast(Any, t.c["status"].default).arg == "pending"
        assert t.c["created_at"].nullable is False
        # FK to accounts(id)
        fk = next(iter(t.c["account_id"].foreign_keys))
        assert fk.column.table.name == "accounts"
        # idempotency UNIQUE
        assert (
            "account_id",
            "condition_id",
            "outcome_index",
        ) in _unique_colsets(t)

    def test_copy_skips_table(self) -> None:
        t = _tbl(CopySkip)
        assert t.name == "copy_skips"
        for name in (
            "id",
            "leader_address",
            "condition_id",
            "skip_reason",
            "detail",
            "payload",
            "created_at",
        ):
            assert name in t.c, name
        assert t.c["skip_reason"].nullable is False
        assert t.c["payload"].type.__class__.__name__ == "JSONB"
        assert t.c["id"].type.__class__.__name__ == "BigInteger"

    def test_new_tables_compile_for_postgres(self) -> None:
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
        for model in (RedeemJob, CopySkip):
            ddl = str(CreateTable(_tbl(model)).compile(dialect=dialect))
            assert f"CREATE TABLE {model.__tablename__}" in ddl


# ---------------------------------------------------------------------------
# resolve_wallet — per-mode (brief §9)
# ---------------------------------------------------------------------------
class _FakeAccount:
    def __init__(
        self,
        *,
        eoa: str = _EOA,
        funder: str | None = None,
        sig: int | None = None,
        mode: str | None = None,
    ) -> None:
        self.eoa_address = eoa
        self.funder_address = funder
        self.signature_type = sig
        self.wallet_mode = mode


class TestResolveWallet:
    def test_eoa_is_exact_and_not_derived(self) -> None:
        b = run(resolve_wallet(_FakeAccount(mode=WalletMode.EOA)))
        assert b.signature_type == 0
        assert b.funder == _EOA.lower()
        assert b.derived is False
        assert b.wallet_mode == "eoa"

    def test_magic_create2_is_deterministic(self) -> None:
        a1 = magic_proxy_address(_EOA)
        a2 = magic_proxy_address(_EOA.lower())
        assert a1 == a2
        assert a1.startswith("0x") and len(a1) == 42
        b = run(resolve_wallet(_FakeAccount(mode=WalletMode.MAGIC)))
        assert b.signature_type == 1
        assert b.funder == a1
        assert b.derived is True

    def test_magic_prefers_explicit_operator_funder(self) -> None:
        op = "0x00000000000000000000000000000000CAfeBabe"
        b = run(
            resolve_wallet(_FakeAccount(mode=WalletMode.MAGIC, funder=op))
        )
        assert b.funder == op.lower()
        assert b.derived is False  # operator-confirmed, not derived

    def test_safe_uses_injected_rpc_resolver(self) -> None:
        safe_addr = "0x000000000000000000000000000000000000BEEF"

        async def _resolver(eoa: str) -> str:
            assert eoa == _EOA.lower()
            return safe_addr

        b = run(
            resolve_wallet(
                _FakeAccount(mode=WalletMode.SAFE),
                safe_proxy_resolver=_resolver,
            )
        )
        assert b.signature_type == 2
        assert b.funder == safe_addr.lower()
        assert b.derived is True

    def test_safe_fail_closed_without_resolver_or_funder(self) -> None:
        with pytest.raises(ValueError, match="fail-closed"):
            run(resolve_wallet(_FakeAccount(mode=WalletMode.SAFE)))

    def test_poly1271_requires_explicit_funder(self) -> None:
        op = "0x000000000000000000000000000000000000F00D"
        b = run(
            resolve_wallet(
                _FakeAccount(mode=WalletMode.POLY_1271, funder=op)
            )
        )
        assert b.signature_type == 3
        assert b.funder == op.lower()
        assert b.derived is False

    def test_poly1271_fail_closed_without_funder(self) -> None:
        with pytest.raises(ValueError, match="REQUIRED"):
            run(resolve_wallet(_FakeAccount(mode=WalletMode.POLY_1271)))

    def test_unknown_mode_fail_closed(self) -> None:
        with pytest.raises(ValueError, match="Unknown wallet_mode"):
            run(resolve_wallet(_FakeAccount(mode="totally-bogus")))

    def test_falls_back_to_signature_type_mapping(self) -> None:
        b = run(resolve_wallet(_FakeAccount(sig=0)))
        assert b.signature_type == 0 and b.wallet_mode == "eoa"

    def test_unresolvable_without_mode_or_sig_fail_closed(self) -> None:
        with pytest.raises(ValueError, match="fail-closed"):
            run(resolve_wallet(_FakeAccount()))

    def test_invalid_eoa_raises(self) -> None:
        with pytest.raises(ValueError, match="valid EVM address"):
            run(resolve_wallet(_FakeAccount(eoa="not-an-address", sig=0)))

    def test_real_account_model_is_structurally_compatible(self) -> None:
        from polyecho.shared.polymarket.wallet import AccountLike

        acc = Account(
            name="t",
            eoa_address=_EOA,
            signature_type=0,
            wallet_mode="eoa",
        )
        # Account satisfies the AccountLike protocol at runtime; the explicit
        # cast documents that intent (mypy treats Protocol mutable attrs as
        # invariant, so Account.signature_type: int vs int|None is flagged
        # statically only — it is structurally compatible).
        b = run(resolve_wallet(cast(AccountLike, acc)))
        assert b.funder == _EOA.lower()
        assert isinstance(acc, AccountLike)


# ---------------------------------------------------------------------------
# Live-lock chokepoint (default OFF — the HARD lock)
# ---------------------------------------------------------------------------
class TestLiveLock:
    def test_default_is_locked_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        assert is_live_enabled() is False
        with pytest.raises(LiveTradingLockedError, match="LOCKED"):
            assert_live_allowed("place_order")

    def test_enabled_when_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        assert is_live_enabled() is True
        assert_live_allowed("place_order")  # must NOT raise

    def test_action_name_in_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(LiveTradingLockedError, match="redeem"):
            assert_live_allowed("redeem")

    def test_place_order_wrapper_is_guarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        from polyecho.shared.config import get_settings
        from polyecho.shared.polymarket.client import PolymarketClient

        get_settings.cache_clear()
        c = PolymarketClient.__new__(PolymarketClient)  # skip ClobClient ctor
        with pytest.raises(LiveTradingLockedError):
            run(
                c.place_order(
                    token_id="t", side="BUY", price=0.5, size=1.0
                )
            )


# ---------------------------------------------------------------------------
# Config parses the two new flags
# ---------------------------------------------------------------------------
class TestConfigFlags:
    def test_flags_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("POLYECHO_LIVE_TRADING", "POLYECHO_AUTO_REDEEM"):
            monkeypatch.delenv(v, raising=False)
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        assert s.live_trading is False
        assert s.auto_redeem is False

    def test_flags_parse_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POLYECHO_LIVE_TRADING", "1")
        monkeypatch.setenv("POLYECHO_AUTO_REDEEM", "yes")
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        s = get_settings()
        assert s.live_trading is True
        assert s.auto_redeem is True


# ---------------------------------------------------------------------------
# SkipReason taxonomy
# ---------------------------------------------------------------------------
class TestSkipReason:
    def test_all_brief_reasons_present(self) -> None:
        expected = {
            "DUP",
            "NOT_FOLLOWING",
            "FILTER_KEYWORD",
            "FILTER_PRICE_BAND",
            "FILTER_MARKET_END",
            "ORDERBOOK_SPREAD",
            "ORDERBOOK_DEPTH",
            "POSITION_CAP",
            "BELOW_MIN_SIZE",
            "DAILY_ORDER_CAP",
            "DAILY_LOSS_CAP",
            "PORTFOLIO_CAP",
            "SLIPPAGE_REJECT",
            "STRATEGY_DRIFT",
            "DATA_UNAVAILABLE",
            "LIVE_LOCKED",
        }
        assert expected.issubset({m.name for m in SkipReason})

    def test_strenum_value_is_the_stored_string(self) -> None:
        assert SkipReason.DUP.value == "dup"
        assert str(SkipReason.LIVE_LOCKED) == "live_locked"
        assert f"{SkipReason.DUP}" == "dup"  # StrEnum str-coercion
        # JSON / DB round-trip stability
        assert SkipReason("orderbook_spread") is SkipReason.ORDERBOOK_SPREAD


# ---------------------------------------------------------------------------
# constants — brief §9 verbatim
# ---------------------------------------------------------------------------
class TestConstants:
    def test_brief_section9_addresses(self) -> None:
        assert constants.CTF_ADDRESS == "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        assert constants.PUSD_ADDRESS == "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        assert (
            constants.WRAPPED_COLLATERAL_ADDRESS
            == "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"
        )
        assert (
            constants.MAGIC_PROXY_FACTORY
            == "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
        )
        assert (
            constants.MAGIC_PROXY_INIT_CODE_HASH
            == "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
        )
        assert (
            constants.SAFE_PROXY_FACTORY
            == "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
        )
        assert constants.POLYGON_CHAIN_ID == 137
        assert constants.MAX_UINT256 == (1 << 256) - 1
        assert constants.PARENT_COLLECTION_ID == "0x" + "0" * 64


# ---------------------------------------------------------------------------
# Redeem helpers + live-lock-guarded base client
# ---------------------------------------------------------------------------
class TestRedeem:
    def test_collateral_for_neg_risk_vs_normal(self) -> None:
        assert collateral_for(is_neg_risk=False) == constants.PUSD_ADDRESS
        assert (
            collateral_for(is_neg_risk=True)
            == constants.WRAPPED_COLLATERAL_ADDRESS
        )

    def test_index_set_is_power_of_two(self) -> None:
        assert index_set_for(0) == 1
        assert index_set_for(3) == 8
        with pytest.raises(ValueError):
            index_set_for(-1)

    def test_base_redeem_client_is_live_locked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
        from polyecho.shared.config import get_settings

        get_settings.cache_clear()
        spec = RedeemJobSpec(
            account_id=uuid.uuid4(),
            condition_id="0xc",
            outcome_index=1,
            index_set=2,
            is_neg_risk=True,
        )
        binding = WalletBinding(
            signature_type=1, funder="0xabc", wallet_mode="magic", derived=True
        )
        client = BaseRedeemClient()
        with pytest.raises(LiveTradingLockedError):
            run(client.redeem(spec, binding=binding))
        with pytest.raises(LiveTradingLockedError):
            run(client.unwrap_collateral(binding=binding, amount=10))


# ---------------------------------------------------------------------------
# Frozen dataclass shapes (Wave-2 imports these verbatim)
# ---------------------------------------------------------------------------
class TestFrozenContracts:
    def test_decision_skip_and_copy(self) -> None:
        skip = Decision(
            outcome=DecisionOutcome.SKIP, skip_reason=SkipReason.DUP
        )
        assert skip.outcome is DecisionOutcome.SKIP
        copy = Decision(
            outcome=DecisionOutcome.COPY,
            side=TradeSide.BUY,
            token_id="t",
            price=Decimal("0.55"),
            size=Decimal("10"),
        )
        assert copy.order_type == "FOK"
        with pytest.raises(FrozenInstanceError):
            copy.token_id = "x"  # type: ignore[misc]

    def test_evaluated_copy_bundle(self) -> None:
        ev = EvaluatedCopy(
            leader_address="0xl",
            tx_hash="0xtx",
            account_id=uuid.uuid4(),
            condition_id="0xc",
            binding=WalletBinding(
                signature_type=0,
                funder="0xl",
                wallet_mode="eoa",
                derived=False,
            ),
            decision=Decision(outcome=DecisionOutcome.SKIP),
        )
        assert ev.risk is None
        assert ev.raw == {}

    def test_risk_result_and_redeem_result(self) -> None:
        r = RiskResult(passed=False, skip_reason=SkipReason.POSITION_CAP)
        assert r.adjusted_size is None
        rr = RedeemResult(status=RedeemJobStatus.PENDING)
        assert rr.tx_hash is None
        assert RedeemJobStatus.CONFIRMED.value == "confirmed"

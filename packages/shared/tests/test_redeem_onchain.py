"""
packages/shared/tests/test_redeem_onchain.py
============================================
Wave C-redeem — concrete on-chain redeem client (brief §9).

Covers the new :class:`Web3RedeemClient`:

* Live-lock chokepoint preserved: lock OFF ⇒ ``redeem`` /
  ``unwrap_collateral`` raise ``LiveTradingLockedError`` *before* any
  ``Web3Backend`` call (zero on-chain calls — money-path safety).
* Lock ON + fake web3 + EOA binding: the correct ``redeemPositions`` call
  is built+signed+sent — collateral asserted **pUSD vs WrappedCollateral
  by ``is_neg_risk``** (the brief's highest-risk detail).
* NegRisk ⇒ WCOL balance poll + ``unwrap(funder, amount)``.
* Receipt ``status != 1`` ⇒ ``FAILED`` (no false confirm).
* Idempotent max-allowance approve (no tx if already approved).
* MAGIC / SAFE / POLY_1271 ⇒ ``RelayerNotConfiguredError`` (clear TODO,
  never a wrong tx).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from polyecho.shared import constants
from polyecho.shared.copytrading import (
    LiveTradingLockedError,
    RedeemJobSpec,
    RedeemJobStatus,
    WalletBinding,
)
from polyecho.shared.polymarket.redeem import (
    CTF_REDEEM_ABI,
    RelayerNotConfiguredError,
    Web3RedeemClient,
    collateral_for,
    index_set_for,
)

from .conftest import run


class _FakeWeb3:
    """Records every send_signed / call / wait_receipt invocation."""

    def __init__(self, *, receipt_status: int = 1, wcol_balance: int = 0) -> None:
        self.sent: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self._receipt_status = receipt_status
        self._wcol_balance = wcol_balance

    async def send_signed(self, **kw: Any) -> str:
        self.sent.append(kw)
        return "0x" + "ab" * 32

    async def wait_receipt(self, tx_hash: str) -> dict[str, Any]:
        return {"status": self._receipt_status, "blockNumber": 42}

    async def call(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if kw["fn_name"] == "balanceOf":
            return self._wcol_balance
        if kw["fn_name"] == "allowance":
            return constants.MAX_UINT256  # already approved
        return 0


async def _key(_binding: WalletBinding) -> str:
    return "0x" + "11" * 32


_EOA = WalletBinding(
    signature_type=0,
    funder="0x000000000000000000000000000000000000dead",
    wallet_mode="eoa",
    derived=False,
)
_MAGIC = WalletBinding(
    signature_type=1, funder="0xabc", wallet_mode="magic", derived=True
)


def _spec(*, is_neg_risk: bool) -> RedeemJobSpec:
    return RedeemJobSpec(
        account_id=uuid.uuid4(),
        condition_id="0xcondition",
        outcome_index=1,
        index_set=index_set_for(1),
        is_neg_risk=is_neg_risk,
    )


def _client(fake: _FakeWeb3) -> Web3RedeemClient:
    return Web3RedeemClient(web3=fake, private_key_resolver=_key)


# ---------------------------------------------------------------------------
# Live-lock chokepoint — OFF ⇒ nothing on-chain (default behaviour)
# ---------------------------------------------------------------------------
def test_lock_off_redeem_raises_before_any_web3_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POLYECHO_LIVE_TRADING", raising=False)
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()
    fake = _FakeWeb3()
    c = _client(fake)
    with pytest.raises(LiveTradingLockedError):
        run(c.redeem(_spec(is_neg_risk=False), binding=_EOA))
    with pytest.raises(LiveTradingLockedError):
        run(c.unwrap_collateral(binding=_EOA, amount=5))
    # PROOF: not a single on-chain build/sign/send happened.
    assert fake.sent == []
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Lock ON — EOA concrete path, collateral branch (highest-risk detail)
# ---------------------------------------------------------------------------
def _enable_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYECHO_LIVE_TRADING", "true")
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()


def test_eoa_redeem_normal_market_uses_pusd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(receipt_status=1)
    res = run(_client(fake).redeem(_spec(is_neg_risk=False), binding=_EOA))
    assert res.status is RedeemJobStatus.CONFIRMED
    assert len(fake.sent) == 1
    tx = fake.sent[0]
    assert tx["fn_name"] == "redeemPositions"
    assert tx["contract_address"] == constants.CTF_ADDRESS
    assert tx["abi"] is CTF_REDEEM_ABI
    collateral, parent, cond, index_sets = tx["args"]
    assert collateral == constants.PUSD_ADDRESS  # normal ⇒ pUSD
    assert parent == constants.PARENT_COLLECTION_ID
    assert cond == "0xcondition"
    assert index_sets == [index_set_for(1)]
    assert tx["chain_id"] == constants.POLYGON_CHAIN_ID


def test_eoa_redeem_neg_risk_uses_wrapped_collateral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(receipt_status=1)
    res = run(_client(fake).redeem(_spec(is_neg_risk=True), binding=_EOA))
    assert res.status is RedeemJobStatus.CONFIRMED
    collateral = fake.sent[0]["args"][0]
    assert collateral == constants.WRAPPED_COLLATERAL_ADDRESS
    assert collateral == collateral_for(is_neg_risk=True)


def test_eoa_redeem_failed_receipt_is_failed_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(receipt_status=0)
    res = run(_client(fake).redeem(_spec(is_neg_risk=False), binding=_EOA))
    assert res.status is RedeemJobStatus.FAILED
    assert res.tx_hash is not None


def test_eoa_neg_risk_unwrap_after_balance_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3(receipt_status=1, wcol_balance=12_345)
    c = _client(fake)
    amount = run(c.poll_wcol_balance(binding=_EOA))
    assert amount == 12_345
    res = run(c.unwrap_collateral(binding=_EOA, amount=amount))
    assert res.status is RedeemJobStatus.CONFIRMED
    unwrap_tx = fake.sent[-1]
    assert unwrap_tx["fn_name"] == "unwrap"
    assert unwrap_tx["contract_address"] == constants.WRAPPED_COLLATERAL_ADDRESS
    assert unwrap_tx["args"] == (_EOA.funder, 12_345)


def test_unwrap_zero_amount_is_noop_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()
    res = run(_client(fake).unwrap_collateral(binding=_EOA, amount=0))
    assert res.status is RedeemJobStatus.CONFIRMED
    assert fake.sent == []  # nothing on-chain when there's nothing to unwrap


def test_idempotent_max_approve_skips_when_already_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()  # allowance() returns MAX_UINT256
    tx = run(
        _client(fake).ensure_collateral_approval(
            binding=_EOA,
            collateral=constants.PUSD_ADDRESS,
            spender=constants.CTF_ADDRESS,
        )
    )
    assert tx is None  # idempotent: already approved ⇒ no tx
    assert fake.sent == []


# ---------------------------------------------------------------------------
# MAGIC / POLY_1271 — sig 1 + sig 3 stay fail-closed even after the
# Phase-4 P3 work (the official Builder-Relayer SDK exposes no PROXY /
# EIP-191 or 1271 surface — verified 2026-05-20).  SAFE (sig 2) is now
# concrete via :mod:`polyecho.shared.polymarket.relayer` and is covered
# in :mod:`packages.shared.tests.test_polymarket_relayer`.
# ---------------------------------------------------------------------------
def test_magic_redeem_raises_relayer_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()
    # No relayer injected (default) ⇒ chokepoint fires for sig 1 MAGIC
    # with the new error message (no more legacy "TODO(verify)" — the
    # SDK is verified to lack a MAGIC surface).
    with pytest.raises(RelayerNotConfiguredError, match="signature_type=1"):
        run(_client(fake).redeem(_spec(is_neg_risk=False), binding=_MAGIC))
    assert fake.sent == []  # never a wrong tx for an unsupported sig


def test_magic_unwrap_raises_relayer_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_live(monkeypatch)
    fake = _FakeWeb3()
    with pytest.raises(RelayerNotConfiguredError):
        run(_client(fake).unwrap_collateral(binding=_MAGIC, amount=10))
    assert fake.sent == []

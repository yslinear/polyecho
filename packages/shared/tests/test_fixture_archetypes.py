"""
tests/test_fixture_archetypes.py
================================
Invariant coverage for :class:`FixtureDataSource` beyond the per-method
type checks in ``test_datasource.py``.

These assert *behavioural archetype invariants* the discovery package
(Wave 2) will rely on, so a fixture edit that silently breaks the
narrative is caught here:

* Leaderboard rows are rank-ordered and every wallet is lowercase ``0x``.
* Every leaderboard wallet that has an activity fixture is internally
  consistent (``record.wallet`` matches the requested wallet).
* Archetype frequency ordering: scalper ≫ day-trader ≫ swing ≫ position
  (by 30-day TRADE count) — the core "replicability" signal.
* ``get_user_activity`` time-window + type filters compose correctly and
  are *order-independent* (start∧end∧type == manual intersection).
* ``raw`` dicts are copies, present, and carry the original keys.
* ``get_positions`` size-threshold is a half-open lower bound.
* ``get_market_top_holders`` returns ≤ limit, descending-ish holders, and
  empty for unknown markets (no exception).
* The fixture source structurally satisfies the ``DataSource`` Protocol
  and needs no async-context-manager.

All deterministic: a fixed "now" is used for any windowed query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polyecho.shared.datasource import (
    ActivityRecord,
    DataSource,
    FixtureDataSource,
    PositionRecord,
)

from .conftest import run

# Archetype wallets present in the bundled fixtures.
SWING = "0xswing1000000000000000000000000000000001"
SCALPER = "0xscalper200000000000000000000000000000002"
DAYTRADER = "0xdaytrader300000000000000000000000000000003"
POSITION = "0xposition400000000000000000000000000000004"
DRIFTED = "0xdrifted500000000000000000000000000000005"
INSIDER = "0xinsider600000000000000000000000000000006"

NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


def _ds() -> FixtureDataSource:
    return FixtureDataSource()


# ---------------------------------------------------------------------------
# Leaderboard invariants
# ---------------------------------------------------------------------------


class TestLeaderboardInvariants:
    def test_rows_rank_ordered(self) -> None:
        entries = run(_ds().get_leaderboard(limit=200))
        ranks = [e.rank for e in entries]
        assert ranks == sorted(ranks)
        assert ranks[0] == 1

    def test_all_wallets_lowercase_hex(self) -> None:
        for e in run(_ds().get_leaderboard(limit=200)):
            assert e.wallet.startswith("0x")
            assert e.wallet == e.wallet.lower()

    def test_archetype_wallets_present(self) -> None:
        wallets = {e.wallet for e in run(_ds().get_leaderboard(limit=200))}
        for w in (SWING, SCALPER, DAYTRADER, POSITION, DRIFTED, INSIDER):
            assert w in wallets, f"{w} missing from leaderboard fixture"

    def test_limit_zero_returns_empty(self) -> None:
        assert run(_ds().get_leaderboard(limit=0)) == []

    def test_limit_is_stable_prefix(self) -> None:
        full = run(_ds().get_leaderboard(limit=200))
        head = run(_ds().get_leaderboard(limit=4))
        assert [e.wallet for e in head] == [e.wallet for e in full[:4]]

    def test_leaderboard_to_activity_roundtrip_for_archetypes(self) -> None:
        """Regression: rank-3 (daytrader) leaderboard wallet was 39 hex
        chars while its ``activity/``/``positions/`` fixture filenames were
        41 chars, so ``get_user_activity(<leaderboard wallet>)`` silently
        returned ``[]`` — breaking the discovery "leaderboard → fetch that
        wallet's history" path. Every archetype on the leaderboard must
        resolve to its own non-empty activity fixture."""
        ds = _ds()
        by_wallet = {e.wallet: e for e in run(ds.get_leaderboard(limit=200))}
        for w in (SWING, SCALPER, DAYTRADER, POSITION, DRIFTED, INSIDER):
            assert w in by_wallet, f"{w} not on leaderboard"
            acts = run(ds.get_user_activity(w, activity_type=None))
            assert acts, f"leaderboard wallet {w} has no resolvable activity"
            assert all(r.wallet == w for r in acts)


# ---------------------------------------------------------------------------
# Archetype frequency ordering (replicability signal)
# ---------------------------------------------------------------------------


class TestArchetypeFrequencyOrdering:
    def _trade_count_30d(self, wallet: str) -> int:
        recs = run(
            _ds().get_user_activity(
                wallet,
                start=NOW - timedelta(days=30),
                end=NOW,
                activity_type="TRADE",
            )
        )
        return len(recs)

    def test_scalper_trades_more_than_daytrader_more_than_swing(self) -> None:
        scalper = self._trade_count_30d(SCALPER)
        daytrader = self._trade_count_30d(DAYTRADER)
        swing = self._trade_count_30d(SWING)
        position = self._trade_count_30d(POSITION)
        assert scalper > daytrader > swing > position, (
            f"archetype ordering violated: scalper={scalper} "
            f"daytrader={daytrader} swing={swing} position={position}"
        )

    def test_drifted_more_active_in_30d_than_7d(self) -> None:
        w7 = run(
            _ds().get_user_activity(
                DRIFTED, start=NOW - timedelta(days=7), end=NOW, activity_type=None
            )
        )
        w30 = run(
            _ds().get_user_activity(
                DRIFTED, start=NOW - timedelta(days=30), end=NOW, activity_type=None
            )
        )
        assert len(w30) > len(w7)


# ---------------------------------------------------------------------------
# Activity filter composition
# ---------------------------------------------------------------------------


class TestActivityFilterComposition:
    def test_combined_filters_equal_manual_intersection(self) -> None:
        ds = _ds()
        all_recs = run(ds.get_user_activity(INSIDER, activity_type=None))
        assert all_recs, "fixture precondition: insider has activity"
        mid = sorted(r.timestamp for r in all_recs)[len(all_recs) // 2]

        combined = run(
            ds.get_user_activity(
                INSIDER, start=mid, activity_type="TRADE"
            )
        )
        manual = [
            r for r in all_recs if r.timestamp >= mid and r.activity_type == "TRADE"
        ]
        assert sorted(r.timestamp for r in combined) == sorted(
            r.timestamp for r in manual
        )
        assert len(combined) == len(manual)

    def test_type_partition_is_exhaustive(self) -> None:
        ds = _ds()
        trades = run(ds.get_user_activity(INSIDER, activity_type="TRADE"))
        redeems = run(ds.get_user_activity(INSIDER, activity_type="REDEEM"))
        every = run(ds.get_user_activity(INSIDER, activity_type=None))
        assert all(r.activity_type == "TRADE" for r in trades)
        assert all(r.activity_type == "REDEEM" for r in redeems)
        assert len(trades) + len(redeems) == len(every)

    def test_unknown_wallet_returns_empty_not_error(self) -> None:
        assert run(_ds().get_user_activity("0xnope")) == []

    def test_limit_truncates(self) -> None:
        recs = run(
            _ds().get_user_activity(SCALPER, activity_type=None, limit=11)
        )
        assert len(recs) == 11

    def test_records_are_utc_aware(self) -> None:
        for r in run(_ds().get_user_activity(SWING))[:20]:
            assert isinstance(r, ActivityRecord)
            assert r.timestamp.tzinfo == UTC

    def test_raw_present_and_copied(self) -> None:
        recs = run(_ds().get_user_activity(SWING))
        assert recs
        r = recs[0]
        assert isinstance(r.raw, dict)
        assert r.raw  # non-empty original payload
        # Mutating the copy must not affect a re-fetch.
        r.raw["__mutated__"] = True
        again = run(_ds().get_user_activity(SWING))[0]
        assert "__mutated__" not in again.raw


# ---------------------------------------------------------------------------
# Positions & top holders
# ---------------------------------------------------------------------------


class TestPositionsAndHolders:
    def test_size_threshold_is_lower_bound(self) -> None:
        ds = _ds()
        every = run(ds.get_positions(POSITION, size_threshold=0.0))
        assert every
        cutoff = min(p.size for p in every) + 0.0001
        filtered = run(ds.get_positions(POSITION, size_threshold=cutoff))
        assert all(p.size >= cutoff for p in filtered)
        assert len(filtered) <= len(every)

    def test_positions_wallet_lowercased(self) -> None:
        for p in run(_ds().get_positions(SWING)):
            assert isinstance(p, PositionRecord)
            assert p.wallet == p.wallet.lower()

    def test_unknown_wallet_positions_empty(self) -> None:
        assert run(_ds().get_positions("0xunknownwallet")) == []

    def test_top_holders_respects_limit(self) -> None:
        cond = "0xcond_politics_potus_2028"
        holders = run(_ds().get_market_top_holders(cond, limit=3))
        assert len(holders) <= 3
        assert all(isinstance(h, PositionRecord) for h in holders)
        assert all(h.condition_id for h in holders)

    def test_top_holders_unknown_condition_empty(self) -> None:
        assert run(_ds().get_market_top_holders("0xno_such_market")) == []

    def test_top_holders_all_known_market_files_nonempty(self) -> None:
        """Each shipped ``market_holders`` fixture must yield holders so
        discovery's "top N holders" source is never silently empty."""
        conds = [
            "0xcond_btc_100k_may2026",
            "0xcond_eth_merge_v2",
            "0xcond_fed_june_2026",
            "0xcond_fifa_brazil_2026",
            "0xcond_nvidia_earnings_q2",
            "0xcond_politics_potus_2028",
            "0xcond_recession_2026",
            "0xcond_trump_indictment",
        ]
        ds = _ds()
        for c in conds:
            holders = run(ds.get_market_top_holders(c))
            assert len(holders) >= 1, f"{c} produced no holders"
            assert all(h.wallet.startswith("0x") for h in holders)


# ---------------------------------------------------------------------------
# Markets & protocol conformance
# ---------------------------------------------------------------------------


class TestMarketsAndProtocol:
    def test_markets_sorted_descending_and_filtered(self) -> None:
        ds = _ds()
        markets = run(ds.get_markets_by_volume(min_volume_24h=0.0))
        vols = [m.volume_24h for m in markets]
        assert vols == sorted(vols, reverse=True)
        high = run(ds.get_markets_by_volume(min_volume_24h=1_000_000.0))
        assert all(m.volume_24h >= 1_000_000.0 for m in high)
        assert len(high) <= len(markets)

    def test_markets_limit_and_utc_end_dates(self) -> None:
        ds = _ds()
        two = run(ds.get_markets_by_volume(limit=2, min_volume_24h=0.0))
        assert len(two) <= 2
        for m in run(ds.get_markets_by_volume(min_volume_24h=0.0)):
            if m.end_date is not None:
                assert m.end_date.tzinfo is not None

    def test_fixture_satisfies_protocol_without_context_manager(self) -> None:
        ds = FixtureDataSource()
        assert isinstance(ds, DataSource)
        # No __aenter__ needed — methods are directly awaitable.
        assert run(ds.get_leaderboard(limit=1))

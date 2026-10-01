"""
Unit Tests for PnL Accounting Invariants and Edge Cases

Tests financial accounting invariants:
- Total PnL = Realized PnL + Unrealized PnL
- Deferred entry fee preservation across partial trims and position reversals
- InventoryManager fill & metric exception handling
- Concurrent fill arrivals during REST reconciliation
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from data.pnl_tracker import PnLTracker, MarketPnL
from data.inventory_manager import InventoryManager


class TestInventoryManagerPnLIntegration:
    """Verifies InventoryManager correctly integrates PnLTracker."""

    def test_inventory_manager_fill_updates_pnl(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        ticker = "KXTEST-26SEP-IM"

        # Buy fill
        fill_buy = {
            "market_ticker": ticker,
            "action": "buy",
            "side": "yes",
            "count": 4,
            "price": 30,
            "fee_cents": 2.0
        }
        im._handle_fill(fill_buy)
        assert im.get_position(ticker) == 4
        # Opening fill: entry fee is attached to inventory lots; realized PnL remains 0.0 until closed
        assert im.get_realized_pnl(ticker) == 0.0

        # Mid price update
        im.update_orderbook_mid(ticker, 35.0)
        # Unrealized: (35 - 30) * 4 - 2.0 (entry fee) = 18.0c
        assert im.get_unrealized_pnl(ticker) == 18.0

        # Sell fill (closing half)
        fill_sell = {
            "market_ticker": ticker,
            "action": "sell",
            "side": "yes",
            "count": 2,
            "price": 40,
            "fee_cents": 1.0
        }
        im._handle_fill(fill_sell)
        assert im.get_position(ticker) == 2
        # Realized for 2 closed contracts:
        # Gross gain = (40 - 30) * 2 = 20.0c
        # Closing fee = 1.0c, Entry fee allocated to closed 2 contracts = (2.0 / 4) * 2 = 1.0c
        # Net realized = 20.0 - 1.0 - 1.0 = 18.0c
        assert im.get_realized_pnl(ticker) == 18.0

        summary = im.get_pnl_summary(ticker)
        assert summary["ticker"] == ticker
        assert summary["realized_pnl_cents"] == 18.0
        # 2 remaining open contracts marked to 35c: (35 - 30) * 2 - 1.0 entry fee = 9.0c
        assert summary["unrealized_pnl_cents"] == 9.0
        assert summary["total_pnl_cents"] == 27.0
        assert summary["net_inventory"] == 2


class TestPnLCoverageEdgeCases:
    """Covers defensive edge cases and fallback branches."""

    def test_record_fill_zero_or_negative_count(self):
        tracker = PnLTracker()
        res_zero = tracker.record_fill("TICKER", "buy", "yes", count=0, price_cents=50)
        assert res_zero == {}
        res_neg = tracker.record_fill("TICKER", "buy", "yes", count=-5, price_cents=50)
        assert res_neg == {}

    def test_inventory_manager_metric_exceptions(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        ticker = "KXTEST-PROM-EX"

        # 1. Prometheus fill metric exception
        with patch("data.inventory_manager.BOT_INVENTORY_NET_POSITION.labels", side_effect=Exception("Prometheus unavailable")):
            fill = {
                "market_ticker": ticker,
                "action": "buy",
                "side": "yes",
                "count": 5,
                "price": 40,
                "fee_cents": 1.0
            }
            # Should succeed and log debug without raising
            im._handle_fill(fill)
            assert im.get_position(ticker) == 5

        # 2. Prometheus unrealized metric exception
        with patch("data.inventory_manager.KALSHI_UNREALIZED_PNL_CENTS.labels", side_effect=Exception("Prometheus gauge error")):
            unrealized = im.update_orderbook_mid(ticker, 45.0)
            assert unrealized == 24.0  # (45 - 40) * 5 - 1.0 (entry fee)

    def test_inventory_manager_scratch_outcome_counter(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        ticker = "KXTEST-SCRATCH"

        # Buy 2 @ 50c
        im._handle_fill({"market_ticker": ticker, "action": "buy", "side": "yes", "count": 2, "price": 50})
        # Sell 2 @ 50c (zero delta -> scratch)
        with patch("data.inventory_manager.KALSHI_ROUND_TRIPS_TOTAL.labels") as mock_labels:
            mock_counter = MagicMock()
            mock_labels.return_value = mock_counter
            im._handle_fill({"market_ticker": ticker, "action": "sell", "side": "yes", "count": 2, "price": 50})
            mock_labels.assert_called_with(ticker=ticker, outcome="scratch")
            mock_counter.inc.assert_called_once()

    def test_seed_initial_inventory_branches(self):
        tracker = PnLTracker()
        ticker = "KXTEST-SEED"

        # 1. Zero position does nothing
        tracker.seed_initial_inventory(ticker, 0)
        assert tracker.get_open_inventory(ticker) == 0

        # 2. Long position with explicit cost basis
        tracker.seed_initial_inventory(ticker, 10, cost_basis_cents=42.5)
        assert tracker.get_open_inventory(ticker) == 10
        market = tracker.get_or_create_market(ticker)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].price_cents == 42.5
        assert market.open_lots[0].action == "buy"

        # 3. Already seeded / open lots exist -> does not overwrite
        tracker.seed_initial_inventory(ticker, 20, cost_basis_cents=99.0)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].price_cents == 42.5

        # 4. Short position with known mid price but missing cost basis -> strictly uncosted (never treat mid as entry basis)
        ticker_short = "KXTEST-SEED-SHORT"
        short_market = tracker.get_or_create_market(ticker_short)
        short_market.last_mid_price = 48.0
        tracker.seed_initial_inventory(ticker_short, -5, cost_basis_cents=None)
        assert tracker.get_open_inventory(ticker_short) == -5
        assert len(short_market.open_lots) == 1
        assert short_market.open_lots[0].price_cents is None
        assert short_market.open_lots[0].is_uncosted is True
        assert short_market.open_lots[0].action == "sell"

        # 5. Position with unknown basis and unknown mid -> marks lot as is_uncosted=True, price_cents=None
        ticker_uncosted = "KXTEST-SEED-UNCOSTED"
        tracker.seed_initial_inventory(ticker_uncosted, 4, cost_basis_cents=None)
        uncosted_market = tracker.get_or_create_market(ticker_uncosted)
        assert len(uncosted_market.open_lots) == 1
        assert uncosted_market.open_lots[0].is_uncosted is True
        assert uncosted_market.open_lots[0].price_cents is None

        # 6. Closing uncosted lot does not fabricate realized PnL or classify trade outcome
        res = tracker.record_fill(ticker_uncosted, action="sell", side="yes", count=4, price_cents=60.0)
        assert res["matched_contracts"] == 4
        assert res["matched_outcomes"] == []
        assert tracker.get_realized_pnl(ticker_uncosted) == 0.0
        assert uncosted_market.winning_trades_count == 0
        assert uncosted_market.losing_trades_count == 0

        # 7. Closing uncosted lot with a fee deducts the closing fee from realized PnL
        ticker_uncosted_fee = "KXTEST-SEED-UNCOSTED-FEE"
        tracker.seed_initial_inventory(ticker_uncosted_fee, 2, cost_basis_cents=None)
        res_fee = tracker.record_fill(ticker_uncosted_fee, action="sell", side="yes", count=2, price_cents=50.0, fee_cents=3.0)
        assert res_fee["matched_contracts"] == 2
        assert res_fee["matched_outcomes"] == []
        assert tracker.get_realized_pnl(ticker_uncosted_fee) == -3.0  # Closing fee accounted for exactly
        assert tracker.get_total_fees(ticker_uncosted_fee) == 3.0

    def test_hydrate_positions_seeds_inventory(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)

        mock_payload = {
            "market_positions": [
                {"ticker": "KXTEST-HYD1", "position_fp": "10.0", "market_exposure": 450.0},
                {"ticker": "KXTEST-HYD2", "position": -5, "market_exposure": "invalid"},
                {"ticker": "KXTEST-ZERO", "position": 0}
            ]
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload

        with patch("data.inventory_manager.requests.get", return_value=mock_resp), \
             patch("data.inventory_manager.get_auth_headers", return_value={}):
            im._hydrate_positions(is_startup=True)

        assert im.get_position("KXTEST-HYD1") == 10
        assert im.get_position("KXTEST-HYD2") == -5
        assert "KXTEST-ZERO" not in im.positions

        # Verify PnL tracker has lots seeded
        assert im.pnl_tracker.get_open_inventory("KXTEST-HYD1") == 10
        assert im.pnl_tracker.get_open_inventory("KXTEST-HYD2") == -5
        # KXTEST-HYD2 has invalid exposure -> uncosted lot
        lot2 = im.pnl_tracker.get_or_create_market("KXTEST-HYD2").open_lots[0]
        assert lot2.is_uncosted is True
        assert lot2.price_cents is None

        # Verify periodic reconciliation (is_startup=False) reconciles inventory drift as uncosted lots
        im.pnl_tracker.markets["KXTEST-RECON"] = MarketPnL(ticker="KXTEST-RECON")
        mock_payload_recon = {
            "market_positions": [{"ticker": "KXTEST-RECON", "position": 8}]
        }
        mock_resp.json.return_value = mock_payload_recon
        with patch("data.inventory_manager.requests.get", return_value=mock_resp), \
             patch("data.inventory_manager.get_auth_headers", return_value={}):
            im._hydrate_positions(is_startup=False)

        assert im.get_position("KXTEST-RECON") == 8
        # PnL tracker is reconciled to REST position with an uncosted delta lot
        assert im.pnl_tracker.get_open_inventory("KXTEST-RECON") == 8
        recon_lot = im.pnl_tracker.get_or_create_market("KXTEST-RECON").open_lots[0]
        assert recon_lot.is_uncosted is True
        assert recon_lot.price_cents is None

        # Subsequent periodic reconciliation showing flat position reconciles tracker to 0
        mock_resp.json.return_value = {"market_positions": []}
        with patch("data.inventory_manager.requests.get", return_value=mock_resp), \
             patch("data.inventory_manager.get_auth_headers", return_value={}):
            im._hydrate_positions(is_startup=False)
        assert im.get_position("KXTEST-RECON") == 0
        assert im.pnl_tracker.get_open_inventory("KXTEST-RECON") == 0

    def test_handle_fill_deducts_fees_from_balance(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000.0
        ticker = "KXTEST-FEE-BAL"

        # Buy 2 @ 40c with fee 1.8c -> total cost = 2*40 + 1.8 = 81.8c -> balance = 9918.2c
        im._handle_fill({
            "market_ticker": ticker,
            "action": "buy",
            "side": "yes",
            "count": 2,
            "price": 40,
            "fee_cents": 1.8
        })
        assert im.balance_cents == 9918.2

        # Sell 2 @ 60c with fee 1.2c -> proceeds = 2*60 - 1.2 = 118.8c -> balance = 9918.2 + 118.8 = 10037.0c
        im._handle_fill({
            "market_ticker": ticker,
            "action": "sell",
            "side": "yes",
            "count": 2,
            "price": 60,
            "fee_cents": 1.2
        })
        assert im.balance_cents == 10037.0
        # Balance delta strictly reconciles with Strategy Realized PnL: 10037.0 - 10000 = +37.0c
        assert im.get_realized_pnl(ticker) == 37.0
        assert im.balance_cents - 10000.0 == im.get_realized_pnl(ticker)

    def test_fee_adjusted_outcome_classification(self):
        tracker = PnLTracker()
        ticker = "KXTEST-NET-FEE"

        # Buy 2 @ 50c
        tracker.record_fill(ticker, action="buy", side="yes", count=2, price_cents=50)

        # Sell 2 @ 51c with fee of 3c:
        # Gross gain = (51 - 50) * 2 = +2c
        # Net gain = +2c - 3c = -1c -> Classified as LOSS because of fee
        res = tracker.record_fill(ticker, action="sell", side="yes", count=2, price_cents=51, fee_cents=3.0)
        assert res["matched_contracts"] == 2
        assert res["matched_outcomes"] == ["loss"]
        assert tracker.get_realized_pnl(ticker) == -1.0
        summary = tracker.get_market_summary(ticker)
        assert summary["winning_trades"] == 0
        assert summary["losing_trades"] == 1

    def test_entry_fee_adjusted_outcome_classification(self):
        """Verify that allocated entry fees are included when classifying matched outcomes."""
        tracker = PnLTracker()
        ticker = "KXTEST-ENTRY-FEE"

        # Buy 1 @ 50c with 2c entry fee
        tracker.record_fill(ticker, action="buy", side="yes", count=1, price_cents=50, fee_cents=2.0)

        # Sell 1 @ 51c with 0c closing fee
        # Gross gain = +1c, Entry fee = 2c, Closing fee = 0c -> Net gain = -1c (Loss)
        res1 = tracker.record_fill(ticker, action="sell", side="yes", count=1, price_cents=51, fee_cents=0.0)
        assert res1["matched_outcomes"] == ["loss"]
        summary1 = tracker.get_market_summary(ticker)
        assert summary1["winning_trades"] == 0
        assert summary1["losing_trades"] == 1

        # Another round trip: Buy 1 @ 50c with 1c entry fee, sell @ 53c with 1c closing fee
        # Gross gain = +3c, Entry fee = 1c, Closing fee = 1c -> Net gain = +1c (Profit)
        tracker.record_fill(ticker, action="buy", side="yes", count=1, price_cents=50, fee_cents=1.0)
        res2 = tracker.record_fill(ticker, action="sell", side="yes", count=1, price_cents=53, fee_cents=1.0)
        assert res2["matched_outcomes"] == ["profit"]
        summary2 = tracker.get_market_summary(ticker)
        assert summary2["winning_trades"] == 1
        assert summary2["losing_trades"] == 1

    def test_reconcile_inventory_branches(self):
        """Verify reconcile_inventory handles drift expansion, reduction, flipping, and clearing."""
        tracker = PnLTracker()
        ticker = "KXTEST-RECON-BRANCHES"

        # 1. Delta == 0 does nothing
        tracker.reconcile_inventory(ticker, 0)
        assert tracker.get_open_inventory(ticker) == 0

        # 2. Flat to positive (seed uncosted)
        tracker.reconcile_inventory(ticker, 5)
        assert tracker.get_open_inventory(ticker) == 5
        market = tracker.get_or_create_market(ticker)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].is_uncosted is True

        # 3. Expansion: 5 -> 8 -> 10 (adds lots of 3 and 2)
        tracker.reconcile_inventory(ticker, 8)
        tracker.reconcile_inventory(ticker, 10)
        assert tracker.get_open_inventory(ticker) == 10
        assert len(market.open_lots) == 3

        # 4. Reduction: 10 -> 4 (FIFO trims lot 0 of 5, trims 1 from lot 1 leaving 2, preserves lot 2 of 2)
        market.last_mid_price = 45.0
        pnl_before = tracker.get_realized_pnl(ticker)
        tracker.reconcile_inventory(ticker, 4)
        assert tracker.get_open_inventory(ticker) == 4
        assert len(market.open_lots) == 2
        assert market.open_lots[0].count == 2
        assert market.open_lots[1].count == 2
        assert tracker.get_realized_pnl(ticker) == pnl_before

        # 5. Position flipped: 4 -> -3 (clears old lots, opens uncosted short lot of 3)
        tracker.reconcile_inventory(ticker, -3)
        assert tracker.get_open_inventory(ticker) == -3
        assert len(market.open_lots) == 1
        assert market.open_lots[0].action == "sell"
        assert market.open_lots[0].count == 3
        assert market.open_lots[0].is_uncosted is True

        # 6. Cleared to 0
        tracker.reconcile_inventory(ticker, 0)
        assert tracker.get_open_inventory(ticker) == 0
        assert len(market.open_lots) == 0

    def test_reconcile_inventory_preserves_deferred_entry_fees_in_realized_pnl(self):
        """Verify reconcile_inventory realizes deferred entry fees when open lots are trimmed or cleared."""
        tracker = PnLTracker()
        ticker = "KXTEST-RECON-FEES"

        # Buy 10 @ 50c with 5c fee (0.5c/contract entry fee)
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=50, fee_cents=5.0)
        assert tracker.get_realized_pnl(ticker) == 0.0
        assert tracker.update_mid_price(ticker, 50.0) == -5.0
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == -5.0

        # Partial reconciliation trim: 10 -> 6 (trims 4 contracts, entry fee = 4 * 0.5 = 2.0c)
        tracker.reconcile_inventory(ticker, 6)
        assert tracker.get_open_inventory(ticker) == 6
        # Realized PnL now reflects the 2.0c entry fee of trimmed contracts
        assert tracker.get_realized_pnl(ticker) == -2.0
        # Remaining 6 lots carry 3.0c deferred entry fee -> Unrealized at mid 50c is -3.0c
        assert tracker.get_unrealized_pnl(ticker) == -3.0
        # Total PnL conserved: -2.0c + -3.0c = -5.0c!
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == -5.0

        # Flattened to 0 by reconciliation: remaining 6 contracts cleared, entry fee = 6 * 0.5 = 3.0c
        tracker.reconcile_inventory(ticker, 0)
        assert tracker.get_open_inventory(ticker) == 0
        # All 5.0c fees now realized into realized PnL
        assert tracker.get_realized_pnl(ticker) == -5.0
        assert tracker.get_unrealized_pnl(ticker) == 0.0
        # Total PnL strictly conserved: -5.0c!
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == -5.0

    def test_reversal_fill_fee_attribution_proportional_and_no_double_counting(self):
        """Verify that position reversal fills divide fee by total fill count, avoiding double-counting."""
        tracker = PnLTracker()
        ticker = "KXTEST-REV-FEE"

        # 1. Buy 4 @ 50c with 2.0c fee -> entry fee per contract = 0.5c
        tracker.record_fill(ticker, action="buy", side="yes", count=4, price_cents=50, fee_cents=2.0)
        market = tracker.get_or_create_market(ticker)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].entry_fee_per_contract == 0.5

        # 2. Sell 10 @ 60c with 10.0c fee (overshoot / reversal)
        # Matched contracts: 4. Gross pnl = (60 - 50) * 4 = +40.0c
        # Closing fee for matched 4 contracts = 10.0c * (4 / 10) = 4.0c (NOT 10.0c!)
        # Entry fee for matched 4 contracts = 0.5c * 4 = 2.0c
        # Net trade pnl = 40.0c - (4.0c + 2.0c) = +34.0c (PROFIT)
        res = tracker.record_fill(ticker, action="sell", side="yes", count=10, price_cents=60, fee_cents=10.0)
        assert res["matched_outcomes"] == ["profit"]
        assert market.round_trips_count == 1
        assert market.winning_trades_count == 1

        # The new opened short lot of 6 contracts must carry entry fee = 10.0c / 10 = 1.0c/contract
        assert len(market.open_lots) == 1
        assert market.open_lots[0].count == 6
        assert market.open_lots[0].action == "sell"
        assert market.open_lots[0].entry_fee_per_contract == 1.0

        # 3. Buy back the 6 short contracts @ 55c with 6.0c fee
        # Matched contracts: 6. Gross pnl = (60 - 55) * 6 = +30.0c
        # Closing fee = 6.0c * (6 / 6) = 6.0c
        # Entry fee = 1.0c * 6 = 6.0c
        # Net trade pnl = 30.0c - (6.0c + 6.0c) = +18.0c (PROFIT)
        res2 = tracker.record_fill(ticker, action="buy", side="yes", count=6, price_cents=55, fee_cents=6.0)
        assert res2["matched_outcomes"] == ["profit"]
        assert market.round_trips_count == 2
        assert market.winning_trades_count == 2
        assert len(market.open_lots) == 0

        # Total realized pnl check:
        # Gross gain = 40c + 30c = 70c
        # Total fees paid = 2c + 10c + 6c = 18c
        # Net realized pnl = 70c - 18c = +52c
        assert tracker.get_realized_pnl(ticker) == 52.0

    @pytest.mark.asyncio
    async def test_inventory_manager_reconciliation_skips_when_fill_arrives_in_flight(self):
        """Verify that periodic REST reconciliation skips when a WebSocket fill arrives in flight."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.positions["KXTEST-DRIFT"] = 5
        im.balance_cents = 5000

        # Simulate a fill arriving while _fetch_positions is executing
        def simulate_in_flight_fill():
            im._handle_fill({
                "market_ticker": "KXTEST-DRIFT",
                "action": "buy",
                "side": "yes",
                "count": 3,
                "price": 50,
                "fee_cents": 1.0
            })
            return [{"ticker": "KXTEST-DRIFT", "position": 5}] # Stale REST response

        with patch.object(im, "_fetch_balance", return_value=5000), \
             patch.object(im, "_fetch_positions", side_effect=simulate_in_flight_fill):
            # Periodic reconciliation (is_startup=False) must detect the in-flight fill and abort
            applied = await im.hydrate(is_startup=False)
            assert applied is False
            # Position should remain 8 (5 + 3 from fill), not overwritten with stale 5
            assert im.get_position("KXTEST-DRIFT") == 8

        # Startup hydration (is_startup=True) should ALSO detect in-flight fills and return False
        with patch.object(im, "_fetch_balance", return_value=6000), \
             patch.object(im, "_fetch_positions", side_effect=simulate_in_flight_fill):
            applied_startup_concurrent = await im.hydrate(is_startup=True)
            assert applied_startup_concurrent is False
            assert im.get_position("KXTEST-DRIFT") == 11 # 8 + 3 from second simulated fill

        # Startup hydration (is_startup=True) should apply normally when no concurrent fill occurs
        with patch.object(im, "_fetch_balance", return_value=6000), \
             patch.object(im, "_fetch_positions", return_value=[{"ticker": "KXTEST-DRIFT", "position": 11}]):
            applied_startup = await im.hydrate(is_startup=True)
            assert applied_startup is True
            assert im.balance_cents == 6000
            assert im.get_position("KXTEST-DRIFT") == 11

    def test_apply_positions_publishes_prometheus_gauges(self):
        """Verify _apply_positions publishes Prometheus metrics for active and flattened tickers."""
        from utils.metrics import (
            BOT_INVENTORY_NET_POSITION,
            BOT_PNL_CENTS,
            KALSHI_REALIZED_PNL_CENTS,
            KALSHI_UNREALIZED_PNL_CENTS,
            KALSHI_FEES_PAID_CENTS,
        )
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 7500
        ticker = "KXTEST-GAUGES"

        # 1. Startup hydration with active position
        market_positions = [
            {"ticker": ticker, "position": 10, "market_exposure": "500"}
        ]
        im._apply_positions(market_positions, is_startup=True)

        assert im.get_position(ticker) == 10
        assert BOT_INVENTORY_NET_POSITION.labels(ticker=ticker)._value.get() == 10
        assert BOT_PNL_CENTS.labels(ticker=ticker)._value.get() == 7500
        assert KALSHI_REALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 0.0
        assert KALSHI_FEES_PAID_CENTS.labels(ticker=ticker)._value.get() == 0.0

        # 2. Periodic reconciliation flattens position to 0
        im._apply_positions([], is_startup=False)
        assert im.get_position(ticker) == 0
        assert BOT_INVENTORY_NET_POSITION.labels(ticker=ticker)._value.get() == 0
        assert KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 0.0

    def test_handle_fill_publishes_unrealized_pnl_gauge(self):
        """Verify _handle_fill publishes KALSHI_UNREALIZED_PNL_CENTS after fill recalculation."""
        from utils.metrics import KALSHI_UNREALIZED_PNL_CENTS
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        ticker = "KXTEST-FILL-UNREALIZED"

        # 1. Seed mid price at 50c
        im.update_orderbook_mid(ticker, 50.0)

        # 2. Buy 5 @ 40c -> unrealized PnL = (50 - 40) * 5 = +50c
        im._handle_fill({
            "market_ticker": ticker,
            "action": "buy",
            "side": "yes",
            "count": 5,
            "price": 40,
            "fee_cents": 0.0
        })
        assert KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 50.0

        # 3. Sell 5 @ 50c -> position becomes 0 -> unrealized PnL drops to 0.0 immediately
        im._handle_fill({
            "market_ticker": ticker,
            "action": "sell",
            "side": "yes",
            "count": 5,
            "price": 50,
            "fee_cents": 0.0
        })
        assert KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 0.0

    def test_unrealized_pnl_accounts_for_entry_fees_and_conserves_total_pnl(self):
        """Verify that open lot entry fees are deducted from unrealized PnL, conserving Total PnL across all phases."""
        tracker = PnLTracker()
        ticker = "KXTEST-FEE-CONSERVE"

        # Buy 10 @ 50c with 5c entry fee (0.5c / contract)
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=50, fee_cents=5.0)
        assert tracker.get_realized_pnl(ticker) == 0.0

        # Mark to mid = 50c: price change is 0, but 5c fee was paid -> Unrealized = -5.0c, Total = -5.0c
        unrealized = tracker.update_mid_price(ticker, 50.0)
        assert unrealized == -5.0
        summary = tracker.get_market_summary(ticker)
        assert summary["realized_pnl_cents"] == 0.0
        assert summary["unrealized_pnl_cents"] == -5.0
        assert summary["total_pnl_cents"] == -5.0

        # Mid rises to 60c: Gross unrealized = (60-50)*10 = 100c. Net unrealized = 100 - 5 = 95.0c
        unrealized2 = tracker.update_mid_price(ticker, 60.0)
        assert unrealized2 == 95.0
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 95.0

        # Close half (Sell 5 @ 60c with 2.5c exit fee)
        # Closed 5: Gross = (60-50)*5 = 50c. Entry fee = 2.5c, Exit fee = 2.5c -> Net realized = 45.0c
        # Remaining 5: Gross = (60-50)*5 = 50c. Entry fee = 2.5c -> Net unrealized = 47.5c
        # Total PnL = 45.0 + 47.5 = 92.5c (100c total gross - 7.5c total fees paid so far)
        res_close_half = tracker.record_fill(ticker, action="sell", side="yes", count=5, price_cents=60, fee_cents=2.5)
        assert res_close_half["realized_delta_cents"] == 45.0
        assert tracker.get_realized_pnl(ticker) == 45.0
        assert tracker.get_unrealized_pnl(ticker) == 47.5
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 92.5

        # Close remainder (Sell 5 @ 60c with 2.5c exit fee)
        # Closed final 5: Net realized = 45.0c -> Cumulative realized = 90.0c
        # Unrealized = 0.0c (no open lots) -> Total PnL = 90.0c (100c gross - 10c total fees)
        res_close_all = tracker.record_fill(ticker, action="sell", side="yes", count=5, price_cents=60, fee_cents=2.5)
        assert tracker.get_realized_pnl(ticker) == 90.0
        assert tracker.get_unrealized_pnl(ticker) == 0.0
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 90.0

    def test_reconcile_inventory_conserves_gross_mtm_pnl_across_all_transitions(self):
        """
        Verify that when costed lots with unrealized mark-to-market PnL are reconciled
        (partial trim, position reversal, or flattened to zero), net mark-to-market PnL
        is realized so Total Strategy PnL is strictly conserved.
        """
        tracker = PnLTracker()
        ticker = "KXTEST-RECON-CONSERVE"

        # 1. Buy 10 @ 50c with 5.0c fee (0.5c/contract)
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=50, fee_cents=5.0)
        # Mid rises to 60c: Gross MTM = (60-50)*10 = 100c. Net unrealized = 100 - 5 = 95.0c
        tracker.update_mid_price(ticker, 60.0)
        assert tracker.get_realized_pnl(ticker) == 0.0
        assert tracker.get_unrealized_pnl(ticker) == 95.0
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 95.0

        # 2. Partial trim: 10 -> 6 (trim 4 contracts)
        # 4 trimmed contracts: gross MTM = (60-50)*4 = 40c, entry fee = 2c -> net realized = 38.0c
        # 6 remaining contracts: gross MTM = (60-50)*6 = 60c, entry fee = 3c -> net unrealized = 57.0c
        tracker.reconcile_inventory(ticker, 6)
        assert tracker.get_open_inventory(ticker) == 6
        assert tracker.get_realized_pnl(ticker) == 38.0
        assert tracker.get_unrealized_pnl(ticker) == 57.0
        # Total PnL strictly conserved: 38 + 57 = 95.0c!
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 95.0

        # 3. Position sign reversal: 6 -> -4
        # All 6 remaining contracts removed: net realized = 57.0c -> cumulative realized = 38 + 57 = 95.0c
        # New 4 short contracts are uncosted -> net unrealized = 0.0c
        tracker.reconcile_inventory(ticker, -4)
        assert tracker.get_open_inventory(ticker) == -4
        assert tracker.get_realized_pnl(ticker) == 95.0
        assert tracker.get_unrealized_pnl(ticker) == 0.0
        # Total PnL strictly conserved: 95.0 + 0 = 95.0c!
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 95.0

        # 4. Flatten to zero: -4 -> 0
        # 4 uncosted short contracts cleared: realized delta = 0.0c
        tracker.reconcile_inventory(ticker, 0)
        assert tracker.get_open_inventory(ticker) == 0
        assert tracker.get_realized_pnl(ticker) == 95.0
        assert tracker.get_unrealized_pnl(ticker) == 0.0
        # Total PnL strictly conserved: 95.0c!
        assert tracker.get_market_summary(ticker)["total_pnl_cents"] == 95.0

    def test_reconciliation_adjustment_and_session_baseline_attribution(self):
        """Verify reconciliation adjustments are explicitly tracked and session baselines isolate intra-session PnL."""
        tracker = PnLTracker()
        ticker = "KXTEST-RECON-SESSION"

        tracker.record_fill(ticker=ticker, action="buy", side="yes", count=10, price_cents=50, fee_cents=0.0)
        tracker.update_mid_price(ticker, 60.0)  # Unrealized: +100c

        # Reconcile position down to 0: MTM (+100c) is transferred to realized and tracked as reconciliation adjustment
        tracker.reconcile_inventory(ticker, target_position=0)
        summary = tracker.get_market_summary(ticker)
        assert summary["realized_pnl_cents"] == 100.0
        assert summary["reconciliation_adjustment_cents"] == 100.0
        assert summary["session_realized_pnl_cents"] == 100.0

        # Reset session for rotation: session baseline is saved at 100.0
        tracker.reset_market_session(ticker)
        summary_after_reset = tracker.get_market_summary(ticker)
        assert summary_after_reset["realized_pnl_cents"] == 100.0
        # Intra-session realized delta resets to 0.0
        assert summary_after_reset["session_realized_pnl_cents"] == 0.0

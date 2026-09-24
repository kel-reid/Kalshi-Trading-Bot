"""
Unit Tests for Real-Time PnL Tracker and Trade Attribution Engine

Tests FIFO lot matching, YES/NO normalization, fees accounting,
mark-to-market unrealized PnL, trade classification, and market rotation attribution.
"""

import asyncio
import pytest
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

from data.pnl_tracker import PnLTracker, InventoryLot, MarketPnL
from data.inventory_manager import InventoryManager
from execution.order_manager import OrderManager


class TestPnLTrackerFIFO:
    """Tests core FIFO lot matching and realized PnL calculation."""

    def test_single_long_roundtrip_profit(self):
        """Buy 10 YES @ 40c, Sell 10 YES @ 60c -> Realized PnL = +200c ($2.00)."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-T1"

        # Buy 10 @ 40c
        r1 = tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=40)
        assert r1["matched_contracts"] == 0
        assert r1["new_contracts"] == 10
        assert r1["realized_delta_cents"] == 0.0
        assert tracker.get_realized_pnl(ticker) == 0.0
        assert tracker.get_open_inventory(ticker) == 10

        # Sell 10 @ 60c
        r2 = tracker.record_fill(ticker, action="sell", side="yes", count=10, price_cents=60)
        assert r2["matched_contracts"] == 10
        assert r2["new_contracts"] == 0
        assert r2["realized_delta_cents"] == 200.0  # (60 - 40) * 10
        assert tracker.get_realized_pnl(ticker) == 200.0
        assert tracker.get_open_inventory(ticker) == 0

        summary = tracker.get_market_summary(ticker)
        assert summary["realized_pnl_cents"] == 200.0
        assert summary["round_trips_count"] == 1
        assert summary["winning_trades"] == 1
        assert summary["losing_trades"] == 0
        assert summary["scratch_trades"] == 0

    def test_single_short_roundtrip_profit(self):
        """Sell 5 YES @ 70c (short), Buy 5 YES @ 50c (cover) -> Realized PnL = +100c ($1.00)."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-T2"

        # Sell 5 @ 70c
        tracker.record_fill(ticker, action="sell", side="yes", count=5, price_cents=70)
        assert tracker.get_open_inventory(ticker) == -5

        # Cover 5 @ 50c
        r = tracker.record_fill(ticker, action="buy", side="yes", count=5, price_cents=50)
        assert r["matched_contracts"] == 5
        assert r["realized_delta_cents"] == 100.0  # (70 - 50) * 5
        assert tracker.get_realized_pnl(ticker) == 100.0
        assert tracker.get_open_inventory(ticker) == 0

        summary = tracker.get_market_summary(ticker)
        assert summary["winning_trades"] == 1

    def test_loss_and_scratch_trades(self):
        """Test losing trade and scratch (breakeven) trade classification."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-T3"

        # Trade 1: Buy 2 @ 60c, Sell 2 @ 50c -> Loss of 20c
        tracker.record_fill(ticker, action="buy", side="yes", count=2, price_cents=60)
        tracker.record_fill(ticker, action="sell", side="yes", count=2, price_cents=50)
        assert tracker.get_realized_pnl(ticker) == -20.0

        # Trade 2: Buy 3 @ 45c, Sell 3 @ 45c -> Scratch (0c)
        tracker.record_fill(ticker, action="buy", side="yes", count=3, price_cents=45)
        tracker.record_fill(ticker, action="sell", side="yes", count=3, price_cents=45)
        assert tracker.get_realized_pnl(ticker) == -20.0

        summary = tracker.get_market_summary(ticker)
        assert summary["round_trips_count"] == 2
        assert summary["winning_trades"] == 0
        assert summary["losing_trades"] == 1
        assert summary["scratch_trades"] == 1

    def test_multi_lot_fifo_ordering(self):
        """
        Test that FIFO correctly closes older lots first:
        - Lot 1: Buy 5 @ 30c
        - Lot 2: Buy 5 @ 40c
        - Sell 7 @ 50c ->
            - Closes 5 from Lot 1: (50 - 30) * 5 = +100c
            - Closes 2 from Lot 2: (50 - 40) * 2 = +20c
            - Total realized = +120c
            - Remaining in Lot 2 = 3 contracts @ 40c
        """
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-T4"

        tracker.record_fill(ticker, action="buy", side="yes", count=5, price_cents=30)
        tracker.record_fill(ticker, action="buy", side="yes", count=5, price_cents=40)

        r = tracker.record_fill(ticker, action="sell", side="yes", count=7, price_cents=50)
        assert r["matched_contracts"] == 7
        assert r["realized_delta_cents"] == 120.0
        assert tracker.get_realized_pnl(ticker) == 120.0
        assert tracker.get_open_inventory(ticker) == 3

        market = tracker.get_or_create_market(ticker)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].count == 3
        assert market.open_lots[0].price_cents == 40.0

    def test_overshoot_position_reversal(self):
        """
        Long 3 contracts, then sell 8 contracts:
        - Closes 3 long @ 50c against sell @ 60c -> +30c
        - Opens 5 short @ 60c
        - Net position goes from +3 to -5
        """
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-T5"

        tracker.record_fill(ticker, action="buy", side="yes", count=3, price_cents=50)
        r = tracker.record_fill(ticker, action="sell", side="yes", count=8, price_cents=60)

        assert r["matched_contracts"] == 3
        assert r["new_contracts"] == 5
        assert r["realized_delta_cents"] == 30.0
        assert tracker.get_realized_pnl(ticker) == 30.0
        assert tracker.get_open_inventory(ticker) == -5

        market = tracker.get_or_create_market(ticker)
        assert len(market.open_lots) == 1
        assert market.open_lots[0].action == "sell"
        assert market.open_lots[0].count == 5
        assert market.open_lots[0].price_cents == 60.0


class TestPnLTrackerNormalizationAndFees:
    """Tests NO-contract normalization and fee deductions."""

    def test_no_contract_normalization(self):
        """
        Buy NO @ 40c is equivalent to Sell YES @ 60c.
        Then Sell NO @ 30c is equivalent to Buy YES @ 70c (which covers the short).
        PnL should be (60 - 70) * 5 = -50c.
        """
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-NO1"

        # Buy 5 NO @ 40c -> Opens short 5 YES @ 60c
        tracker.record_fill(ticker, action="buy", side="no", count=5, price_cents=40)
        assert tracker.get_open_inventory(ticker) == -5

        # Sell 5 NO @ 30c -> Covers 5 YES @ 70c
        r = tracker.record_fill(ticker, action="sell", side="no", count=5, price_cents=30)
        assert r["matched_contracts"] == 5
        assert r["realized_delta_cents"] == -50.0  # (60 - 70) * 5
        assert tracker.get_realized_pnl(ticker) == -50.0
        assert tracker.get_open_inventory(ticker) == 0

    def test_fee_accounting(self):
        """Fees should be deducted from realized PnL and tracked cumulatively."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-FEE"

        # Buy 10 @ 50c, fee = 5c
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=50, fee_cents=5.0)
        assert tracker.get_total_fees(ticker) == 5.0
        # Entry fee is attached to open lots and deferred until matching; realized PnL remains 0.0
        assert tracker.get_realized_pnl(ticker) == 0.0

        # Sell 10 @ 60c, fee = 5c -> gross profit 100c - 10c total fees = +90c delta
        tracker.record_fill(ticker, action="sell", side="yes", count=10, price_cents=60, fee_cents=5.0)
        assert tracker.get_total_fees(ticker) == 10.0
        assert tracker.get_realized_pnl(ticker) == 90.0  # 100 gross - 10 total fees


class TestMarkToMarketUnrealizedPnL:
    """Tests unrealized PnL mark-to-market calculations."""

    def test_long_inventory_mark_to_market(self):
        """Long 10 contracts @ 40c. Mid price moves to 48c -> Unrealized = +80c."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-MTM1"

        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=40)
        unrealized = tracker.update_mid_price(ticker, 48.0)
        assert unrealized == 80.0
        assert tracker.get_unrealized_pnl(ticker) == 80.0

        # Mid price drops to 35c -> Unrealized = -50c
        unrealized2 = tracker.update_mid_price(ticker, 35.0)
        assert unrealized2 == -50.0
        assert tracker.get_unrealized_pnl(ticker) == -50.0

    def test_short_inventory_mark_to_market(self):
        """Short 5 contracts @ 60c. Mid price drops to 52c -> Unrealized = +40c."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-MTM2"

        tracker.record_fill(ticker, action="sell", side="yes", count=5, price_cents=60)
        unrealized = tracker.update_mid_price(ticker, 52.0)
        assert unrealized == 40.0  # (60 - 52) * 5

        # Mid price jumps to 70c -> Unrealized = -50c
        unrealized2 = tracker.update_mid_price(ticker, 70.0)
        assert unrealized2 == -50.0

    def test_uncosted_inventory_mark_to_market_skipped(self):
        """Uncosted lots are skipped in mark-to-market calculations."""
        tracker = PnLTracker()
        ticker = "KXTEST-UNCOSTED-MTM"
        tracker.seed_initial_inventory(ticker, 5, cost_basis_cents=None)
        unrealized = tracker.update_mid_price(ticker, 55.0)
        assert unrealized == 0.0


class TestSessionAttributionAndRotation:
    """Tests session tracking across market rotations."""

    def test_rotation_session_reset(self):
        """Starting a new session resets rotation_session_id without dropping ticker state."""
        tracker = PnLTracker()
        ticker = "KXTEST-26SEP-ROT"

        s1 = tracker.get_or_create_market(ticker).rotation_session_id
        assert s1 is not None

        s2 = tracker.reset_market_session(ticker)
        assert s2 != s1
        assert tracker.get_or_create_market(ticker).rotation_session_id == s2


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


class TestOrderManagerPnLPersistence:
    """Verifies OrderManager persists PnL attribution snapshots to DB."""

    def test_record_pnl_snapshot_sql(self):
        om = OrderManager()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        with patch.object(om, "_get_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            om.record_pnl_snapshot(
                ticker="KXTEST-26SEP-PERSIST",
                realized_pnl_cents=125.50,
                unrealized_pnl_cents=32.00,
                total_fees_cents=8.50,
                inventory=5,
                rotation_session_id="11111111-2222-3333-4444-555555555555"
            )

            assert mock_cursor.execute.called
            call_sql = mock_cursor.execute.call_args[0][0]
            call_args = mock_cursor.execute.call_args[0][1]

            assert "INSERT INTO pnl_attribution" in call_sql
            assert call_args[0] == "KXTEST-26SEP-PERSIST"
            assert call_args[2] == 125.5
            assert call_args[3] == 32.0
            assert call_args[4] == 8.5
            assert call_args[5] == 5
            assert call_args[6] == "11111111-2222-3333-4444-555555555555"
            assert mock_conn.commit.called

    def test_record_pnl_snapshot_rejects_invalid_uuid(self):
        om = OrderManager()
        with pytest.raises(ValueError, match="Invalid rotation_session_id"):
            om.record_pnl_snapshot(
                ticker="KXTEST-BAD-UUID",
                realized_pnl_cents=0.0,
                unrealized_pnl_cents=0.0,
                total_fees_cents=0.0,
                inventory=0,
                rotation_session_id="not-a-valid-uuid"
            )

    @pytest.mark.asyncio
    async def test_record_pnl_snapshot_async(self):
        om = OrderManager()
        with patch.object(om, "record_pnl_snapshot") as mock_record:
            await om.record_pnl_snapshot_async(
                ticker="KXTEST-26SEP-ASYNC",
                realized_pnl_cents=50.0,
                unrealized_pnl_cents=10.0,
                total_fees_cents=2.0,
                inventory=2,
                rotation_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )
            mock_record.assert_called_once_with(
                ticker="KXTEST-26SEP-ASYNC",
                realized_pnl_cents=50.0,
                unrealized_pnl_cents=10.0,
                total_fees_cents=2.0,
                inventory=2,
                rotation_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )

    def test_record_pnl_snapshot_exception_handled(self):
        om = OrderManager()
        with patch.object(om, "_get_connection", side_effect=Exception("DB pool exhausted")):
            # Should not raise exception
            om.record_pnl_snapshot(
                ticker="KXTEST-26SEP-ERR",
                realized_pnl_cents=10.0,
                unrealized_pnl_cents=5.0,
                total_fees_cents=1.0,
                inventory=1,
                rotation_session_id="22222222-3333-4444-5555-666666666666"
            )


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



class TestMarketMakerPnLLifecycle:
    """Verifies MarketMaker PnL snapshot triggers and error handling."""

    @pytest.mark.asyncio
    async def test_tick_triggers_periodic_pnl_snapshot(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-TICK", min_spread=2)
        import asyncio
        snapshot_event = asyncio.Event()

        async def controlled_snapshot(**kwargs):
            await snapshot_event.wait()

        bot.om.record_pnl_snapshot_async = AsyncMock(side_effect=controlled_snapshot)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
        bot._update_quotes = AsyncMock()

        # Force periodic interval trigger
        bot._last_pnl_snapshot = 0.0
        await bot._tick()

        bot.om.record_pnl_snapshot_async.assert_called_once()
        # Task must be actively registered in _background_tasks while executing
        assert len(bot._background_tasks) == 1
        active_task = next(iter(bot._background_tasks))
        assert not active_task.done()

        # Release the event to complete snapshot
        snapshot_event.set()
        await active_task

        # Task must be discarded from _background_tasks upon completion
        assert len(bot._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_tick_hedged_logging_with_pnl(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-HEDGE", min_spread=2)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
        bot._update_quotes = AsyncMock()

        # Long hedge
        bot.inv_manager.positions[bot.ticker] = 10
        await bot._tick()

        # Short hedge
        bot.inv_manager.positions[bot.ticker] = -10
        await bot._tick()

    @pytest.mark.asyncio
    async def test_rotate_market_records_pnl_and_handles_error(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-ROT1")
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.ob_manager.unsubscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()
        bot.om.record_pnl_snapshot_async = AsyncMock()

        # Successful snapshot during rotation
        res = await bot.rotate_market("KXTEST-MM-ROT2")
        assert res is True
        bot.om.record_pnl_snapshot_async.assert_called_once()
        assert bot.ticker == "KXTEST-MM-ROT2"

        # Exception during snapshot write should be logged and not abort rotation
        bot.om.record_pnl_snapshot_async.side_effect = Exception("Snapshot async failed")
        res2 = await bot.rotate_market("KXTEST-MM-ROT3")
        assert res2 is True
        assert bot.ticker == "KXTEST-MM-ROT3"

    @pytest.mark.asyncio
    async def test_stop_records_pnl_and_handles_error(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-STOP")
        call_order = []
        bot._cancel_all_quotes = AsyncMock(side_effect=lambda: call_order.append("cancel"))
        bot.om.record_pnl_snapshot_async = AsyncMock(side_effect=lambda **kwargs: call_order.append("snapshot"))

        # Successful stop: quotes cancelled first, then snapshot persisted
        await bot.stop()
        assert call_order == ["cancel", "snapshot"]
        assert bot.running is False

        # Exception in stop snapshot should not prevent quote cancellation
        call_order.clear()
        bot.om.record_pnl_snapshot_async.side_effect = Exception("Stop snapshot failed")
        await bot.stop()
        assert "cancel" in call_order

        # Background tasks are cancelled and awaited upon stop
        dummy_task = asyncio.create_task(asyncio.sleep(10))
        bot._background_tasks.add(dummy_task)
        dummy_task.add_done_callback(bot._background_tasks.discard)
        await bot.stop()
        assert dummy_task.cancelled()
        assert len(bot._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_escalates_to_emergency_kill_on_cancel_failure(self):
        """Verify bot.stop() escalates to emergency KillSwitch when _cancel_all_quotes fails."""
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-FAIL-CANCEL")
        bot._cancel_all_quotes = AsyncMock(return_value=False)
        bot.om.record_pnl_snapshot_async = AsyncMock()

        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock()

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            result = await bot.stop()

        mock_killer.trigger.assert_awaited_once()
        assert result is True
        assert bot.running is False

    @pytest.mark.asyncio
    async def test_stop_escalates_to_emergency_kill_when_om_has_tracked_active_orders(self):
        """Verify bot.stop() triggers emergency KillSwitch when om.active_orders is non-empty even if quote IDs are None."""
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-ACTIVE-ORDERS")
        bot.current_bid_id = None
        bot.current_ask_id = None
        bot.om.active_orders = {"stray-order-1": {"kalshi_order_id": "k1"}}
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.om.record_pnl_snapshot_async = AsyncMock()

        async def mock_kill():
            bot.om.active_orders.clear()

        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock(side_effect=mock_kill)

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            result = await bot.stop()

        mock_killer.trigger.assert_awaited_once()
        assert result is True
        assert len(bot.om.active_orders) == 0

    @pytest.mark.asyncio
    async def test_stop_clears_quote_ids_and_returns_true_when_kill_switch_clears_active_orders(self):
        """Verify bot.stop() resets current quote IDs and returns True if KillSwitch successfully cancels active orders."""
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-KILL-RECOVER")
        bot.current_bid_id = "bid-order-1"
        bot.current_bid_price = 45
        bot.om.active_orders = {"bid-order-1": {"kalshi_order_id": "k-bid-1"}}
        bot._cancel_all_quotes = AsyncMock(return_value=False)
        bot.om.record_pnl_snapshot_async = AsyncMock()

        async def mock_kill():
            bot.om.active_orders.clear()

        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock(side_effect=mock_kill)

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            result = await bot.stop()

        mock_killer.trigger.assert_awaited_once()
        assert result is True
        assert bot.current_bid_id is None
        assert bot.current_bid_price is None

    @pytest.mark.asyncio
    async def test_market_maker_start_propagates_fatal_exception(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-CRASH")
        bot.om.sync_and_recover_state = AsyncMock()
        bot.ws_client.connect = AsyncMock()
        bot.ws_client.is_connected = True
        bot.inv_manager.hydrate = AsyncMock(return_value=True)
        bot.inv_manager._sync_loop = AsyncMock()
        bot.inv_manager.subscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()

        # Make _tick raise a fatal exception
        bot._tick = AsyncMock(side_effect=RuntimeError("Fatal quoting error"))

        with patch("strategy.market_maker.start_metrics_server"), \
             patch("strategy.market_maker.send_alert", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="Fatal quoting error"):
                await bot.start()

        assert bot.running is False

    @pytest.mark.asyncio
    async def test_market_maker_start_aborts_on_startup_hydration_failure(self):
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-HYDRATE-FAIL")
        bot.om.sync_and_recover_state = AsyncMock()
        bot.ws_client.connect = AsyncMock()
        bot.ws_client.is_connected = True
        # Simulate startup hydration returning False (e.g. REST API timeout/error)
        bot.inv_manager.hydrate = AsyncMock(return_value=False)
        bot.inv_manager._sync_loop = AsyncMock()
        bot.inv_manager.subscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()

        with patch("strategy.market_maker.start_metrics_server"), \
             patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
            with pytest.raises(RuntimeError, match="Startup hydration failed"):
                await bot.start()

        assert bot.running is False
        mock_alert.assert_awaited_once()
        assert "Startup Aborted" in mock_alert.await_args[0][0]

    def test_inventory_manager_rejects_non_positive_fill_counts(self):
        """Verify _handle_fill rejects count <= 0 at the ingress boundary without mutating any state."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000
        initial_fill_count = im._fill_count

        # Zero count
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "yes", "count": 0, "price": 50})
        # Negative count
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "yes", "count": -5, "price": 50})
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "sell", "side": "yes", "count": -1, "price": 50})

        assert im.balance_cents == 10000
        assert im.get_position("KXTEST-REJECT") == 0
        assert im._fill_count == initial_fill_count
        assert im.get_realized_pnl("KXTEST-REJECT") == 0.0

    def test_inventory_manager_rejects_invalid_action_and_side(self):
        """Verify _handle_fill rejects invalid action and side values without mutating state or counters."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000
        initial_fill_count = im._fill_count

        # Missing action
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "side": "yes", "count": 5, "price": 50})
        # Invalid action
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "hold", "side": "yes", "count": 5, "price": 50})
        # Missing side
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "count": 5, "price": 50})
        # Invalid side
        im._handle_fill({"market_ticker": "KXTEST-REJECT", "action": "buy", "side": "maybe", "count": 5, "price": 50})

        assert im.balance_cents == 10000
        assert im.get_position("KXTEST-REJECT") == 0
        assert im._fill_count == initial_fill_count
        assert im.get_realized_pnl("KXTEST-REJECT") == 0.0

    @pytest.mark.asyncio
    async def test_hydrate_startup_requires_both_balance_and_positions(self):
        """Verify startup hydration fails if either balance or positions fetch fails, and succeeds only when both exist."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)

        # 1. Balance succeeds, positions fails -> Startup must return False and NOT mutate balance_cents
        im.balance_cents = 0
        with patch.object(im, "_fetch_balance", return_value=5000), \
             patch.object(im, "_fetch_positions", return_value=None):
            assert await im.hydrate(is_startup=True) is False
            assert im.balance_cents == 0

        # 2. Balance fails, positions succeeds -> Startup must return False and NOT seed positions/lots
        with patch.object(im, "_fetch_balance", return_value=None), \
             patch.object(im, "_fetch_positions", return_value=[{"ticker": "KXTEST-FAIL", "position": 10}]):
            assert await im.hydrate(is_startup=True) is False
            assert im.get_position("KXTEST-FAIL") == 0
            assert len(im.pnl_tracker.get_or_create_market("KXTEST-FAIL").open_lots) == 0

        # 3. Both succeed -> Startup returns True and mutates state
        with patch.object(im, "_fetch_balance", return_value=5000), \
             patch.object(im, "_fetch_positions", return_value=[]):
            assert await im.hydrate(is_startup=True) is True
            assert im.balance_cents == 5000

    @pytest.mark.asyncio
    async def test_hydrate_periodic_allows_partial_success(self):
        """Verify periodic reconciliation applies whichever snapshot succeeded and only returns False if both fail."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)

        # 1. Balance succeeds, positions fails -> Periodic returns True
        with patch.object(im, "_fetch_balance", return_value=5500), \
             patch.object(im, "_fetch_positions", return_value=None):
            assert await im.hydrate(is_startup=False) is True
            assert im.balance_cents == 5500

        # 2. Balance fails, positions succeeds -> Periodic returns True
        with patch.object(im, "_fetch_balance", return_value=None), \
             patch.object(im, "_fetch_positions", return_value=[{"ticker": "KXTEST-PARTIAL", "position": 2}]):
            assert await im.hydrate(is_startup=False) is True
            assert im.get_position("KXTEST-PARTIAL") == 2

        # 3. Both fail -> Periodic returns False
        with patch.object(im, "_fetch_balance", return_value=None), \
             patch.object(im, "_fetch_positions", return_value=None):
            assert await im.hydrate(is_startup=False) is False

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

    def test_inventory_manager_rejects_missing_non_numeric_and_negative_prices(self):
        """Verify _handle_fill drops missing, non-numeric, zero, negative, out-of-range (>=100), or boolean prices without incrementing _fill_count."""
        mock_ws = MagicMock()
        im = InventoryManager(ws_client=mock_ws)
        initial_balance = im.balance_cents

        invalid_prices = [None, "", "invalid", 0, -10, -0.01, True, False, 100, 100.5, 150, float("nan"), float("inf")]
        for p in invalid_prices:
            im._handle_fill({
                "market_ticker": "KXTEST-PRICE",
                "action": "buy",
                "side": "yes",
                "count": 5,
                "price": p
            })
            assert im._fill_count == 0, f"Expected _fill_count == 0 for price {p!r}"
            assert im.balance_cents == initial_balance
            assert im.get_position("KXTEST-PRICE") == 0

    def test_inventory_manager_rejects_invalid_fees_without_incrementing_counter(self):
        """Verify _handle_fill drops non-numeric or negative fees without incrementing _fill_count or mutating balance."""
        mock_ws = MagicMock()
        im = InventoryManager(ws_client=mock_ws)
        initial_balance = im.balance_cents

        invalid_fees = ["bad-fee", -5.0, -0.001, float("nan"), float("inf")]
        for f in invalid_fees:
            im._handle_fill({
                "market_ticker": "KXTEST-FEE",
                "action": "buy",
                "side": "yes",
                "count": 5,
                "price": 50,
                "fee_cents": f
            })
            assert im._fill_count == 0, f"Expected _fill_count == 0 for fee {f!r}"
            assert im.balance_cents == initial_balance
            assert im.get_position("KXTEST-FEE") == 0

    @pytest.mark.asyncio
    async def test_stop_cancels_untracked_quote_and_clears_id_only_on_confirmation(self):
        """Verify stop() cancels untracked quotes via om.cancel_order and retains IDs/returns False if cancel fails."""
        from strategy.market_maker import AvellanedaStoikovBot
        bot = AvellanedaStoikovBot(ticker="KXTEST-UNTRACKED")
        bot.current_bid_id = "untracked-bid-999"
        bot.current_bid_price = 45
        bot.om.active_orders = {}  # Quote was never tracked in om.active_orders
        bot._cancel_all_quotes = AsyncMock(return_value=False)
        bot.om.record_pnl_snapshot_async = AsyncMock()

        # Scenario 1: cancel_order fails on Kalshi
        bot.om.cancel_order = AsyncMock(return_value=False)
        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock()

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            res_fail = await bot.stop()

        assert res_fail is False
        assert bot.current_bid_id == "untracked-bid-999"
        bot.om.cancel_order.assert_awaited_with("untracked-bid-999")

        # Scenario 2: cancel_order succeeds on Kalshi
        bot.om.cancel_order = AsyncMock(return_value=True)
        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            res_success = await bot.stop()

        assert res_success is True
        assert bot.current_bid_id is None
        assert bot.current_bid_price is None

    def test_market_summary_preserves_four_decimal_precision(self):
        """Verify get_market_summary preserves 4-decimal precision matching NUMERIC(12,4) schema."""
        tracker = PnLTracker()
        ticker = "KXTEST-PRECISION"
        tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=50.1234, fee_cents=1.5678)
        tracker.update_mid_price(ticker, 55.4321)

        summary = tracker.get_market_summary(ticker)
        assert summary["realized_pnl_cents"] == 0.0
        # (55.4321 - 50.1234) * 10 - 1.5678 = 53.0870 - 1.5678 = 51.5192
        assert summary["unrealized_pnl_cents"] == 51.5192
        assert summary["total_pnl_cents"] == 51.5192
        assert summary["total_fees_cents"] == 1.5678
        assert summary["total_pnl_cents"] == summary["realized_pnl_cents"] + summary["unrealized_pnl_cents"]

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

    def test_fetch_balance_rejects_missing_or_invalid_balance_field(self):
        """Verify _fetch_balance returns None when 'balance' is missing or non-numeric/boolean."""
        im = InventoryManager(ws_client=MagicMock())

        invalid_responses = [
            {},
            {"other_key": 5000},
            {"balance": None},
            {"balance": "5000"},
            {"balance": True},
            {"balance": False},
            {"balance": float("nan")},
            {"balance": float("inf")},
            {"balance": float("-inf")},
        ]
        for invalid_json in invalid_responses:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = invalid_json
            with patch("requests.get", return_value=mock_resp):
                assert im._fetch_balance() is None, f"Expected None for invalid payload: {invalid_json}"

        # Valid balance returns integer cents
        valid_resp = MagicMock(status_code=200)
        valid_resp.json.return_value = {"balance": 12500}
        with patch("requests.get", return_value=valid_resp):
            assert im._fetch_balance() == 12500

    def test_fetch_positions_rejects_missing_or_invalid_positions_field(self):
        """Verify _fetch_positions returns None when 'market_positions' is missing or not a list."""
        im = InventoryManager(ws_client=MagicMock())

        invalid_responses = [
            {},
            {"other_key": []},
            {"market_positions": None},
            {"market_positions": "invalid"},
            {"market_positions": 123},
        ]
        for invalid_json in invalid_responses:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = invalid_json
            with patch("requests.get", return_value=mock_resp):
                assert im._fetch_positions() is None, f"Expected None for invalid payload: {invalid_json}"

        # Valid positions returns the list
        valid_resp = MagicMock(status_code=200)
        valid_resp.json.return_value = {"market_positions": [{"ticker": "KXTEST", "position": 5}]}
        with patch("requests.get", return_value=valid_resp):
            assert im._fetch_positions() == [{"ticker": "KXTEST", "position": 5}]

    def test_handle_fill_supports_raw_vendor_yes_and_no_price_payloads(self):
        """Verify _handle_fill parses real Kalshi vendor fill messages carrying yes_price and no_price."""
        im = InventoryManager(ws_client=MagicMock())
        im.balance_cents = 10000

        # 1. Raw Kalshi YES fill payload with yes_price=45, no_price=55
        raw_yes_fill = {
            "trade_id": "t-1",
            "market_ticker": "KXTEST-VENDOR",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "yes_price": 45,
            "no_price": 55,
            "fee_cents": 2.0
        }
        im._handle_fill(raw_yes_fill)
        assert im._fill_count == 1
        assert im.get_position("KXTEST-VENDOR") == 10
        # Paid (45 * 10) + 2 fee = 452c
        assert im.balance_cents == 10000 - 452
        assert im.get_realized_pnl("KXTEST-VENDOR") == 0.0

        # 2. Raw Kalshi NO fill payload with yes_price=45, no_price=55
        raw_no_fill = {
            "trade_id": "t-2",
            "market_ticker": "KXTEST-VENDOR-NO",
            "action": "buy",
            "side": "no",
            "count": 5,
            "yes_price": 45,
            "no_price": 55,
            "fee_cents": 1.0
        }
        im._handle_fill(raw_no_fill)
        assert im._fill_count == 2
        # Bought NO -> Net YES position is -5
        assert im.get_position("KXTEST-VENDOR-NO") == -5
        # Paid (55 * 5) + 1 fee = 276c
        assert im.balance_cents == (10000 - 452) - 276

    def test_apply_positions_atomically_rejects_malformed_entries_without_partial_mutation(self):
        """Verify _apply_positions rejects malformed snapshots upfront without partially mutating state or PnL lots."""
        im = InventoryManager(ws_client=MagicMock())
        im.positions = {"KXTEST-EXISTING": 5}

        # 1. Non-dict entry in market_positions
        malformed_snapshot_1 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            "not-a-dict"
        ]
        im._apply_positions(malformed_snapshot_1, is_startup=True)
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 2. Malformed non-numeric position_fp value
        malformed_snapshot_2 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-BAD", "position_fp": "invalid-pos"}
        ]
        im._apply_positions(malformed_snapshot_2, is_startup=True)
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 3. Missing ticker
        malformed_snapshot_3 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"position": 5}
        ]
        assert im._apply_positions(malformed_snapshot_3, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 4. Missing position field (neither position_fp nor position provided)
        malformed_snapshot_4 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-NOPOS", "market_exposure": 500}
        ]
        assert im._apply_positions(malformed_snapshot_4, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

        # 5. Malformed non-finite position
        malformed_snapshot_5 = [
            {"ticker": "KXTEST-NEW", "position": 10},
            {"ticker": "KXTEST-NAN", "position_fp": "nan"}
        ]
        assert im._apply_positions(malformed_snapshot_5, is_startup=False) is False
        assert im.positions == {"KXTEST-EXISTING": 5}
        assert len(im.pnl_tracker.get_or_create_market("KXTEST-NEW").open_lots) == 0

    def test_fetch_positions_rejects_non_string_and_blank_tickers(self):
        """Verify _fetch_positions rejects truthy non-string tickers (int, bool), blank strings, and missing position fields."""
        im = InventoryManager(ws_client=MagicMock())

        # 1. Non-string integer ticker
        resp_int = MagicMock(status_code=200)
        resp_int.json.return_value = {"market_positions": [{"ticker": 123, "position": 5}]}
        with patch("requests.get", return_value=resp_int):
            assert im._fetch_positions() is None

        # 2. Blank whitespace ticker
        resp_blank = MagicMock(status_code=200)
        resp_blank.json.return_value = {"market_positions": [{"ticker": "   ", "position": 5}]}
        with patch("requests.get", return_value=resp_blank):
            assert im._fetch_positions() is None

        # 3. Boolean ticker
        resp_bool = MagicMock(status_code=200)
        resp_bool.json.return_value = {"market_positions": [{"ticker": True, "position": 5}]}
        with patch("requests.get", return_value=resp_bool):
            assert im._fetch_positions() is None

        # 4. Missing position and position_fp fields
        resp_nopos = MagicMock(status_code=200)
        resp_nopos.json.return_value = {"market_positions": [{"ticker": "KXTEST-NOPOS", "market_exposure": 500}]}
        with patch("requests.get", return_value=resp_nopos):
            assert im._fetch_positions() is None

        # 5. Non-finite position values
        resp_nan = MagicMock(status_code=200)
        resp_nan.json.return_value = {"market_positions": [{"ticker": "KXTEST-NAN", "position_fp": "nan"}]}
        with patch("requests.get", return_value=resp_nan):
            assert im._fetch_positions() is None

        resp_inf = MagicMock(status_code=200)
        resp_inf.json.return_value = {"market_positions": [{"ticker": "KXTEST-INF", "position": float("inf")}]}
        with patch("requests.get", return_value=resp_inf):
            assert im._fetch_positions() is None

    def test_apply_positions_startup_ignores_non_finite_exposure(self):
        """Verify startup hydration ignores non-finite exposure (leaves lot uncosted) without error."""
        im = InventoryManager(ws_client=MagicMock())
        snapshot = [
            {"ticker": "KXTEST-NAN-EXP", "position": 10, "market_exposure": "nan"},
            {"ticker": "KXTEST-INF-EXP", "position": 5, "market_exposure": float("inf")},
            {"ticker": "KXTEST-ZERO-EXP", "position": 4, "market_exposure": 0},
            {"ticker": "KXTEST-VALID-EXP", "position": 2, "market_exposure": 80.0},
        ]
        assert im._apply_positions(snapshot, is_startup=True) is True
        # Uncosted lots:
        assert im.pnl_tracker.get_or_create_market("KXTEST-NAN-EXP").open_lots[0].is_uncosted is True
        assert im.pnl_tracker.get_or_create_market("KXTEST-INF-EXP").open_lots[0].is_uncosted is True
        assert im.pnl_tracker.get_or_create_market("KXTEST-ZERO-EXP").open_lots[0].is_uncosted is True
        # Costed lot: 80 / 2 = 40.0c
        valid_lot = im.pnl_tracker.get_or_create_market("KXTEST-VALID-EXP").open_lots[0]
        assert valid_lot.is_uncosted is False
        assert valid_lot.price_cents == 40.0

    @pytest.mark.asyncio
    async def test_hydrate_startup_aborts_if_apply_positions_fails(self):
        """Verify hydrate(is_startup=True) returns False and aborts safely if _apply_positions fails."""
        im = InventoryManager(ws_client=MagicMock())
        im.positions = {"EXISTING": 1}

        with patch.object(im, "_fetch_balance", return_value=5000), \
             patch.object(im, "_fetch_positions", return_value=[{"ticker": "NEW", "position": 10}]), \
             patch.object(im, "_apply_positions", return_value=False):
            success = await im.hydrate(is_startup=True)
            assert success is False

    def test_handle_fill_complements_opposite_side_price(self):
        """Verify _handle_fill calculates 100c complement when only opposite-side price is provided."""
        im = InventoryManager(ws_client=MagicMock())
        im.balance_cents = 10000.0

        # 1. YES fill with only no_price=55 -> YES price must be 100 - 55 = 45c
        im._handle_fill({
            "market_ticker": "KXTEST-COMP",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "no_price": 55,
            "fee_cents": 1.0
        })
        assert im.get_position("KXTEST-COMP") == 10
        # Cost: 10 * 45c + 1c fee = 451c -> balance = 10000 - 451 = 9549.0
        assert im.balance_cents == 9549.0

        # 2. NO fill with only yes_price=40 -> NO price must be 100 - 40 = 60c
        im._handle_fill({
            "market_ticker": "KXTEST-COMP-NO",
            "action": "buy",
            "side": "no",
            "count": 5,
            "yes_price": 40,
            "fee_cents": 1.5
        })
        # Buying NO -> Net YES position is -5
        assert im.get_position("KXTEST-COMP-NO") == -5
        # Cost: 5 * 60c + 1.5c fee = 301.5c -> balance = 9549.0 - 301.5 = 9247.5
        assert im.balance_cents == 9247.5






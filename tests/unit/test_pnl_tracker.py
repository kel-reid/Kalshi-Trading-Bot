"""
Unit Tests for Real-Time PnL Tracker and Trade Attribution Engine

Tests FIFO lot matching, YES/NO normalization, fees accounting,
mark-to-market unrealized PnL, trade classification, and market rotation attribution.
"""

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
        # Fee on entry is deducted immediately
        assert tracker.get_realized_pnl(ticker) == -5.0

        # Sell 10 @ 60c, fee = 5c -> gross profit 100c - 5c fee = +95c delta
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
        assert im.get_realized_pnl(ticker) == -2.0  # Entry fee deducted

        # Mid price update
        im.update_orderbook_mid(ticker, 35.0)
        assert im.get_unrealized_pnl(ticker) == 20.0  # (35 - 30) * 4

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
        # Realized: -2.0 (entry fee) + (40 - 30) * 2 - 1.0 (exit fee) = 17.0c
        assert im.get_realized_pnl(ticker) == 17.0

        summary = im.get_pnl_summary(ticker)
        assert summary["ticker"] == ticker
        assert summary["realized_pnl_cents"] == 17.0
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
                rotation_session_id="test-session-123"
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
            assert call_args[6] == "test-session-123"
            assert mock_conn.commit.called

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
                rotation_session_id="async-uuid"
            )
            mock_record.assert_called_once_with(
                ticker="KXTEST-26SEP-ASYNC",
                realized_pnl_cents=50.0,
                unrealized_pnl_cents=10.0,
                total_fees_cents=2.0,
                inventory=2,
                rotation_session_id="async-uuid"
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
                rotation_session_id="err-session"
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
            assert unrealized == 25.0

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

        # 4. Short position with known mid price
        ticker_short = "KXTEST-SEED-SHORT"
        short_market = tracker.get_or_create_market(ticker_short)
        short_market.last_mid_price = 48.0
        tracker.seed_initial_inventory(ticker_short, -5, cost_basis_cents=None)
        assert tracker.get_open_inventory(ticker_short) == -5
        assert len(short_market.open_lots) == 1
        assert short_market.open_lots[0].price_cents == 48.0
        assert short_market.open_lots[0].is_uncosted is False
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

    def test_hydrate_positions_seeds_inventory(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)

        mock_payload = {
            "market_positions": [
                {"ticker": "KXTEST-HYD1", "position_fp": "10.0", "market_exposure": 450.0},
                {"ticker": "KXTEST-HYD2", "position": -5, "total_traded": "invalid"},
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

        # Verify periodic reconciliation (is_startup=False) does not seed lots
        im.pnl_tracker.markets["KXTEST-RECON"] = MarketPnL(ticker="KXTEST-RECON")
        mock_payload_recon = {
            "market_positions": [{"ticker": "KXTEST-RECON", "position": 8}]
        }
        mock_resp.json.return_value = mock_payload_recon
        with patch("data.inventory_manager.requests.get", return_value=mock_resp), \
             patch("data.inventory_manager.get_auth_headers", return_value={}):
            im._hydrate_positions(is_startup=False)

        assert im.get_position("KXTEST-RECON") == 8
        # PnL tracker must NOT have been seeded during periodic reconciliation
        assert im.pnl_tracker.get_open_inventory("KXTEST-RECON") == 0

    def test_handle_fill_deducts_fees_from_balance(self):
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 10000
        ticker = "KXTEST-FEE-BAL"

        # Buy 2 @ 40c with fee 1.8c -> total cost = 2*40 + 2 = 82c -> balance = 9918c
        im._handle_fill({
            "market_ticker": ticker,
            "action": "buy",
            "side": "yes",
            "count": 2,
            "price": 40,
            "fee_cents": 1.8
        })
        assert im.balance_cents == 9918

        # Sell 2 @ 60c with fee 1.2c -> proceeds = 2*60 - 1 = 119c -> balance = 10037c
        im._handle_fill({
            "market_ticker": ticker,
            "action": "sell",
            "side": "yes",
            "count": 2,
            "price": 60,
            "fee_cents": 1.2
        })
        assert im.balance_cents == 10037

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

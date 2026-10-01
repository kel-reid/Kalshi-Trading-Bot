"""
Unit Tests for Strategy Lifecycle, Quoting Engine PnL Snapshots, and Escalation

Tests periodic PnL snapshots, logging, market rotation snapshot error handling,
quiescence on stop, startup hydration gating, and escalation to emergency KillSwitch.
"""

import asyncio
import pytest
import time
from unittest.mock import MagicMock, AsyncMock, patch

from data.inventory_manager import InventoryManager
from strategy.market_maker import AvellanedaStoikovBot


class TestMarketMakerPnLLifecycle:
    """Verifies MarketMaker PnL snapshot triggers, lifecycle events, and error handling."""

    @pytest.mark.asyncio
    async def test_tick_triggers_periodic_pnl_snapshot(self):
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-TICK", min_spread=2)
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
        bot = AvellanedaStoikovBot(ticker="KXTEST-MM-ROT1")
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.ob_manager.unsubscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()
        bot.om.record_pnl_snapshot_async = AsyncMock()
        bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

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

    @pytest.mark.asyncio
    async def test_stop_cancels_untracked_quote_and_clears_id_only_on_confirmation(self):
        """Verify stop() cancels untracked quotes via om.cancel_order and retains IDs/returns False if cancel fails."""
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

    @pytest.mark.asyncio
    async def test_rotate_market_escalates_to_kill_switch_when_active_orders_remain(self):
        """Verify rotate_market() triggers KillSwitch and aborts rotation when active orders remain in om.active_orders."""
        bot = AvellanedaStoikovBot(ticker="OLD-TICKER")
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.om.active_orders = {"stray-order-1": {"kalshi_order_id": "k1"}}
        bot.ob_manager.unsubscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()

        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock()

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            rotated = await bot.rotate_market("NEW-TICKER")

        # Must trigger KillSwitch to cancel the dangling orders on the old ticker
        mock_killer.trigger.assert_awaited_once()
        # Rotation must be aborted to protect the bot
        assert rotated is False
        assert bot.ticker == "OLD-TICKER"
        bot.ob_manager.unsubscribe.assert_not_awaited()
        bot.ob_manager.subscribe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotate_market_cancels_untracked_quote_and_clears_id_only_on_confirmation(self):
        """Verify rotate_market() cancels untracked quotes via om.cancel_order during escalation and aborts rotation."""
        bot = AvellanedaStoikovBot(ticker="OLD-TICKER")
        bot.current_bid_id = "untracked-bid-42"
        bot.current_bid_price = 45
        bot.om.active_orders = {}  # Untracked in om.active_orders
        bot._cancel_all_quotes = AsyncMock(return_value=False)
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.ob_manager.unsubscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()

        mock_killer = MagicMock()
        mock_killer.trigger = AsyncMock()

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            rotated = await bot.rotate_market("NEW-TICKER")

        # Must trigger KillSwitch and explicitly cancel untracked quote
        mock_killer.trigger.assert_awaited_once()
        bot.om.cancel_order.assert_awaited_once_with("untracked-bid-42")
        # Clears quote id on confirmation
        assert bot.current_bid_id is None
        assert bot.current_bid_price is None
        # Must abort rotation safely
        assert rotated is False
        assert bot.ticker == "OLD-TICKER"
        bot.ob_manager.unsubscribe.assert_not_awaited()
        bot.ob_manager.subscribe.assert_not_awaited()

        # Scenario 2: cancel_order fails on exchange for untracked ask quote
        bot.current_ask_id = "untracked-ask-99"
        bot.current_ask_price = 55
        bot.om.cancel_order = AsyncMock(return_value=False)
        mock_killer.trigger.reset_mock()

        with patch("execution.kill_switch.KillSwitch", return_value=mock_killer):
            rotated_fail = await bot.rotate_market("NEW-TICKER-2")

        mock_killer.trigger.assert_awaited_once()
        bot.om.cancel_order.assert_awaited_once_with("untracked-ask-99")
        # ID is retained when cancellation was not confirmed
        assert bot.current_ask_id == "untracked-ask-99"
        assert bot.current_ask_price == 55
        assert rotated_fail is False

    @pytest.mark.asyncio
    async def test_tick_schedules_snapshot_after_updating_current_mid(self):
        """Verify _tick() updates orderbook mid price before scheduling periodic snapshot."""
        bot = AvellanedaStoikovBot(ticker="KXTEST-SNAP-TIMING", min_spread=2)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(40, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))  # Mid = 50c
        bot._update_quotes = AsyncMock()
        bot._last_pnl_snapshot = 0.0  # Force snapshot interval to trigger

        # Seed open inventory lot of 10 contracts @ 40c
        bot.inv_manager.positions[bot.ticker] = 10
        bot.inv_manager.pnl_tracker.record_fill(bot.ticker, "buy", "yes", 10, 40)
        # Prior to tick, unrealized PnL is 0 because mid price hasn't been set yet
        assert bot.inv_manager.get_unrealized_pnl(bot.ticker) == 0.0

        snapshot_args = []
        async def mock_record_snapshot(**kwargs):
            snapshot_args.append(kwargs)

        bot.om.record_pnl_snapshot_async = AsyncMock(side_effect=mock_record_snapshot)

        await bot._tick()

        # Await any background snapshot tasks
        if bot._background_tasks:
            await asyncio.gather(*list(bot._background_tasks), return_exceptions=True)

        assert len(snapshot_args) == 1
        # Mid price was updated to 50c: (50 - 40) * 10 = +100c unrealized PnL
        assert snapshot_args[0]["unrealized_pnl_cents"] == 100.0
        assert snapshot_args[0]["inventory"] == 10

    @pytest.mark.asyncio
    async def test_maybe_schedule_pnl_snapshot_uses_current_ticker_and_inventory(self):
        """Verify _maybe_schedule_pnl_snapshot always reads current ticker and inventory context consistently."""
        bot = AvellanedaStoikovBot(ticker="OLD-TICKER")
        bot.inv_manager.positions["OLD-TICKER"] = 25
        bot.inv_manager.positions["NEW-TICKER"] = 3

        snapshot_args = []
        async def mock_record_snapshot(**kwargs):
            snapshot_args.append(kwargs)

        bot.om.record_pnl_snapshot_async = AsyncMock(side_effect=mock_record_snapshot)

        # After rotation, bot.ticker is "NEW-TICKER"
        bot.ticker = "NEW-TICKER"
        bot._last_pnl_snapshot = 0.0

        # Call snapshot helper without passing inventory (or passing stale old inventory)
        bot._maybe_schedule_pnl_snapshot(now=time.time(), inventory=25)

        if bot._background_tasks:
            await asyncio.gather(*list(bot._background_tasks), return_exceptions=True)

        assert len(snapshot_args) == 1
        # Snapshot must be tagged with NEW-TICKER and its actual inventory (3), not the old inventory (25)
        assert snapshot_args[0]["ticker"] == "NEW-TICKER"
        assert snapshot_args[0]["inventory"] == 3

    @pytest.mark.asyncio
    async def test_startup_hydration_commits_balance_only_after_positions_validate_atomically(self):
        """Verify startup hydration does not mutate balance if positions validation fails."""
        mock_ws = MagicMock()
        im = InventoryManager(mock_ws)
        im.balance_cents = 1234

        # Malformed positions payload (missing ticker)
        bad_positions = [{"position": 5, "fees_paid": 0}]
        with patch.object(im, "_fetch_balance", return_value=99999), \
             patch.object(im, "_fetch_positions", return_value=bad_positions):
            success = await im.hydrate(is_startup=True)
            assert success is False
            # Balance must NOT be mutated if positions fail validation
            assert im.balance_cents == 1234

        # Valid payload commits both balance and positions
        good_positions = [{"ticker": "KXTEST-ATOMIC", "position": 10, "fees_paid": 0}]
        with patch.object(im, "_fetch_balance", return_value=99999), \
             patch.object(im, "_fetch_positions", return_value=good_positions):
            success = await im.hydrate(is_startup=True)
            assert success is True
            assert im.balance_cents == 99999
            assert im.get_position("KXTEST-ATOMIC") == 10

    @pytest.mark.asyncio
    async def test_market_maker_serializes_snapshots_and_quiesces_on_stop(self):
        """Verify market maker serializes snapshot tasks and cancels sync task on stop."""
        bot = AvellanedaStoikovBot(ticker="KXTEST-SERIALIZE")
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.om.record_pnl_snapshot_async = AsyncMock()

        # Simulate running sync task
        mock_sync = asyncio.create_task(asyncio.sleep(10))
        bot._sync_task = mock_sync
        bot._background_tasks.add(mock_sync)

        # Simulate running snapshot task
        mock_snap = asyncio.create_task(asyncio.sleep(0.01))
        bot._snapshot_task = mock_snap
        bot._background_tasks.add(mock_snap)

        # _maybe_schedule_pnl_snapshot should skip if snapshot task is already in-flight
        bot._last_pnl_snapshot = 0.0
        bot._maybe_schedule_pnl_snapshot(now=time.time())
        bot.om.record_pnl_snapshot_async.assert_not_called()

        # Stop should cancel sync task and await snapshot task
        await bot.stop()
        assert mock_sync.cancelled()
        assert mock_snap.done()
        assert not mock_snap.cancelled()
        assert bot.om.record_pnl_snapshot_async.call_count == 1

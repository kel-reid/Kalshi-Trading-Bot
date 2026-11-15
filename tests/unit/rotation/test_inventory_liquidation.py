"""
Unit tests for AvellanedaStoikovBot inventory liquidation, order slicing,
position flipping guards, and execution confirmation barriers.
"""

import asyncio
import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock

from strategy.market_maker import AvellanedaStoikovBot


@pytest.mark.asyncio
async def test_liquidate_inventory_positive_position_sells_yes():
    """Verify liquidate_inventory with positive position places sell yes order crossing best bid."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    # Orderbook has best bid at 45c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Inventory starts at 5, decrements to 0 on order fill
    current_pos = 5
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=lambda t: {0: current_pos})
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})

    async def mock_place_order(**kwargs):
        nonlocal current_pos
        current_pos = 0
        return "order-liq-1"

    bot.om.place_order = AsyncMock(side_effect=mock_place_order)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER")

    assert success is True
    bot.om.place_order.assert_awaited_once_with(
        ticker="TEST-TICKER",
        side="yes",
        action="sell",
        count=5,
        price=45,
        reduce_only=True, time_in_force="immediate_or_cancel", exchange_index=0,
    )


@pytest.mark.asyncio
async def test_liquidate_inventory_negative_position_buys_yes():
    """Verify liquidate_inventory with negative position places buy yes order crossing best ask."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    # Orderbook has best ask at 55c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Short position of -8 contracts, decrements to 0 on fill
    current_pos = -8
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=lambda t: {0: current_pos})
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "8"})

    async def mock_place_order(**kwargs):
        nonlocal current_pos
        current_pos = 0
        return "order-liq-2"

    bot.om.place_order = AsyncMock(side_effect=mock_place_order)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER")

    assert success is True
    bot.om.place_order.assert_awaited_once_with(
        ticker="TEST-TICKER",
        side="yes",
        action="buy",
        count=8,
        price=55,
        reduce_only=True, time_in_force="immediate_or_cancel", exchange_index=0,
    )


@pytest.mark.asyncio
async def test_liquidate_inventory_caps_slice_at_max_order_contracts():
    """Verify liquidate_inventory caps individual liquidation order counts to max_order_contracts."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4, max_order_contracts=10)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 50))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 50))

    # 25 contracts long: sliced into 10, 10, 5
    current_pos = 25
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=lambda t: {0: current_pos})
    bot.om.get_order_status = AsyncMock(side_effect=lambda order: {
        "status": "executed", "fill_count_fp": str(bot.om.place_order.await_args.kwargs["count"])
    })

    async def mock_place_order(**kwargs):
        nonlocal current_pos
        current_pos -= kwargs["count"]
        return f"order-{current_pos}"

    bot.om.place_order = AsyncMock(side_effect=mock_place_order)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is True
    assert bot.om.place_order.await_count == 3
    # Check slice counts
    assert bot.om.place_order.await_args_list[0].kwargs["count"] == 10
    assert bot.om.place_order.await_args_list[1].kwargs["count"] == 10
    assert bot.om.place_order.await_args_list[2].kwargs["count"] == 5


@pytest.mark.asyncio
async def test_liquidate_inventory_cancels_resting_order_if_unfilled():
    """Verify liquidate_inventory cancels any resting order remaining in active_orders."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-unfilled")
    bot.om.cancel_order = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(side_effect=[
        {"status": "canceled", "fill_count_fp": "0"},
        {"status": "executed", "fill_count_fp": "5"},
    ])

    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # The first placement unexpectedly remains resting in active_orders.
    async def place(**kwargs):
        if not bot.om.active_orders and bot.om.place_order.await_count == 1:
            bot.om.active_orders["order-unfilled"] = {"ticker": "TEST-TICKER"}
        return "order-unfilled"
    bot.om.place_order.side_effect = place

    # The first IOC is canceled unfilled; the second fills completely.
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {0: 5}, {0: 5}, {}])

    async def cancel(order_id):
        bot.om.active_orders.pop(order_id)
        return True

    bot.om.cancel_order.side_effect = cancel

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    bot.om.cancel_order.assert_awaited_once_with("order-unfilled")


@pytest.mark.asyncio
async def test_liquidate_inventory_flat_is_noop():
    """Verify flat inventory is confirmed after withdrawing quotes."""
    bot = AvellanedaStoikovBot(ticker="FLAT-TICKER")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock()
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})

    success = await bot.liquidate_inventory(ticker="FLAT-TICKER")

    assert success is True
    bot.om.place_order.assert_not_called()
    bot._cancel_all_quotes.assert_awaited_once_with(ticker="FLAT-TICKER")
    bot.inv_manager._fetch_ticker_positions.assert_called_once_with("FLAT-TICKER")


@pytest.mark.asyncio
async def test_liquidate_inventory_fractional_subcontract_is_noop():
    """Fractional positions are not flat and require a confirmed close."""
    bot = AvellanedaStoikovBot(ticker="FRACTIONAL-TICKER")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock()
    bot.inv_manager.get_position = MagicMock(return_value=0.02)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 0.02})
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)

    success = await bot.liquidate_inventory(ticker="FRACTIONAL-TICKER")

    assert success is False
    bot.om.place_order.assert_not_called()
    bot._cancel_all_quotes.assert_awaited_once_with(ticker="FRACTIONAL-TICKER")

    bot.inv_manager.get_position = MagicMock(return_value=-0.02)
    bot.inv_manager._fetch_ticker_positions.return_value = {0: -0.02}
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)
    success_neg = await bot.liquidate_inventory(ticker="FRACTIONAL-TICKER")
    assert success_neg is False
    bot.om.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_liquidate_inventory_returns_false_when_retries_exhausted():
    """Verify liquidate_inventory returns False if position cannot be flattened after max_retries."""
    bot = AvellanedaStoikovBot(ticker="STUBBORN-TICKER")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-fail")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "count": 10, "remaining_count": 10, "fill_count_fp": "0.00"})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Inventory remains 10 contracts throughout
    bot.inv_manager.get_position = MagicMock(return_value=10)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 10})

    success = await bot.liquidate_inventory(ticker="STUBBORN-TICKER", max_retries=2)

    assert success is False
    assert bot.om.place_order.await_count == 2


@pytest.mark.asyncio
async def test_liquidation_financial_accounting_invariants():
    """
    Adversarial Invariant Test:
    Total PnL = Realized PnL + Unrealized PnL holds strictly before, during, and after liquidation.
    Net inventory becomes 0, unrealized PnL becomes 0, and all realized gains/losses and fees are preserved.
    """
    ticker = "INVARIANT-LIQ-TICKER"
    bot = AvellanedaStoikovBot(ticker=ticker, gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    # 1. Establish initial position: Bought 10 contracts @ 40c with 5c entry fee
    bot.inv_manager.pnl_tracker.record_fill(ticker, action="buy", side="yes", count=10, price_cents=40.0, fee_cents=5.0)
    bot.inv_manager.pnl_tracker.update_mid_price(ticker, 45.0)

    # Pre-liquidation verification:
    pre_summary = bot.inv_manager.pnl_tracker.get_market_summary(ticker)
    assert pre_summary["net_inventory"] == 10
    assert round(pre_summary["realized_pnl_cents"] + pre_summary["unrealized_pnl_cents"], 4) == round(pre_summary["total_pnl_cents"], 4)

    # Best bid is 45c
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 20))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 20))

    # Stateful position & fill execution during liquidation
    current_pos = 10
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=lambda t: {0: current_pos})
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "10"})

    async def mock_liquidation_fill(**kwargs):
        nonlocal current_pos
        count = kwargs["count"]
        price = kwargs["price"]
        current_pos -= count
        # Record liquidation fill in PnL tracker (with 2c exit fee)
        bot.inv_manager.pnl_tracker.record_fill(ticker, action="sell", side="yes", count=count, price_cents=price, fee_cents=2.0)
        return "liq-fill-1"

    bot.om.place_order = AsyncMock(side_effect=mock_liquidation_fill)

    # Execute liquidation
    success = await bot.liquidate_inventory(ticker=ticker)
    assert success is True

    # Post-liquidation verification:
    post_summary = bot.inv_manager.pnl_tracker.get_market_summary(ticker)
    assert post_summary["net_inventory"] == 0
    assert post_summary["unrealized_pnl_cents"] == 0.0
    # Invariant: Total PnL = Realized PnL + Unrealized PnL
    assert round(post_summary["realized_pnl_cents"] + post_summary["unrealized_pnl_cents"], 4) == round(post_summary["total_pnl_cents"], 4)
    # Total PnL = gross profit (10 * (45 - 40) = +50c) - total fees (5 + 2 = 7c) = +43c
    assert round(post_summary["total_pnl_cents"], 4) == 43.0
    assert round(post_summary["realized_pnl_cents"], 4) == 43.0
    assert round(post_summary["total_fees_cents"], 4) == 7.0


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_cancel_order_fails():
    """Verify liquidate_inventory immediately aborts and returns False if cancelling a resting order fails."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-stuck")
    bot.om.cancel_order = AsyncMock(return_value=False)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    async def place(**kwargs):
        bot.om.active_orders["order-stuck"] = {"ticker": "TEST-TICKER"}
        return "order-stuck"
    bot.om.place_order.side_effect = place
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.cancel_order.assert_awaited_once_with("order-stuck")
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_reports_false_when_liquidation_fails():
    """Verify stop() returns False if liquidation of open inventory fails on shutdown."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot._ws_task = asyncio.create_task(asyncio.Event().wait())
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.liquidate_inventory = AsyncMock(return_value=False)
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.stop()

    assert success is False
    bot.liquidate_inventory.assert_awaited_once_with("TEST-TICKER")
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_place_order_fails():
    """Verify liquidate_inventory immediately aborts and returns False if place_order returns None."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value=None)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_on_position_flip():
    """Verify liquidate_inventory aborts if an overfill causes the position sign to flip."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-flip")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Position was +5, but unexpectedly flipped to -3 after execution
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {0: -3}])
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_cancel_all_quotes_fails():
    """Verify liquidate_inventory immediately aborts and returns False if _cancel_all_quotes fails."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=False)
    bot.om.place_order = AsyncMock()
    bot.inv_manager.get_position = MagicMock(return_value=15)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot._cancel_all_quotes.assert_awaited_once_with(ticker="TEST-TICKER")
    bot.om.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_liquidate_inventory_continues_slicing_when_progress_is_made():
    """Verify liquidate_inventory continues past max_retries slices when each slice successfully reduces position."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4, max_order_contracts=10)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 50))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 50))

    # Initial position 35 contracts. With max_order_contracts=10, takes 4 slices: 10, 10, 10, 5.
    current_pos = 35
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=lambda t: {0: current_pos})
    bot.om.get_order_status = AsyncMock(side_effect=lambda order: {
        "status": "executed", "fill_count_fp": str(bot.om.place_order.await_args.kwargs["count"])
    })

    async def mock_place_order(**kwargs):
        nonlocal current_pos
        current_pos -= kwargs["count"]
        return f"order-{current_pos}"

    bot.om.place_order = AsyncMock(side_effect=mock_place_order)

    # max_retries is set to 2; total slices needed is 4 (4 > max_retries).
    # Since progress is made on each slice, it must succeed without aborting.
    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    assert bot.om.place_order.await_count == 4
    assert [call.kwargs["count"] for call in bot.om.place_order.await_args_list] == [10, 10, 10, 5]


@pytest.mark.asyncio
async def test_inactive_market_recovery_retries_liquidation_and_alerts_on_failure():
    """Verify that when market is inactive and position is nonzero, liquidation is retried and alerts on failure."""
    bot = AvellanedaStoikovBot(ticker="INACTIVE_TICKER", gamma=0.5, min_spread=4, order_size=1, auto_rotate=False)
    bot._market_inactive = True
    bot._last_inactive_retry = time.time() - 10.0  # Elapse the retry interval
    bot.inv_manager.get_position = MagicMock(return_value=8)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.liquidate_inventory = AsyncMock(return_value=False)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        await bot._tick()

        bot.liquidate_inventory.assert_awaited_once_with("INACTIVE_TICKER")
        mock_alert.assert_awaited_once()
        assert "Inactive market liquidation retry failed for INACTIVE_TICKER" in mock_alert.await_args[0][0]


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_reconciliation_returns_none():
    """Verify liquidate_inventory aborts and returns False if reconcile_resting_orders returns None."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-1")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=None)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot.om.place_order.assert_awaited_once()
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="TEST-TICKER")


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_reconciliation_raises_exception():
    """Verify liquidate_inventory catches exceptions from reconcile_resting_orders and returns False."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-1")
    bot.om.reconcile_resting_orders = AsyncMock(side_effect=RuntimeError("Exchange connection dropped"))
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot.om.place_order.assert_awaited_once()
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="TEST-TICKER")


@pytest.mark.asyncio
async def test_liquidate_inventory_awaits_websocket_fill_when_exchange_confirms_execution():
    """A delivered fill and matching terminal order and REST position confirm liquidation."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-exec-1")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    bot.inv_manager.get_position = MagicMock(return_value=5)

    async def delivered_status(order_id):
        bot.inv_manager.get_position.return_value = 0
        bot.inv_manager._fill_count += 1
        return {"status": "executed", "fill_count_fp": "5"}

    bot.om.get_order_status.side_effect = delivered_status
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {}])

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    bot.om.get_order_status.assert_awaited_once_with("order-exec-1")
    assert bot.inv_manager.get_position("TEST-TICKER") == 0


@pytest.mark.asyncio
async def test_liquidate_inventory_hydrates_rest_positions_when_websocket_fill_delayed():
    """A delayed local fill blocks success until local reconciliation catches up."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-exec-2")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    current_pos = 5
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)

    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {}, {}])

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2
    assert bot.inv_manager.get_position("TEST-TICKER") == 5
    current_pos = 0
    assert await bot.liquidate_inventory(ticker="TEST-TICKER") is True
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_order_outcome_unconfirmed():
    """A flat REST position cannot substitute for a terminal order outcome."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-unconfirmed")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value=None)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {}])

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.get_order_status.assert_awaited_once_with("order-unconfirmed")
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_execution_confirmed_but_hydrated_position_unchanged():
    """Verify liquidate_inventory aborts when execution is confirmed on exchange but refreshed position is stale."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-stale-hydrate")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)  # Position remains 5
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.get_order_status.assert_awaited_once_with("order-stale-hydrate")
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_liquidate_inventory_parses_fixed_point_fill_count():
    """Verify liquidate_inventory correctly recognizes execution from fill_count_fp string."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-fp-fill")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(side_effect=[
        {"status": "canceled", "initial_count_fp": "10.00", "remaining_count_fp": "5.00", "fill_count_fp": "5.00"},
        {"status": "executed", "fill_count_fp": "5.00"},
    ])
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    bot.inv_manager.get_position = MagicMock(side_effect=[10, 0])
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 10}, {0: 5}, {0: 5}, {}])

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    assert bot.om.get_order_status.await_count == 2
    assert [call.kwargs["count"] for call in bot.om.place_order.await_args_list] == [10, 5]


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_on_malformed_order_quantities():
    """Verify liquidate_inventory fails closed when quantity fields cannot be parsed as numeric."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-corrupt")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={
        "status": "canceled",
        "fill_count_fp": "not-a-number",
    })
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.get_order_status.assert_awaited_once_with("order-corrupt")
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_liquidate_inventory_directionally_rounds_subcent_prices():
    """Verify liquidate_inventory floors bids for sells and ceils asks for buys to guarantee crossing."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.place_order = AsyncMock(return_value="order-subcent")

    # Long position (sells into 45.6c bid -> floors to 45c)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.6, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.4, 10))
    bot.inv_manager.get_position = MagicMock(side_effect=[5, 0])
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: 5}, {}])
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "5"})

    success = await bot.liquidate_inventory(ticker="TEST-TICKER")
    assert success is True
    bot.om.place_order.assert_awaited_once_with(
        ticker="TEST-TICKER",
        side="yes",
        action="sell",
        count=5,
        price=45,
        reduce_only=True, time_in_force="immediate_or_cancel", exchange_index=0,
    )

    # Short position (buys into 55.4c ask -> ceils to 56c)
    bot.om.place_order.reset_mock()
    bot.inv_manager.get_position = MagicMock(side_effect=[-5, 0])
    bot.inv_manager._fetch_ticker_positions.side_effect = [{0: -5}, {}]

    success_short = await bot.liquidate_inventory(ticker="TEST-TICKER")
    assert success_short is True
    bot.om.place_order.assert_awaited_once_with(
        ticker="TEST-TICKER",
        side="yes",
        action="buy",
        count=5,
        price=56,
        reduce_only=True, time_in_force="immediate_or_cancel", exchange_index=0,
    )


@pytest.mark.asyncio
async def test_liquidate_inventory_refuses_when_opposing_book_is_empty():
    """Verify liquidate_inventory fails closed without sending 1c/99c orders when opposing book is empty."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock()

    # Long position with empty bids
    bot.inv_manager.get_position = MagicMock(return_value=5)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: 5})
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=(50.0, 10))

    success = await bot.liquidate_inventory(ticker="TEST-TICKER")
    assert success is False
    bot.om.place_order.assert_not_awaited()

    # Short position with empty asks
    bot.inv_manager.get_position = MagicMock(return_value=-5)
    bot.inv_manager._fetch_ticker_positions.return_value = {0: -5}
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)

    success_short = await bot.liquidate_inventory(ticker="TEST-TICKER")
    assert success_short is False
    bot.om.place_order.assert_not_awaited()

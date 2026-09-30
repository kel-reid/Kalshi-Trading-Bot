"""
Unit tests for AvellanedaStoikovBot market rotation, quote cancellation, and starvation alerting.
"""

import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock

from strategy.market_maker import AvellanedaStoikovBot


@pytest.mark.asyncio
async def test_bot_rotate_market():
    """Verify that rotate_market cancels quotes, resubscribes, and sends alert."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()

    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        rotated = await bot.rotate_market("NEW-TICKER")

        assert rotated is True
        bot._cancel_all_quotes.assert_awaited_once()
        bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="OLD-TICKER")
        bot.ob_manager.unsubscribe.assert_awaited_once_with(["OLD-TICKER"])
        bot.ob_manager.subscribe.assert_awaited_once_with(["NEW-TICKER"])
        assert bot.ticker == "NEW-TICKER"
        mock_alert.assert_awaited_once()
        assert "NEW-TICKER" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_bot_rotate_market_aborts_when_quote_cancel_fails():
    """Verify that rotate_market aborts without changing subscription if quote cancellation fails."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=False)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is False
    assert bot.ticker == "OLD-TICKER"
    bot.ob_manager.unsubscribe.assert_not_awaited()
    bot.ob_manager.subscribe.assert_not_awaited()
    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_rotate_market_aborts_when_reconciliation_fails():
    """Verify that rotate_market escalates to kill switch and aborts if resting-order reconciliation fails."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=None)
    bot._escalate_to_kill_switch = AsyncMock(return_value=False)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is False
    assert bot.ticker == "OLD-TICKER"
    bot._cancel_all_quotes.assert_awaited_once()
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="OLD-TICKER")
    bot._escalate_to_kill_switch.assert_awaited_once()
    bot.ob_manager.unsubscribe.assert_not_awaited()
    bot.ob_manager.subscribe.assert_not_awaited()
    mock_alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_quotes_preserves_failed_order_ids():
    """Verify that only successfully cancelled orders have their state cleared."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot.current_bid_id = "bid-1"
    bot.current_bid_price = 45
    bot.current_ask_id = "ask-1"
    bot.current_ask_price = 55
    bot.om.cancel_order = AsyncMock(side_effect=[False, True])
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    cancelled = await bot._cancel_all_quotes()

    assert cancelled is False
    assert bot.current_bid_id == "bid-1"
    assert bot.current_bid_price == 45
    assert bot.current_ask_id is None
    assert bot.current_ask_price is None


@pytest.mark.asyncio
async def test_cancel_all_quotes_sweeps_exchange_even_when_no_tracked_quotes():
    """Verify that _cancel_all_quotes executes resting-order sweep even when no quotes are tracked locally."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot.current_bid_id = None
    bot.current_ask_id = None
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    cancelled = await bot._cancel_all_quotes()

    assert cancelled is True
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="MOCK_TICKER")


@pytest.mark.asyncio
async def test_cancel_all_quotes_returns_false_when_reconciliation_fails():
    """Verify that _cancel_all_quotes returns False if resting-order reconciliation fails."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot.current_bid_id = "bid-1"
    bot.current_ask_id = "ask-1"
    bot.om.cancel_order = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=None)

    cancelled = await bot._cancel_all_quotes()

    assert cancelled is False
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="MOCK_TICKER")


@pytest.mark.asyncio
async def test_bot_unknown_market_status_does_not_rotate():
    """Verify that indeterminate market status check retains the current market without halting."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(54, 10))
    bot._cancel_all_quotes = AsyncMock()
    bot._update_quotes = AsyncMock()
    bot._last_market_status_check = 0

    with patch("strategy.market_maker.is_market_active_async", new=AsyncMock(return_value=None)) as mock_status, \
         patch("strategy.market_maker.discover_active_market_async", new=AsyncMock()) as mock_discover:
        await bot._tick()

    mock_status.assert_awaited_once_with("MOCK_TICKER")
    mock_discover.assert_not_awaited()
    bot._cancel_all_quotes.assert_not_awaited()
    bot._update_quotes.assert_awaited_once_with(50, 54)
    assert bot._market_inactive is False


@pytest.mark.asyncio
async def test_bot_inactive_market_with_no_replacement_halts_quoting():
    """Verify that an inactive market with no replacement cancels quotes and halts further evaluation."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(54, 10))
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._update_quotes = AsyncMock()
    bot._last_market_status_check = 0

    with patch("strategy.market_maker.is_market_active_async", new=AsyncMock(return_value=False)) as mock_status, \
         patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value=None)) as mock_discover:
        await bot._tick()
        await bot._tick()

    mock_status.assert_awaited_once_with("MOCK_TICKER")
    mock_discover.assert_awaited_once_with(
        target_preference="MOCK_TICKER",
        exclude_tickers=["MOCK_TICKER"],
        min_mid_price=bot.min_mid_price,
        max_mid_price=bot.max_mid_price,
    )
    bot._cancel_all_quotes.assert_awaited_once()
    bot._update_quotes.assert_not_awaited()
    assert bot._market_inactive is True


@pytest.mark.asyncio
async def test_bot_starvation_alerting():
    """Verify that orderbook starvation triggers an alert after timeout and resets on recovery."""
    bot = AvellanedaStoikovBot(
        ticker="MOCK_TICKER",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        starvation_timeout=900.0,
        auto_rotate=False
    )
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._update_quotes = AsyncMock()

    # 1. Orderbook empty: first tick sets starvation start time
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        await bot._tick()
        assert bot._starvation_start_time is not None
        assert bot._starvation_alert_sent is False
        mock_alert.assert_not_called()

        # 2. Simulate 901 seconds elapsed (past 15 minute threshold)
        bot._starvation_start_time = time.time() - 901
        await bot._tick()
        assert bot._starvation_alert_sent is True
        mock_alert.assert_awaited_once()
        assert "STARVATION ALERT" in mock_alert.call_args[0][0]

        # 3. Next tick while still starved: do NOT spam duplicate alert
        mock_alert.reset_mock()
        await bot._tick()
        mock_alert.assert_not_called()

        # 4. Liquidity returns: alert restoration and reset timers
        bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(54, 10))
        await bot._tick()
        assert bot._starvation_alert_sent is False
        assert bot._starvation_start_time is None
        mock_alert.assert_awaited_once()
        assert "Orderbook Restored" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_starvation_triggers_auto_rotation():
    """Verify that when starvation timeout is reached and auto_rotate is True, rotation is initiated."""
    bot = AvellanedaStoikovBot(
        ticker="DEAD_TICKER",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        starvation_timeout=900.0,
        auto_rotate=True
    )
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)
    bot.rotate_market = AsyncMock(return_value=True)
    bot._cancel_all_quotes = AsyncMock(return_value=True)

    # Fast forward starvation time to > 900s
    bot._starvation_start_time = time.time() - 905

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert, \
         patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value="REPLACEMENT_TICKER")) as mock_discover:
        await bot._tick()

        mock_alert.assert_awaited_once()
        mock_discover.assert_awaited_once_with(
            target_preference="DEAD_TICKER",
            exclude_tickers=["DEAD_TICKER"],
            min_mid_price=bot.min_mid_price,
            max_mid_price=bot.max_mid_price,
        )
        bot.rotate_market.assert_awaited_once_with("REPLACEMENT_TICKER")
        assert bot._market_inactive is False


@pytest.mark.asyncio
async def test_bot_retries_quote_cancellation_when_market_inactive():
    """Verify that if quotes remain active in an inactive market, cancellation is retried each tick."""
    bot = AvellanedaStoikovBot(
        ticker="INACTIVE_TICKER", gamma=0.5, min_spread=4, order_size=1, auto_rotate=False
    )
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._market_inactive = True
    bot.current_bid_id = "unconfirmed-bid-1"
    bot.current_ask_id = None
    bot._cancel_all_quotes = AsyncMock(return_value=False)

    await bot._tick()

    bot._cancel_all_quotes.assert_awaited_once()
    assert bot.current_bid_id == "unconfirmed-bid-1"
    assert bot._market_inactive is True


@pytest.mark.asyncio
async def test_bot_retries_discovery_and_recovers_from_inactive():
    """Verify that when market is inactive, discovery is retried after interval and successfully recovers."""
    bot = AvellanedaStoikovBot(ticker="INACTIVE_TICKER", gamma=0.5, min_spread=4, order_size=1, auto_rotate=True)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._market_inactive = True
    bot._last_inactive_retry = time.time() - 10.0  # Elapse the 5s retry interval
    bot.rotate_market = AsyncMock(return_value=True)

    with patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value="RECOVERED_TICKER")) as mock_discover:
        await bot._tick()

        mock_discover.assert_awaited_once_with(
            target_preference="INACTIVE_TICKER",
            exclude_tickers=["INACTIVE_TICKER"],
            min_mid_price=bot.min_mid_price,
            max_mid_price=bot.max_mid_price,
        )
        bot.rotate_market.assert_awaited_once_with("RECOVERED_TICKER")
        assert bot._market_inactive is False


@pytest.mark.asyncio
async def test_starvation_rotation_failure_sets_retry_timestamp():
    """Verify that starvation rotation failure sets _last_inactive_retry allowing subsequent recovery."""
    bot = AvellanedaStoikovBot(
        ticker="DEAD_TICKER",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        starvation_timeout=900.0,
        auto_rotate=True
    )
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)
    bot.rotate_market = AsyncMock(return_value=False)  # Rotation fails (e.g. cancel failed)
    bot._starvation_start_time = time.time() - 905

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock), \
         patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value="REPLACEMENT_TICKER")):
        now_before = time.time()
        await bot._tick()

        assert bot._market_inactive is True
        assert bot._last_inactive_retry >= now_before


@pytest.mark.asyncio
async def test_rotate_market_liquidates_open_inventory_before_switching():
    """Verify that rotate_market liquidates open inventory on old_ticker before switching."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.liquidate_inventory = AsyncMock(return_value=True)

    # Old ticker has 10 contracts open; new ticker is 0
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: 10 if t == "OLD-TICKER" else 0)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock):
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is True
    bot.liquidate_inventory.assert_awaited_once_with(ticker="OLD-TICKER")
    bot.ob_manager.unsubscribe.assert_awaited_once_with(["OLD-TICKER"])
    bot.ob_manager.subscribe.assert_awaited_once_with(["NEW-TICKER"])
    assert bot.ticker == "NEW-TICKER"


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
    bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "count": 5, "remaining_count": 5})

    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Order remained resting in active_orders
    bot.om.active_orders["order-unfilled"] = {"ticker": "TEST-TICKER"}

    # Inventory remains 5 on attempt 1, then goes to 0 on attempt 2
    positions = [5, 5, 0]
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: positions.pop(0) if positions else 0)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    bot.om.cancel_order.assert_awaited_once_with("order-unfilled")


@pytest.mark.asyncio
async def test_liquidate_inventory_flat_is_noop():
    """Verify liquidate_inventory returns True immediately when position is already 0."""
    bot = AvellanedaStoikovBot(ticker="FLAT-TICKER")
    bot._cancel_all_quotes = AsyncMock()
    bot.om.place_order = AsyncMock()
    bot.inv_manager.get_position = MagicMock(return_value=0)

    success = await bot.liquidate_inventory(ticker="FLAT-TICKER")

    assert success is True
    bot.om.place_order.assert_not_called()
    bot._cancel_all_quotes.assert_not_called()


@pytest.mark.asyncio
async def test_liquidate_inventory_returns_false_when_retries_exhausted():
    """Verify liquidate_inventory returns False if position cannot be flattened after max_retries."""
    bot = AvellanedaStoikovBot(ticker="STUBBORN-TICKER")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-fail")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "count": 10, "remaining_count": 10})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Inventory remains 10 contracts throughout
    bot.inv_manager.get_position = MagicMock(return_value=10)

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
async def test_rotate_market_aborts_when_liquidation_fails():
    """Verify that rotate_market aborts and does not swap subscriptions when liquidation of old ticker fails."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.liquidate_inventory = AsyncMock(return_value=False)

    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: 10 if t == "OLD-TICKER" else 0)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is False
    bot.liquidate_inventory.assert_awaited_once_with(ticker="OLD-TICKER")
    bot.ob_manager.unsubscribe.assert_not_awaited()
    bot.ob_manager.subscribe.assert_not_awaited()
    assert bot.ticker == "OLD-TICKER"
    mock_alert.assert_awaited_once()
    assert "Rotation aborted" in mock_alert.await_args[0][0]


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

    bot.om.active_orders["order-stuck"] = {"ticker": "TEST-TICKER"}
    bot.inv_manager.get_position = MagicMock(return_value=5)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.cancel_order.assert_awaited_once_with("order-stuck")
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_reports_false_when_liquidation_fails():
    """Verify stop() returns False if liquidation of open inventory fails on shutdown."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.liquidate_inventory = AsyncMock(return_value=False)
    bot.inv_manager.get_position = MagicMock(return_value=5)

    success = await bot.stop()

    assert success is False
    bot.liquidate_inventory.assert_awaited_once_with("TEST-TICKER")


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_place_order_fails():
    """Verify liquidate_inventory immediately aborts and returns False if place_order returns None."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value=None)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)

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
    positions = [5, 5, -3]
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: positions.pop(0) if positions else -3)

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

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=3)

    assert success is False
    bot.om.place_order.assert_awaited_once()
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="TEST-TICKER")


@pytest.mark.asyncio
async def test_liquidate_inventory_awaits_websocket_fill_when_exchange_confirms_execution():
    """Verify liquidate_inventory awaits WebSocket fill dispatch when order status confirms execution."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-exec-1")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "count": 5, "remaining_count": 0})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    # Position is 5 initially. On the 2nd check inside the await loop, WebSocket fill arrives and sets it to 0.
    positions = [5, 5, 5, 0]
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: positions.pop(0) if positions else 0)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    bot.om.get_order_status.assert_awaited_once_with("order-exec-1")


@pytest.mark.asyncio
async def test_liquidate_inventory_hydrates_rest_positions_when_websocket_fill_delayed():
    """Verify liquidate_inventory triggers REST hydration when WebSocket fill fails to arrive within wait window."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-exec-2")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "count": 5, "remaining_count": 0})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))

    current_pos = 5
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: current_pos)

    async def mock_hydrate(is_startup=False):
        nonlocal current_pos
        current_pos = 0
        return True

    bot.inv_manager.hydrate = AsyncMock(side_effect=mock_hydrate)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is True
    bot.inv_manager.hydrate.assert_awaited_once_with(is_startup=False)


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_order_outcome_unconfirmed():
    """Verify liquidate_inventory aborts when both order status and REST hydration fail to confirm execution."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-unconfirmed")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value=None)
    bot.inv_manager.hydrate = AsyncMock(return_value=False)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.get_order_status.assert_awaited_once_with("order-unconfirmed")
    bot.inv_manager.hydrate.assert_awaited_once_with(is_startup=False)


@pytest.mark.asyncio
async def test_liquidate_inventory_aborts_when_execution_confirmed_but_hydrated_position_unchanged():
    """Verify liquidate_inventory aborts when execution is confirmed on exchange but refreshed position is stale."""
    bot = AvellanedaStoikovBot(ticker="TEST-TICKER", gamma=0.5, min_spread=4)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.place_order = AsyncMock(return_value="order-stale-hydrate")
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "count": 5, "remaining_count": 0})
    bot.inv_manager.hydrate = AsyncMock(return_value=True)  # REST call succeeded but returned stale/unchanged data
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45.0, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55.0, 10))
    bot.inv_manager.get_position = MagicMock(return_value=5)  # Position remains 5

    success = await bot.liquidate_inventory(ticker="TEST-TICKER", max_retries=2)

    assert success is False
    bot.om.get_order_status.assert_awaited_once_with("order-stale-hydrate")
    bot.inv_manager.hydrate.assert_awaited_once_with(is_startup=False)






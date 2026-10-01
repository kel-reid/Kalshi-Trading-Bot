"""
Unit tests for AvellanedaStoikovBot market rotation, quote cancellation, and market status checks.
"""

import pytest
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
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot._escalate_to_kill_switch = AsyncMock(return_value=False)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is False
    assert bot.ticker == "OLD-TICKER"
    bot.om.reconcile_resting_orders.assert_awaited_once_with(ticker="OLD-TICKER")
    bot._escalate_to_kill_switch.assert_awaited_once()
    assert "market rotation" in bot._escalate_to_kill_switch.await_args.kwargs["context"]
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
async def test_rotate_market_liquidates_open_inventory_before_switching():
    """Verify that rotate_market liquidates open inventory on old_ticker before switching."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    call_order = []
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock(side_effect=lambda *a, **k: call_order.append("unsubscribe"))
    bot.ob_manager.subscribe = AsyncMock()
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)

    async def _liquidate(**kwargs):
        call_order.append("liquidate")
        return True

    bot.liquidate_inventory = AsyncMock(side_effect=_liquidate)

    # Old ticker has 10 contracts open; new ticker is 0
    bot.inv_manager.get_position = MagicMock(side_effect=lambda t: 10 if t == "OLD-TICKER" else 0)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock):
        rotated = await bot.rotate_market("NEW-TICKER")

    assert rotated is True
    assert call_order == ["liquidate", "unsubscribe"]
    bot.liquidate_inventory.assert_awaited_once_with(ticker="OLD-TICKER")
    bot.ob_manager.unsubscribe.assert_awaited_once_with(["OLD-TICKER"])
    bot.ob_manager.subscribe.assert_awaited_once_with(["NEW-TICKER"])
    assert bot.ticker == "NEW-TICKER"


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

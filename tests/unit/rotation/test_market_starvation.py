"""
Unit tests for AvellanedaStoikovBot orderbook starvation watchdog and inactive market recovery.
"""

import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock

from strategy.market_maker import AvellanedaStoikovBot


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

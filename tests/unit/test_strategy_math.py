"""
Strategy Math Unit Tests

This suite verifies the mathematical correctness of the Avellaneda-Stoikov quoting formula, 
focusing on:
1. Reservation price calculation for neutral, long, and short inventory positions.
2. Optimal bid and ask quoting spreads based on risk aversion (gamma) and min spread parameters.
3. Out-of-bounds price clipping (1c to 99c bounds).
4. Auto-cancellation triggers when orderbooks are empty.
"""

import pytest
import math
from unittest.mock import patch, AsyncMock, MagicMock
from strategy.market_maker import AvellanedaStoikovBot

@pytest.mark.asyncio
async def test_neutral_pricing():
    """When inventory is 0, reservation price should equal the mid price."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    # Mock manager inputs
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    # Mock output execution
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = 0, Gamma = 0.5 -> ResPrice = 55.0
    # Optimal Bid = floor(55 - 2) = 53
    # Optimal Ask = ceil(55 + 2) = 57
    bot._update_quotes.assert_called_once_with(53, 57)

@pytest.mark.asyncio
async def test_long_skew_pricing():
    """When inventory is positive (long YES), reservation price and quotes should skew downward."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=2) # Holds 2 YES contracts (below threshold of 5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = 2, Gamma = 0.5 -> ResPrice = 55.0 - (2 * 0.5) = 54.0
    # Optimal Bid = floor(54.0 - 2) = 52
    # Optimal Ask = ceil(54.0 + 2) = 56
    bot._update_quotes.assert_called_once_with(52, 56)

@pytest.mark.asyncio
async def test_short_skew_pricing():
    """When inventory is negative (short YES / holding NO), reservation price and quotes should skew upward."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=-2) # Short 2 YES contracts (below threshold of 5)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 55.0, Inv = -2, Gamma = 0.5 -> ResPrice = 55.0 - (-2 * 0.5) = 56.0
    # Optimal Bid = floor(56.0 - 2) = 54
    # Optimal Ask = ceil(56.0 + 2) = 58
    bot._update_quotes.assert_called_once_with(54, 58)

@pytest.mark.asyncio
async def test_active_hedge_long():
    """When long inventory meets/exceeds threshold, bot must halt BIDs and cross ASKs."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=5) # Meets threshold
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Bid is None, Ask is matched to best bid (50)
    bot._update_quotes.assert_called_once_with(None, 50)

@pytest.mark.asyncio
async def test_active_hedge_short():
    """When short inventory meets/exceeds threshold, bot must halt ASKs and cross BIDs."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=-5) # Meets threshold
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(60, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Bid matches best ask (60), Ask is None
    bot._update_quotes.assert_called_once_with(60, None)

@pytest.mark.asyncio
async def test_bounds_clipping():
    """Quotes should be clipped to Kalshi's boundaries (1c to 99c) and not cross each other."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=1.0, min_spread=100, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=0) # 0 position to avoid active hedge
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(2, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(3, 10))
    
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    # Mid = 2.5, Inv = 0 -> ResPrice = 2.5
    # Optimal Bid = floor(2.5 - 50) = -48 -> clipped to 1
    # Optimal Ask = ceil(2.5 + 50) = 53
    # Prevents crossing and verifies clipping
    bot._update_quotes.assert_called_once_with(1, 53)

@pytest.mark.asyncio
async def test_empty_orderbook_handling():
    """If the orderbook is empty, the bot should cancel all active quotes to avoid risk."""
    bot = AvellanedaStoikovBot(ticker="MOCK_TICKER", gamma=0.5, min_spread=4, order_size=1)
    
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    
    # Simulate empty book
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)
    
    bot._cancel_all_quotes = AsyncMock()
    bot._update_quotes = AsyncMock()
    
    await bot._tick()
    
    bot._cancel_all_quotes.assert_called_once()
    bot._update_quotes.assert_not_called()
